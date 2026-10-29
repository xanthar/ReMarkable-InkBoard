from ipaddress import IPv4Address
from pathlib import Path

import pytest
from pydantic import ValidationError

from inkboard.core.config import WINDOWS_SSH, Settings, default_ssh_bin, is_wifi_candidate
from inkboard.services.geometry import Orientation

# Example values only (the RFC 7042 documentation MAC range, an example private LAN): never a real device.
MAC = "00:00:5E:00:53:AF"


def test_defaults() -> None:
    s = Settings()
    assert s.ssh_host == "remarkable"
    assert s.pen_device == "/dev/input/event1"
    assert s.event_size == 16
    assert s.orientation_value == Orientation(swap_xy=True, flip_y=True)
    assert s.port == 8000


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKBOARD_SSH_HOST", "tablet-alias")
    monkeypatch.setenv("INKBOARD_EVENT_SIZE", "24")
    monkeypatch.setenv("INKBOARD_ORIENTATION", "flip_y, swap_xy")
    s = Settings()
    assert (s.ssh_host, s.event_size, s.orientation) == ("tablet-alias", 24, "swap_xy,flip_y")


def test_no_personal_wifi_defaults() -> None:
    s = Settings()
    assert s.wifi_addresses == []
    assert s.tablet_mac is None  # no MAC, so no network search until the user sets one


def test_empty_mac_in_env_means_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKBOARD_TABLET_MAC", "  ")
    assert Settings().tablet_mac is None


def test_wifi_addresses_from_env_keep_their_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKBOARD_WIFI_ADDRESSES", " 192.168.1.42 , 10.0.0.7,,172.16.4.4 ")
    assert Settings().wifi_addresses == [
        IPv4Address("192.168.1.42"),
        IPv4Address("10.0.0.7"),
        IPv4Address("172.16.4.4"),
    ]


@pytest.mark.parametrize(
    "value",
    [
        "-oProxyCommand=calc",
        "192.168.1.300",
        "10.11.99.1",  # the USB link
        "127.0.0.1",
        "169.254.3.4",
        "224.0.0.1",
        "0.0.0.0",  # nosec B104 - a rejected test value, nothing binds to it
        "8.8.8.8",  # public
        "100.64.0.1",  # carrier-grade NAT, not a LAN
        "255.255.255.255",  # broadcast
        "240.0.0.1",  # reserved
        "198.18.0.1",  # benchmarking
        "192.0.2.1",  # documentation
        "192.168.1.42,192.168.1.42",
        ",".join(f"192.168.1.{n}" for n in range(1, 18)),  # 17 entries
    ],
)
def test_wifi_addresses_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("INKBOARD_WIFI_ADDRESSES", value)
    with pytest.raises(ValidationError):
        Settings()


def test_sixteen_wifi_addresses_are_allowed() -> None:
    addresses = [IPv4Address(f"192.168.1.{n}") for n in range(1, 17)]
    assert Settings(wifi_addresses=addresses).wifi_addresses == addresses


@pytest.mark.parametrize("mac", ["00-00-5e-00-53-af", "00:00:5e:00:53:AF"])
def test_tablet_mac_is_normalised(mac: str) -> None:
    assert Settings(tablet_mac=mac).tablet_mac == MAC


@pytest.mark.parametrize("mac", ["00:00:5E:00:53", "00:00:5E:00:53:AF:00", "zz:00:5e:00:53:af", "0000.5E00.53AF"])
def test_tablet_mac_rejected(mac: str) -> None:
    with pytest.raises(ValidationError):
        Settings(tablet_mac=mac)


@pytest.mark.parametrize(
    ("address", "ok"),
    [
        ("192.168.1.42", True),
        ("10.0.0.5", True),
        ("172.20.10.2", True),  # a phone hotspot
        ("172.31.0.9", True),
        ("10.11.99.6", False),  # the USB link
        ("8.8.8.8", False),
        ("100.64.0.1", False),
        ("192.0.2.1", False),
        ("198.18.0.1", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
    ],
)
def test_is_wifi_candidate(address: str, ok: bool) -> None:
    assert is_wifi_candidate(IPv4Address(address)) is ok


def test_env_file_is_read_from_the_working_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        f"INKBOARD_TABLET_MAC={MAC}\nINKBOARD_WIFI_ADDRESSES=192.168.1.42\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    s = Settings(_env_file=".env")  # type: ignore[call-arg]  # what the default config does outside the tests
    assert (s.tablet_mac, s.wifi_addresses) == (MAC, [IPv4Address("192.168.1.42")])


def test_real_environment_wins_over_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("INKBOARD_PORT=8100\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INKBOARD_PORT", "8200")
    assert Settings(_env_file=".env").port == 8200  # type: ignore[call-arg]


def test_missing_env_file_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    s = Settings(_env_file=".env")  # type: ignore[call-arg]
    assert s.tablet_mac is None and s.port == 8000


def test_the_isolation_fixture_keeps_a_local_env_away(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(f"INKBOARD_TABLET_MAC={MAC}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Settings().tablet_mac is None  # tests/conftest.py disabled the env file


@pytest.mark.parametrize("host", ["-oProxyCommand=calc", "remark able", "", "host;rm"])
def test_host_cannot_inject_options_or_shell(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings(ssh_host=host)


@pytest.mark.parametrize("device", ["/etc/passwd", "/dev/input/event1; reboot", "/dev/input/mouse0", "event1"])
def test_device_must_be_an_input_event_node(device: str) -> None:
    with pytest.raises(ValidationError):
        Settings(pen_device=device)


@pytest.mark.parametrize("field", [{"event_size": 20}, {"batch_ms": 1}, {"port": 80}, {"orientation": "rotate"}])
def test_out_of_range_values_rejected(field: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**field)  # type: ignore[arg-type]


def test_windows_uses_system32_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    assert default_ssh_bin() == WINDOWS_SSH
    monkeypatch.setattr("sys.platform", "linux")
    assert default_ssh_bin() == "ssh"
