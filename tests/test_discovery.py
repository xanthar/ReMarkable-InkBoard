import socket
from collections.abc import Callable, Iterator
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

import pytest

from inkboard.services import discovery
from inkboard.services.discovery import (
    ARP,
    POWERSHELL,
    SEARCH_LIMIT_PREFIX,
    Network,
    SearchResult,
    UdpTouch,
    find_tablet,
    local_address_for,
    neighbours,
    parse_neighbours,
    parse_subnets,
    physical_subnets,
    tcp_port,
)
from inkboard.services.sources import Runner
from tests import netguard

# Documentation values only (RFC 7042 MAC range, an example private LAN): never a real device.
MAC = "00:00:5E:00:53:AF"
TABLET = IPv4Address("192.168.1.163")
PC = IPv4Interface("192.168.1.66/24")

ARP_EN = """
Interface: 192.168.1.66 --- 0x7
  Internet Address      Physical Address      Type
  192.168.1.1          00-00-5e-00-53-01     dynamic
  192.168.1.163        00-00-5e-00-53-af     dynamic
  192.168.1.255        ff-ff-ff-ff-ff-ff     static
"""
ARP_DA = """
Grænseflade: 192.168.1.66 --- 0x7
  Internetadresse       Fysisk adresse        Type
  192.168.1.163        00-00-5E-00-53-AF     dynamisk
  192.168.1.163        00-00-5e-00-53-af     dynamisk
  999.1.1.1             00-00-5e-00-53-af     dynamisk
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    netguard.install(monkeypatch)
    yield


class FakeRunner(Runner):
    """Answers powershell and arp from scripted text; records every argv."""

    def __init__(self, subnets: str = "", arp: list[str] | None = None, code: int = 0) -> None:
        super().__init__()
        self.subnets = subnets
        self.arp = list(arp or [""])
        self.code = code
        self.calls: list[list[str]] = []

    def run(self, argv: object, timeout: float) -> tuple[int, bytes, bytes]:
        argv = list(argv)  # type: ignore[call-overload]
        self.calls.append(argv)
        if argv[0] == POWERSHELL:
            return self.code, self.subnets.encode(), b""
        if argv[0] == ARP:
            text = self.arp.pop(0) if len(self.arp) > 1 else self.arp[0]
            return self.code, text.encode(), b""
        raise AssertionError(f"unexpected command {argv[0]}")


def make_net(
    runner: FakeRunner,
    ports: dict[IPv4Address, discovery.Port] | None = None,
    egress: Callable[[IPv4Address], IPv4Address | None] | None = None,
    stopping: Callable[[], bool] = lambda: False,
) -> tuple[Network, list[IPv4Address]]:
    touched: list[IPv4Address] = []
    net = Network(
        runner=runner,
        platform="win32",
        local_address_for=egress or (lambda target: PC.ip),
        tcp_port=lambda address: (ports or {}).get(address, "silent"),
        touch=touched.append,
        sleep=lambda seconds: None,
        stopping=stopping,
    )
    return net, touched


# ---- parsing --------------------------------------------------------------------------------


def test_parse_subnets_keeps_private_lan_only() -> None:
    text = (
        '[{"IPAddress":"10.11.99.2","PrefixLength":29},{"IPAddress":"192.168.1.66","PrefixLength":24},'
        '{"IPAddress":"169.254.0.101","PrefixLength":16},{"IPAddress":"8.8.4.4","PrefixLength":24},'
        '{"IPAddress":"bad","PrefixLength":24},"junk"]'
    )
    assert parse_subnets(text) == [PC]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"IPAddress":"192.168.1.66","PrefixLength":24}', [PC]),  # one object, not a list
        ("", []),
        ("[]", []),
        ("not json", []),
    ],
)
def test_parse_subnets_shapes(text: str, expected: list[IPv4Interface]) -> None:
    assert parse_subnets(text) == expected


def test_parse_neighbours_in_english_and_danish() -> None:
    assert parse_neighbours(ARP_EN, MAC) == [TABLET]
    assert parse_neighbours(ARP_DA, MAC.lower().replace(":", "-")) == [TABLET]  # once, valid only
    assert parse_neighbours(ARP_EN, "00:00:5E:00:53:02") == []


def test_windows_reads_use_fixed_system_paths() -> None:
    runner = FakeRunner(subnets='[{"IPAddress":"192.168.1.66","PrefixLength":24}]', arp=[ARP_EN])
    net, _ = make_net(runner)
    assert physical_subnets(net) == [PC]
    assert neighbours(net, MAC) == [TABLET]
    assert runner.calls[0][0] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert '"' not in runner.calls[0][-1]  # Windows argv quoting cannot change it
    assert runner.calls[1] == [r"C:\Windows\System32\arp.exe", "-a"]


def test_failed_windows_reads_say_so() -> None:
    net, _ = make_net(FakeRunner(subnets="[]", arp=[ARP_EN], code=1))
    assert physical_subnets(net) is None
    assert neighbours(net, MAC) is None


def test_failed_windows_commands_are_named_on_the_chip() -> None:
    net, touched = make_net(FakeRunner(subnets="[]", arp=[ARP_EN], code=1))
    result = find_tablet(net, MAC)
    assert result.failed == ["arp", "PowerShell"] and touched == []
    assert result.summary() == "Wi-Fi search unavailable (arp and PowerShell failed)"


def test_working_commands_report_no_failure() -> None:
    net, _ = make_net(FakeRunner(subnets="[]", arp=[ARP_EN]))
    result = find_tablet(net, "00:00:5E:00:53:01")  # in the cache list, but nothing listens: control
    assert result.failed == []
    assert "unavailable" not in result.summary()


# ---- the search -----------------------------------------------------------------------------


def test_cache_answer_needs_no_traffic() -> None:
    runner = FakeRunner(arp=[ARP_EN])
    net, touched = make_net(runner, ports={TABLET: "open"})
    result = find_tablet(net, MAC)
    assert result.candidates == [TABLET]
    assert touched == [] and all(call[0] != POWERSHELL for call in runner.calls)


def test_sweep_touches_the_subnet_then_finds_the_tablet() -> None:
    runner = FakeRunner(subnets='[{"IPAddress":"192.168.1.66","PrefixLength":24}]', arp=["", ARP_EN])
    net, touched = make_net(runner, ports={TABLET: "open"})
    result = find_tablet(net, MAC)
    assert result.candidates == [TABLET]
    assert result.searched == [IPv4Network("192.168.1.0/24")]
    assert len(touched) == 253 and PC.ip not in touched  # every host but the PC itself
    assert result.touched == 253


def test_tablet_with_ssh_off_is_reported() -> None:
    runner = FakeRunner(arp=[ARP_EN])
    net, _ = make_net(runner, ports={TABLET: "refused"})
    result = find_tablet(net, MAC)
    assert result.candidates == [] and result.no_ssh == [TABLET]
    assert "SSH over Wi-Fi is off (see docs/operations/device-setup.md, D3b)" in result.summary()


def test_mac_seen_but_silent_is_not_a_candidate() -> None:
    net, _ = make_net(FakeRunner(arp=[ARP_EN]), ports={})
    result = find_tablet(net, MAC)
    assert result.candidates == [] and result.no_ssh == []


def test_larger_than_the_limit_is_reported_and_not_touched() -> None:
    # Refusal: a /21 is over the /22 limit. Control: a /22 is searched.
    big = '[{"IPAddress":"10.20.0.5","PrefixLength":21}]'
    net, touched = make_net(FakeRunner(subnets=big))
    result = find_tablet(net, MAC)
    assert touched == [] and result.too_large == [IPv4Network("10.20.0.0/21")]
    assert f"too large to search (over /{SEARCH_LIMIT_PREFIX})" in result.summary()
    assert "INKBOARD_WIFI_ADDRESSES" in result.summary()

    ok = '[{"IPAddress":"10.20.0.5","PrefixLength":22}]'
    net, touched = make_net(FakeRunner(subnets=ok), egress=lambda target: IPv4Address("10.20.0.5"))
    assert find_tablet(net, MAC).too_large == [] and len(touched) == 1021


def test_vpn_routed_subnet_is_skipped() -> None:
    # Refusal: the PC routes the subnet elsewhere. Control: the default fake egress matches.
    subnets = '[{"IPAddress":"192.168.1.66","PrefixLength":24}]'
    net, touched = make_net(FakeRunner(subnets=subnets), egress=lambda target: IPv4Address("10.99.0.2"))
    result = find_tablet(net, MAC)
    assert touched == [] and result.vpn == [IPv4Network("192.168.1.0/24")]
    assert "routed through a VPN, not searched" in result.summary()
    net, touched = make_net(FakeRunner(subnets=subnets))
    find_tablet(net, MAC)
    assert len(touched) == 253


def test_no_route_counts_as_vpn() -> None:
    subnets = '[{"IPAddress":"192.168.1.66","PrefixLength":24}]'
    net, _ = make_net(FakeRunner(subnets=subnets), egress=lambda target: None)
    assert find_tablet(net, MAC).vpn == [IPv4Network("192.168.1.0/24")]


def test_a_single_address_subnet_has_nothing_to_probe() -> None:
    net, touched = make_net(FakeRunner(subnets='[{"IPAddress":"192.168.5.5","PrefixLength":32}]'))
    result = find_tablet(net, MAC)
    assert touched == [] and result.vpn == [IPv4Network("192.168.5.5/32")]


def test_search_stops_when_inkboard_stops() -> None:
    calls = {"n": 0}

    def stopping() -> bool:
        calls["n"] += 1
        return calls["n"] > 10

    subnets = '[{"IPAddress":"192.168.1.66","PrefixLength":24}]'
    net, touched = make_net(FakeRunner(subnets=subnets), stopping=stopping)
    find_tablet(net, MAC)
    assert len(touched) == 10


def test_search_is_windows_only() -> None:
    net, _ = make_net(FakeRunner())
    net.platform = "linux"
    result = find_tablet(net, MAC)
    assert result.available is False
    assert result.summary() == "Wi-Fi search not available on this OS"


def test_summary_never_empty() -> None:
    assert SearchResult().summary() == "no Wi-Fi or Ethernet network to search"
    searched = SearchResult(searched=[IPv4Network("192.168.1.0/24")])
    assert searched.summary() == "searched 192.168.1.0/24"


# ---- the real socket helpers, on loopback only ----------------------------------------------


def test_local_address_for_loopback_and_no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    assert local_address_for(IPv4Address("127.0.0.1")) == IPv4Address("127.0.0.1")

    def boom(self: socket.socket, address: object) -> None:
        raise OSError("no route")

    monkeypatch.setattr(socket.socket, "connect", boom)
    assert local_address_for(IPv4Address("127.0.0.1")) is None


def test_tcp_port_open_refused_and_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert tcp_port(IPv4Address("127.0.0.1"), port) == "open"
    assert tcp_port(IPv4Address("127.0.0.1"), port) == "refused"

    def silent(self: socket.socket, address: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(socket.socket, "connect", silent)
    assert tcp_port(IPv4Address("127.0.0.1"), port) == "silent"


def test_udp_touch_sends_one_empty_datagram() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        port = receiver.getsockname()[1]
        touch = UdpTouch()
        original = discovery.DISCARD_PORT
        try:
            discovery.DISCARD_PORT = port
            touch(IPv4Address("127.0.0.1"))
            data, _ = receiver.recvfrom(16)
        finally:
            discovery.DISCARD_PORT = original
            touch.close()
            touch.close()  # closing twice is harmless
        assert data == b""


def test_udp_touch_ignores_send_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: socket.socket, data: bytes, *args: object) -> int:
        raise OSError("unreachable")

    monkeypatch.setattr(socket.socket, "sendto", refuse)
    touch = UdpTouch()
    touch(IPv4Address("127.0.0.1"))  # no exception
    touch.close()
