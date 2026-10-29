"""Choosing and watching the link to the tablet: USB or Wi-Fi (ADR 0003).

``resolve()`` runs in the pen source's thread before every connection and walks the connection
order:

1. USB ``10.11.99.1``, when the PC routes it over the USB adapter and port 22 answers;
2. the Wi-Fi address the tablet reported for itself, then the last Wi-Fi address a stream
   connected on (both kept in ``link.json`` outside the repo);
3. ``INKBOARD_WIFI_ADDRESSES``, in order;
4. the search (``discovery.find_tablet``): only when ``INKBOARD_TABLET_MAC`` is set, never while
   the USB route is up, once when the tablet is lost, then after 5 and 15 minutes, then hourly.

A candidate is only a place to try. The pen stream's own ssh run checks the host key (see
``sources.ssh_base_argv``) before any data or key leaves the PC; an address that fails it is
skipped for 10 minutes (never the USB address, whose key changes after a firmware update).

The watcher thread checks every second whether the PC routes ``10.11.99.1`` over the USB
adapter. That check is local: nothing is sent and nothing runs on the tablet. Cable out while on
USB: switch at once. Cable in while on Wi-Fi: two checks in a row and one ssh host-key check,
then switch back.
"""

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path

import structlog

from inkboard.core.config import USB_NET, Settings, is_wifi_candidate
from inkboard.services.discovery import Network, find_tablet
from inkboard.services.sources import Route, ssh_base_argv

log = structlog.get_logger(__name__)

USB_ADDRESS = IPv4Address("10.11.99.1")
CHECK_S = 1.0
USB_CONFIRMATIONS = 2
REJECT_S = 600.0
SEARCH_BACKOFF_S = (300.0, 900.0, 3600.0)  # after the first search of a lost episode
VALIDATE_TIMEOUT_S = 10.0
ISOLATION_HINT = "guest or company Wi-Fi may block devices from reaching each other"
NO_MAC_HINT = "Wi-Fi search off - set INKBOARD_TABLET_MAC (see README, USB and Wi-Fi)"
HOST_KEY_HINT = (
    "different SSH host key - not the tablet, or the tablet after a firmware update "
    "(see README, After a tablet firmware update)"
)
STATE_KEYS = ("wifi_reported", "wifi_last_ok")

Switch = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class Unreachable:
    """No route this time; ``detail`` is one ASCII line for the status chip."""

    detail: str


