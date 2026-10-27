import json
import os
import threading
import time
from collections.abc import Iterator, Sequence
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from inkboard.core.config import Settings
from inkboard.services import link as link_module
from inkboard.services.discovery import Network, SearchResult
from inkboard.services.link import (
    HOST_KEY_HINT,
    ISOLATION_HINT,
    NO_MAC_HINT,
    REJECT_S,
    USB_ADDRESS,
    TabletLink,
    Unreachable,
    default_state_path,
)
from inkboard.services.sources import Route, Runner
from tests import netguard

# Example LAN values and the RFC 7042 documentation MAC: never a real network or device.
WIFI = IPv4Address("192.168.1.42")
OTHER = IPv4Address("192.168.1.50")
SET1 = IPv4Address("192.168.1.200")
USB_LOCAL = IPv4Address("10.11.99.2")
LAN_LOCAL = IPv4Address("192.168.1.10")
MAC = "00:00:5E:00:53:AF"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    netguard.install(monkeypatch)
    yield


class FakeRunner(Runner):
    def __init__(self, result: tuple[int, bytes, bytes] = (0, b"inkboard-ok\n", b"")) -> None:
        super().__init__()
        self.result = result
        self.calls: list[list[str]] = []
        self.stopped = False

    def run(self, argv: Sequence[str], timeout: float) -> tuple[int, bytes, bytes]:
        self.calls.append(list(argv))
        return self.result

    def stop(self) -> None:
        self.stopped = True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class World:
    """The PC and network as the link sees them, all switchable from a test."""

    def __init__(self) -> None:
        self.usb_route = True
        self.ports: dict[IPv4Address, str] = {}
        self.runner = FakeRunner()
        self.net = Network(
            runner=self.runner,
            platform="win32",
            local_address_for=lambda target: USB_LOCAL if self.usb_route else LAN_LOCAL,
            tcp_port=lambda address: self.ports.get(address, "silent"),  # type: ignore[arg-type,return-value]
            touch=lambda address: None,
            sleep=lambda seconds: None,
        )


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def searches(monkeypatch: pytest.MonkeyPatch) -> list[SearchResult]:
    """Scripted search results, consumed in order; the default finds nothing."""
    results: list[SearchResult] = []
    calls: list[SearchResult] = []

    def fake_find(net: Network, mac: str) -> SearchResult:
        result = results.pop(0) if results else SearchResult()
        calls.append(result)
        return result

    monkeypatch.setattr(link_module, "find_tablet", fake_find)
    fake_find.calls = calls  # type: ignore[attr-defined]
    return results


def make(world: World, tmp_path: Path, clock: Clock | None = None, **settings: object) -> TabletLink:
    """A link with a tablet MAC set (the search needs one); pass tablet_mac=None to unset it."""
    settings.setdefault("tablet_mac", MAC)
    return TabletLink(Settings(**settings), tmp_path / "link.json", network=world.net, clock=clock or Clock())  # type: ignore[arg-type]


# ---- resolve: the connection order ----------------------------------------------------------


def test_usb_first_when_the_cable_answers(world: World, tmp_path: Path) -> None:
    world.ports[USB_ADDRESS] = "open"
    world.ports[SET1] = "open"
    assert make(world, tmp_path, wifi_addresses=[SET1]).resolve() == Route("usb", USB_ADDRESS)


def test_asleep_on_the_cable_tries_wifi_but_never_searches(
    world: World, tmp_path: Path, searches: list[SearchResult]
) -> None:
    result = make(world, tmp_path, wifi_addresses=[SET1]).resolve()
    assert isinstance(result, Unreachable)
    assert result.detail == (
        f"USB: no answer (tablet asleep?); {SET1}: no answer (tablet asleep?); "
        "no Wi-Fi search while the USB cable is in"
    )


def test_usb_refused_is_named(world: World, tmp_path: Path) -> None:
    world.ports[USB_ADDRESS] = "refused"
    result = make(world, tmp_path).resolve()
    assert isinstance(result, Unreachable) and result.detail.startswith("USB: SSH refused")


def test_order_reported_then_last_ok_then_settings(world: World, tmp_path: Path) -> None:
    world.usb_route = False
    (tmp_path / "link.json").write_text(
        json.dumps({"wifi_reported": {"address": str(WIFI)}, "wifi_last_ok": {"address": str(OTHER)}}),
        encoding="utf-8",
    )
    tried: list[IPv4Address] = []
    world.net.tcp_port = lambda address: tried.append(address) or "silent"  # type: ignore[func-returns-value]
    make(world, tmp_path, wifi_addresses=[OTHER, SET1]).resolve()
    assert tried == [WIFI, OTHER, SET1]  # OTHER once


