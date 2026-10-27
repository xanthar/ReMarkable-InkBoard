"""Finding the tablet on the PC's own network by its MAC address (Windows only; ADR 0003).

The search is kept small and quiet:

- only the private IPv4 subnets of the PC's **physical** adapters (``Get-NetAdapter -Physical``
  leaves out Hyper-V, WSL, Tailscale and VPN adapters), never the USB link;
- a subnet larger than the limit (/22) is not searched, and one that the PC routes somewhere
  else (into a VPN) is skipped: both are reported, never silently cut;
- the PC's neighbour cache is read first (no traffic). If the tablet is not in it, one empty
  UDP datagram per address goes to the discard port (9), only so Windows learns which MAC answers
  where; nothing else is sent to any host;
- port 22 is contacted only on an address with the tablet's MAC, once, to tell "found" from
  "found, but SSH over Wi-Fi is off".

The ssh host-key check, not the MAC, proves an address is the tablet; the MAC only keeps
inkboard away from every other host. All I/O goes through :class:`Network`, so tests inject
fakes and never touch the real network.
"""

import contextlib
import json
import re
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Literal

from inkboard.core.config import USB_NET
from inkboard.services.sources import Runner

POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
ARP = r"C:\Windows\System32\arp.exe"
# Fixed text: no double quotes, so Windows argv quoting passes it unchanged.
SUBNETS_COMMAND = (
    "ConvertTo-Json -Compress -InputObject @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | "
    "Where-Object Status -eq Up | Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
    "Select-Object IPAddress,PrefixLength)"
)
SEARCH_LIMIT_PREFIX = 22  # search networks up to /22 (1,022 addresses); larger ones are reported, not searched
COMMAND_TIMEOUT_S = 15.0
# Windows reports a refused connection only after two retries, about 2.05 s (measured on
# loopback and on the tablet), so a shorter limit would call every refusal silence.
PORT_TIMEOUT_S = 3.0
TOUCH_PAUSE_S = 0.001
SETTLE_S = 1.0  # time for ARP replies to land in the neighbour cache
DISCARD_PORT = 9
# An IPv4 address followed by a dash-separated MAC; no column words, which Windows translates.
ARP_LINE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})", re.IGNORECASE)

Port = Literal["open", "refused", "silent"]


def local_address_for(target: IPv4Address) -> IPv4Address | None:
    """The PC's own address for packets to ``target``, from the routing table. A UDP connect
    sends nothing; None when there is no route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((str(target), DISCARD_PORT))
            return IPv4Address(probe.getsockname()[0])
    except (OSError, ValueError):
        return None


def tcp_port(address: IPv4Address, port: int = 22, timeout: float = PORT_TIMEOUT_S) -> Port:
    try:
        with socket.create_connection((str(address), port), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "silent"


class UdpTouch:
    """Sends one empty datagram to the discard port; errors are ignored (nobody answers UDP 9)."""

    def __init__(self) -> None:
        self._socket: socket.socket | None = None

    def __call__(self, address: IPv4Address) -> None:
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with contextlib.suppress(OSError):
            self._socket.sendto(b"", (str(address), DISCARD_PORT))

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


@dataclass
class Network:
    """Every bit of network I/O the link and the search use, injectable for tests."""

    runner: Runner = field(default_factory=Runner)
    platform: str = sys.platform
    local_address_for: Callable[[IPv4Address], IPv4Address | None] = local_address_for
    tcp_port: Callable[[IPv4Address], Port] = tcp_port
    touch: Callable[[IPv4Address], None] = field(default_factory=UdpTouch)
    sleep: Callable[[float], None] = time.sleep
    stopping: Callable[[], bool] = lambda: False


# ---- reading Windows ---------------------------------------------------------------------------


def parse_subnets(text: str) -> list[IPv4Interface]:
    """The private, non-link-local subnets in ``SUBNETS_COMMAND``'s JSON, without the USB link."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    found: list[IPv4Interface] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            interface = IPv4Interface(f"{item.get('IPAddress')}/{item.get('PrefixLength')}")
        except ValueError:
            continue
        ip = interface.ip
        if ip.is_private and not (ip.is_link_local or ip.is_loopback) and ip not in USB_NET:
            found.append(interface)
    return found


def physical_subnets(net: Network) -> list[IPv4Interface] | None:
    """The searchable subnets, or None when PowerShell could not be run or failed."""
    code, out, _ = net.runner.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", SUBNETS_COMMAND], COMMAND_TIMEOUT_S
    )
    return parse_subnets(out.decode("utf-8", errors="replace")) if code == 0 else None