def default_state_path() -> Path:
    """%LOCALAPPDATA%\\inkboard on Windows; $XDG_STATE_HOME/inkboard (default ~/.local/state) elsewhere."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"])
    else:
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return root / "inkboard" / "link.json"


class TabletLink:
    """Thread-safe. The constructor does no I/O and starts nothing; ``start()`` starts the watcher."""

    def __init__(
        self,
        settings: Settings,
        state_path: Path | None = None,
        *,
        network: Network | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._state_path = state_path or default_state_path()
        self._net = network or Network()
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._net.stopping = self._stop.is_set
        self._current: Route | None = None
        self._last_logged: Route | None = None
        self._rejected: dict[IPv4Address, float] = {}
        self._state: dict[str, IPv4Address] | None = None  # link.json, read on first use
        self._next_search = 0.0
        self._searches = 0
        self._lost_logged = False
        self._positives = 0
        self._watcher: threading.Thread | None = None

    # -- what the rest of inkboard asks ---------------------------------------------------------

    @property
    def current(self) -> Route | None:
        """The route the pen stream is connected on; None while it is not."""
        with self._lock:
            return self._current

    def usb_route_up(self) -> bool:
        local = self._net.local_address_for(USB_ADDRESS)
        return local is not None and local in USB_NET

    def resolve(self) -> Route | Unreachable:
        if self._stop.is_set():
            return Unreachable("inkboard is stopping")
        tried: list[str] = []
        usb_up = self.usb_route_up()
        if usb_up:
            port = self._net.tcp_port(USB_ADDRESS)
            if port == "open":
                return Route("usb", USB_ADDRESS)
            tried.append("USB: no answer (tablet asleep?)" if port == "silent" else "USB: SSH refused")
        else:
            tried.append("USB: cable not connected")
        now = self._clock()
        silent: list[IPv4Address] = []
        for address in self._candidates():
            if self._is_rejected(address, now):
                tried.append(f"{address}: {HOST_KEY_HINT}")
                continue
            port = self._net.tcp_port(address)
            if port == "open":
                return Route("wifi", address)
            if port == "refused":
                tried.append(
                    f"{address}: SSH refused - SSH over Wi-Fi may be off on the tablet "
                    "(see docs/operations/device-setup.md, D3b)"
                )
            else:
                silent.append(address)
                tried.append(f"{address}: no answer (tablet asleep?)")
        if usb_up:
            tried.append("no Wi-Fi search while the USB cable is in")
        elif (mac := self._settings.tablet_mac) is None:
            tried.append(NO_MAC_HINT)
        else:
            with self._lock:
                due = now >= self._next_search
                wait = self._next_search - now
            if due:
                found = self._search(now, mac, silent)
                if isinstance(found, Route):
                    return found
                tried.append(found)
            else:
                tried.append(f"next Wi-Fi search in {max(1, round(wait / 60))} min")
        detail = "; ".join(tried)
        with self._lock:
            first = not self._lost_logged
            self._lost_logged = True
        if first:
            log.info("link.unreachable", detail=detail)
        return Unreachable(detail)

    def connected(self, route: Route) -> None:
        """The pen stream on ``route`` passed the host-key check and runs."""
        with self._lock:
            self._current = route
            self._rejected.pop(route.address, None)
            self._next_search, self._searches, self._lost_logged, self._positives = 0.0, 0, False, 0
            changed = route != self._last_logged
            self._last_logged = route
        if route.link == "wifi":
            self._save("wifi_last_ok", route.address)
        if changed:
            log.info("link.connected", link=route.link, address=str(route.address))

    def lost(self) -> None:
        with self._lock:
            self._current = None

    def rejected(self, route: Route) -> None:
        """ssh refused ``route``'s host key. A Wi-Fi address is skipped for a while; the USB address
        never is, because a firmware update changes the tablet's real key there."""
        log.warning("link.rejected", link=route.link, address=str(route.address), reason="host key")
        if route.link == "wifi":
            with self._lock:
                self._rejected[route.address] = self._clock() + REJECT_S

    def reported_wifi(self, address: IPv4Address) -> None:
        """The tablet's own Wi-Fi address, from the start of the pen stream."""
        if is_wifi_candidate(address):
            self._save("wifi_reported", address)

    # -- the watcher ----------------------------------------------------------------------------

    def start(self, on_switch: Switch) -> None:
        self._watcher = threading.Thread(target=self._watch, args=(on_switch,), name="inkboard-link", daemon=True)
        self._watcher.start()

    def stop(self) -> None:
        self._stop.set()
        self._net.runner.stop()
        if self._watcher is not None:
            self._watcher.join(timeout=5)

    def _watch(self, on_switch: Switch) -> None:
        while not self._stop.wait(CHECK_S):
            self.tick(on_switch)

    def tick(self, on_switch: Switch) -> None:
        """One watcher check (the thread calls it every second; tests call it directly)."""
        current = self.current
        up = self.usb_route_up()
        if current is None:
            self._positives = 0
            return
        if current.link == "usb":
            self._positives = 0
            if not up:
                log.info("link.switch", from_link="usb", reason="cable out")
                self.lost()
                on_switch("USB cable out - trying Wi-Fi")
            return
        self._positives = self._positives + 1 if up else 0
        if self._positives < USB_CONFIRMATIONS:
            return
        self._positives = 0
        if self.validate(USB_ADDRESS):
            log.info("link.switch", from_link="wifi", reason="cable in")
            self.lost()
            on_switch("switching to USB")

    def validate(self, address: IPv4Address) -> bool:
        """One ssh run with the host-key options; True only on exit 0 and the expected word."""
        code, out, _ = self._net.runner.run(
            [*ssh_base_argv(self._settings, address), "echo inkboard-ok"], VALIDATE_TIMEOUT_S
        )
        return code == 0 and out.strip() == b"inkboard-ok"

    # -- internals ------------------------------------------------------------------------------

    def _is_rejected(self, address: IPv4Address, now: float) -> bool:
        with self._lock:
            until = self._rejected.get(address)
        return until is not None and until > now

    def _candidates(self) -> list[IPv4Address]:
        state = self._load()
        ordered = [state.get(key) for key in STATE_KEYS] + list(self._settings.wifi_addresses)
        found: list[IPv4Address] = []
        for address in ordered:
            if address is not None and address not in found:
                found.append(address)
        return found

    def _search(self, now: float, mac: str, silent: list[IPv4Address]) -> Route | str:
        """``silent``: known Wi-Fi addresses that gave no answer just before this search."""
        started = self._clock()
        result = find_tablet(self._net, mac)
        with self._lock:
            self._searches += 1
            self._next_search = now + SEARCH_BACKOFF_S[min(self._searches, len(SEARCH_BACKOFF_S)) - 1]
        log.info(
            "link.search",
            searched=[str(n) for n in result.searched],
            too_large=[str(n) for n in result.too_large],
            vpn=[str(n) for n in result.vpn],
            failed=result.failed,
            touched=result.touched,
            found=len(result.candidates),
            no_ssh=len(result.no_ssh),
            ms=round((self._clock() - started) * 1000),
        )
        for address in result.candidates:
            if is_wifi_candidate(address) and not self._is_rejected(address, now):
                return Route("wifi", address)
        summary = result.summary()
        if result.searched and not result.candidates and not result.no_ssh:
            # A known address inside a searched network means the tablet lives on this network:
            # it is most likely asleep, so the isolation hint would mislead.
            home = any(address in network for address in silent for network in result.searched)
            summary += " - tablet not found" if home else f" - tablet not found ({ISOLATION_HINT})"
        return summary

    def _load(self) -> dict[str, IPv4Address]:
        with self._lock:
            if self._state is not None:
                return self._state
        state: dict[str, IPv4Address] = {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            log.info("link.state_ignored", reason="unreadable")
            raw = {}
        if not isinstance(raw, dict):
            log.info("link.state_ignored", reason="not an object")
            raw = {}
        for key in STATE_KEYS:
            entry = raw.get(key)
            if entry is None:
                continue
            text = entry.get("address") if isinstance(entry, dict) else None
            try:
                address = IPv4Address(text) if isinstance(text, str) else None
            except ValueError:
                address = None
            if address is not None and is_wifi_candidate(address):
                state[key] = address
            else:
                log.info("link.state_ignored", key=key, reason="not a private Wi-Fi address")
        with self._lock:
            if self._state is None:
                self._state = state
            return self._state

    def _save(self, key: str, address: IPv4Address) -> None:
        state = self._load()
        with self._lock:
            if state.get(key) == address:
                return
            state[key] = address
            data = {k: {"address": str(v), "at": datetime.now(UTC).isoformat()} for k, v in state.items()}
        text = json.dumps(data, indent=2) + "\n"
        path = self._state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(text, encoding="utf-8")
            try:
                os.replace(temp, path)
            except PermissionError:  # another program holds the file open: write it in place
                path.write_text(text, encoding="utf-8")
                temp.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("link.state_not_saved", error=type(exc).__name__)