def test_first_open_wifi_candidate_wins(world: World, tmp_path: Path) -> None:
    world.usb_route = False
    world.ports[SET1] = "open"
    link = make(world, tmp_path, wifi_addresses=[OTHER, SET1])
    assert link.resolve() == Route("wifi", SET1)


def test_refused_wifi_address_points_at_the_runbook(world: World, tmp_path: Path, searches: list[SearchResult]) -> None:
    world.usb_route = False
    world.ports[SET1] = "refused"
    result = make(world, tmp_path, wifi_addresses=[SET1]).resolve()
    assert isinstance(result, Unreachable)
    assert (
        f"{SET1}: SSH refused - SSH over Wi-Fi may be off on the tablet (see docs/operations/device-setup.md, D3b)"
        in result.detail
    )
    assert result.detail.startswith("USB: cable not connected")


def test_rejected_wifi_address_is_skipped_for_ten_minutes(
    world: World, tmp_path: Path, searches: list[SearchResult]
) -> None:
    world.usb_route = False
    world.ports[SET1] = "open"
    clock = Clock()
    link = make(world, tmp_path, clock, wifi_addresses=[SET1])
    link.rejected(Route("wifi", SET1))
    refused = link.resolve()
    assert isinstance(refused, Unreachable) and f"{SET1}: {HOST_KEY_HINT}" in refused.detail
    clock.now += REJECT_S + 1  # control: the same address is used again afterwards
    assert link.resolve() == Route("wifi", SET1)


def test_usb_rejection_is_never_cached(world: World, tmp_path: Path) -> None:
    world.ports[USB_ADDRESS] = "open"
    link = make(world, tmp_path)
    link.rejected(Route("usb", USB_ADDRESS))
    assert link.resolve() == Route("usb", USB_ADDRESS)


def test_connected_clears_a_rejection_and_remembers_wifi(world: World, tmp_path: Path) -> None:
    world.usb_route = False
    world.ports[SET1] = "open"
    link = make(world, tmp_path, wifi_addresses=[SET1])
    link.rejected(Route("wifi", SET1))
    link.connected(Route("wifi", SET1))
    assert link.current == Route("wifi", SET1)
    assert link.resolve() == Route("wifi", SET1)
    saved = json.loads((tmp_path / "link.json").read_text(encoding="utf-8"))
    assert saved["wifi_last_ok"]["address"] == str(SET1) and "at" in saved["wifi_last_ok"]
    link.lost()
    assert link.current is None


def test_usb_connection_is_not_remembered(world: World, tmp_path: Path) -> None:
    link = make(world, tmp_path)
    link.connected(Route("usb", USB_ADDRESS))
    link.connected(Route("usb", USB_ADDRESS))  # same route again: logged once, nothing saved
    assert not (tmp_path / "link.json").exists()


def test_stopping_link_resolves_nothing(world: World, tmp_path: Path) -> None:
    world.ports[USB_ADDRESS] = "open"
    link = make(world, tmp_path)
    link.stop()
    assert link.resolve() == Unreachable("inkboard is stopping")
    assert world.runner.stopped and world.net.stopping()


# ---- the search schedule ---------------------------------------------------------------------


def test_search_runs_once_then_backs_off_and_resets(world: World, tmp_path: Path, searches: list[SearchResult]) -> None:
    world.usb_route = False
    clock = Clock()
    link = make(world, tmp_path, clock)
    calls = link_module.find_tablet.calls  # type: ignore[attr-defined]

    first = link.resolve()
    assert len(calls) == 1 and isinstance(first, Unreachable)
    assert "next Wi-Fi search in 5 min" in str(link.resolve()) and len(calls) == 1
    for wait, expected in ((300, 2), (900, 3), (3600, 4), (3600, 5)):
        clock.now += wait - 1
        link.resolve()
        assert len(calls) == expected - 1  # not yet
        clock.now += 1
        link.resolve()
        assert len(calls) == expected
    link.connected(Route("usb", USB_ADDRESS))  # a connection resets the episode
    link.lost()
    link.resolve()
    assert len(calls) == 6