def normalise_mac(mac: str) -> str:
    return mac.lower().replace(":", "-")


def parse_neighbours(text: str, mac: str) -> list[IPv4Address]:
    """The addresses ``arp -a`` lists for ``mac``, in order, each once."""
    wanted = normalise_mac(mac)
    found: list[IPv4Address] = []
    for match in ARP_LINE.finditer(text):
        if match.group(2).lower() != wanted:
            continue
        try:
            address = IPv4Address(match.group(1))
        except ValueError:
            continue
        if address not in found:
            found.append(address)
    return found


def neighbours(net: Network, mac: str) -> list[IPv4Address] | None:
    """The cached addresses for ``mac``, or None when arp could not be run or failed."""
    code, out, _ = net.runner.run([ARP, "-a"], COMMAND_TIMEOUT_S)
    return parse_neighbours(out.decode("utf-8", errors="replace"), mac) if code == 0 else None


# ---- the search --------------------------------------------------------------------------------


@dataclass
class SearchResult:
    available: bool = True
    candidates: list[IPv4Address] = field(default_factory=list)
    no_ssh: list[IPv4Address] = field(default_factory=list)  # the tablet's MAC, port 22 refused
    searched: list[IPv4Network] = field(default_factory=list)
    too_large: list[IPv4Network] = field(default_factory=list)
    vpn: list[IPv4Network] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # Windows commands that could not be run
    touched: int = 0

    def summary(self) -> str:
        """One ASCII line for the status chip; never empty."""
        if not self.available:
            return "Wi-Fi search not available on this OS"
        parts: list[str] = []
        if self.failed:
            parts.append(f"Wi-Fi search unavailable ({' and '.join(self.failed)} failed)")
        if self.no_ssh:
            where = ", ".join(map(str, self.no_ssh))
            parts.append(
                f"tablet found at {where} but SSH over Wi-Fi is off (see docs/operations/device-setup.md, D3b)"
            )
        if self.searched:
            parts.append(f"searched {', '.join(map(str, self.searched))}")
        if self.too_large:
            parts.append(
                f"{', '.join(map(str, self.too_large))} too large to search (over /{SEARCH_LIMIT_PREFIX}) - "
                "plug in once or set INKBOARD_WIFI_ADDRESSES"
            )
        if self.vpn:
            parts.append(f"{', '.join(map(str, self.vpn))} routed through a VPN, not searched")
        if not parts:
            parts.append("no Wi-Fi or Ethernet network to search")
        return "; ".join(parts)


def _check_found(net: Network, addresses: list[IPv4Address], result: SearchResult) -> None:
    for address in addresses:  # parse_neighbours lists each address once
        state = net.tcp_port(address)
        if state == "open":
            result.candidates.append(address)
        elif state == "refused":
            result.no_ssh.append(address)


def _egress_ok(net: Network, interface: IPv4Interface) -> bool:
    """True when the PC sends this subnet's traffic out of the adapter that owns it."""
    probe = next((host for host in interface.network.hosts() if host != interface.ip), None)
    return probe is not None and net.local_address_for(probe) == interface.ip


def find_tablet(net: Network, mac: str, limit_prefix: int = SEARCH_LIMIT_PREFIX) -> SearchResult:
    result = SearchResult()
    if net.platform != "win32":
        return SearchResult(available=False)
    cached = neighbours(net, mac)
    if cached is None:
        result.failed.append("arp")
    else:
        _check_found(net, cached, result)
    if result.candidates or result.no_ssh:
        return result  # the cache already answered: no traffic needed
    subnets = physical_subnets(net)
    if subnets is None:
        result.failed.append("PowerShell")
    for interface in subnets or []:
        network = interface.network
        if network.prefixlen < limit_prefix:
            result.too_large.append(network)
        elif not _egress_ok(net, interface):
            result.vpn.append(network)
        else:
            result.searched.append(network)
            for host in network.hosts():
                if net.stopping():
                    return result
                if host != interface.ip:
                    net.touch(host)
                    result.touched += 1
                    net.sleep(TOUCH_PAUSE_S)
    if result.touched:
        net.sleep(SETTLE_S)
        after = neighbours(net, mac)
        if after is None:
            if "arp" not in result.failed:
                result.failed.append("arp")
        else:
            _check_found(net, after, result)
    return result