def test_search_finds_the_tablet(world: World, tmp_path: Path, searches: list[SearchResult]) -> None:
    world.usb_route = False
    searches.append(SearchResult(candidates=[WIFI]))
    assert make(world, tmp_path).resolve() == Route("wifi", WIFI)


def test_no_mac_means_no_search_and_says_so(world: World, tmp_path: Path, searches: list[SearchResult]) -> None:
    world.usb_route = False
    searches.append(SearchResult(candidates=[WIFI]))  # would be found if a search ran
    calls = link_module.find_tablet.calls  # type: ignore[attr-defined]
    result = make(world, tmp_path, tablet_mac=None).resolve()
    assert isinstance(result, Unreachable) and NO_MAC_HINT in result.detail
    assert calls == []  # nothing was searched, so nothing was sent on the network


def test_no_mac_still_uses_listed_addresses(world: World, tmp_path: Path) -> None:
    world.usb_route = False
    world.ports[SET1] = "open"
    assert make(world, tmp_path, tablet_mac=None, wifi_addresses=[SET1]).resolve() == Route("wifi", SET1)


def test_search_skips_a_rejected_or_public_candidate(
    world: World, tmp_path: Path, searches: list[SearchResult]
) -> None:
    world.usb_route = False
    searches.append(SearchResult(candidates=[IPv4Address("8.8.8.8"), WIFI]))
    link = make(world, tmp_path)
    link.rejected(Route("wifi", WIFI))
    assert isinstance(link.resolve(), Unreachable)


def test_isolation_hint_only_after_a_search_found_nothing(
    world: World, tmp_path: Path, searches: list[SearchResult]
) -> None:
    from ipaddress import IPv4Network

    world.usb_route = False
    searches.append(SearchResult(searched=[IPv4Network("192.168.1.0/24")]))
    found_nothing = make(world, tmp_path).resolve()
    assert isinstance(found_nothing, Unreachable) and ISOLATION_HINT in found_nothing.detail

    searches.append(SearchResult(no_ssh=[WIFI], searched=[IPv4Network("192.168.1.0/24")]))
    ssh_off = make(world, tmp_path / "b").resolve()
    assert isinstance(ssh_off, Unreachable) and ISOLATION_HINT not in ssh_off.detail
    assert "SSH over Wi-Fi is off" in ssh_off.detail


def test_no_isolation_hint_when_a_known_address_in_the_searched_network_is_silent(
    world: World, tmp_path: Path, searches: list[SearchResult]
) -> None:
    """The tablet asleep at home: its known address is silent and inside the searched network."""
    from ipaddress import IPv4Network

    world.usb_route = False
    searches.append(SearchResult(searched=[IPv4Network("192.168.1.0/24")]))
    asleep = make(world, tmp_path, wifi_addresses=[WIFI]).resolve()
    assert isinstance(asleep, Unreachable)
    assert asleep.detail == (
        f"USB: cable not connected; {WIFI}: no answer (tablet asleep?); searched 192.168.1.0/24 - tablet not found"
    )

    # Control: the known address belongs to another network, so isolation stays a likely cause.
    elsewhere = IPv4Address("10.20.0.5")
    searches.append(SearchResult(searched=[IPv4Network("192.168.1.0/24")]))
    away = make(world, tmp_path / "b", wifi_addresses=[elsewhere]).resolve()
    assert isinstance(away, Unreachable) and ISOLATION_HINT in away.detail


# ---- link.json --------------------------------------------------------------------------------


def test_reported_address_is_remembered_and_used(world: World, tmp_path: Path) -> None:
    link = make(world, tmp_path)
    link.reported_wifi(WIFI)
    link.reported_wifi(WIFI)  # unchanged: no second write
    link.reported_wifi(IPv4Address("8.8.8.8"))  # public: ignored
    world.usb_route = False
    world.ports[WIFI] = "open"
    assert make(world, tmp_path).resolve() == Route("wifi", WIFI)  # a new link reads the file


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[1, 2]",
        json.dumps({"wifi_reported": {"address": "8.8.8.8"}, "wifi_last_ok": "192.168.1.163"}),
        json.dumps({"wifi_reported": {"address": "10.11.99.1"}, "wifi_last_ok": {"address": "nonsense"}}),
    ],
)
def test_bad_state_is_ignored(world: World, tmp_path: Path, content: str, searches: list[SearchResult]) -> None:
    (tmp_path / "link.json").write_text(content, encoding="utf-8")
    world.usb_route = False
    tried: list[IPv4Address] = []
    world.net.tcp_port = lambda address: tried.append(address) or "silent"  # type: ignore[func-returns-value]
    assert isinstance(make(world, tmp_path).resolve(), Unreachable)
    assert tried == []


def test_unreadable_state_file_is_ignored(world: World, tmp_path: Path, searches: list[SearchResult]) -> None:
    (tmp_path / "link.json").mkdir()  # reading a directory raises OSError
    world.usb_route = False
    assert isinstance(make(world, tmp_path).resolve(), Unreachable)


def test_save_falls_back_to_writing_in_place(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def locked(src: object, dst: object) -> None:
        raise PermissionError("held open by another program")

    monkeypatch.setattr(os, "replace", locked)
    make(world, tmp_path).reported_wifi(WIFI)
    saved = json.loads((tmp_path / "link.json").read_text(encoding="utf-8"))
    assert saved["wifi_reported"]["address"] == str(WIFI)
    assert not (tmp_path / "link.json.tmp").exists()


def test_save_failure_is_not_fatal(world: World, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    link = TabletLink(Settings(), blocker / "inkboard" / "link.json", network=world.net)
    link.reported_wifi(WIFI)  # the parent is a file: mkdir fails, logged, no exception


def test_default_state_path_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(link_module.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_state_path() == tmp_path / "inkboard" / "link.json"
    monkeypatch.delenv("LOCALAPPDATA")  # no LOCALAPPDATA: the same fallback as elsewhere
    assert default_state_path() == Path.home() / ".local" / "state" / "inkboard" / "link.json"


def test_default_state_path_elsewhere_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(link_module.sys, "platform", "linux")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "ignored"))  # not Windows: never used
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_state_path() == tmp_path / "state" / "inkboard" / "link.json"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert default_state_path() == Path.home() / ".local" / "state" / "inkboard" / "link.json"


# ---- the watcher --------------------------------------------------------------------------------


def test_watcher_leaves_usb_at_once_when_the_cable_goes(world: World, tmp_path: Path) -> None:
    link = make(world, tmp_path)
    switched: list[str] = []
    link.tick(switched.append)  # not connected: nothing to do
    link.connected(Route("usb", USB_ADDRESS))
    link.tick(switched.append)  # cable in: stay
    assert switched == []
    world.usb_route = False
    link.tick(switched.append)
    assert switched == ["USB cable out - trying Wi-Fi"] and link.current is None


def test_watcher_needs_two_checks_and_the_host_key_to_return_to_usb(world: World, tmp_path: Path) -> None:
    link = make(world, tmp_path)
    switched: list[str] = []
    link.connected(Route("wifi", WIFI))
    link.tick(switched.append)  # refusal: one check is not enough
    assert switched == [] and world.runner.calls == []
    world.usb_route = False
    link.tick(switched.append)  # a gap resets the count
    world.usb_route = True
    link.tick(switched.append)
    assert switched == []
    link.tick(switched.append)  # control: two in a row, host key OK
    assert switched == ["switching to USB"] and link.current is None
    argv = world.runner.calls[0]
    assert "HostName=10.11.99.1" in argv and "HostKeyAlias=remarkable" in argv and argv[-1] == "echo inkboard-ok"


def test_watcher_stays_on_wifi_when_the_usb_host_key_fails(world: World, tmp_path: Path) -> None:
    world.runner.result = (255, b"", b"Host key verification failed.")
    link = make(world, tmp_path)
    switched: list[str] = []
    link.connected(Route("wifi", WIFI))
    link.tick(switched.append)
    link.tick(switched.append)
    assert switched == [] and link.current == Route("wifi", WIFI)


@pytest.mark.parametrize(
    ("result", "ok"),
    [((0, b"inkboard-ok\r\n", b""), True), ((0, b"something else", b""), False), ((255, b"inkboard-ok", b""), False)],
)
def test_validate_needs_exit_zero_and_the_word(
    world: World, tmp_path: Path, result: tuple[int, bytes, bytes], ok: bool
) -> None:
    world.runner.result = result
    assert make(world, tmp_path).validate(USB_ADDRESS) is ok


def test_watcher_thread_runs_and_stops(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(link_module, "CHECK_S", 0.01)
    link = make(world, tmp_path)
    link.connected(Route("usb", USB_ADDRESS))
    world.usb_route = False
    switched = threading.Event()
    link.start(lambda detail: switched.set())
    try:
        assert switched.wait(5)
    finally:
        started = time.monotonic()
        link.stop()
    assert time.monotonic() - started < 1
