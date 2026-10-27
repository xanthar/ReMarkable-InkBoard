"""probe, serve and wait_for_taps - with the tablet replaced by fakes."""

import subprocess
import sys
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest

from inkboard import cli
from inkboard.services.events import InputEvent
from inkboard.services.geometry import Orientation, to_raw
from inkboard.services.link import TabletLink
from inkboard.services.screen import SshGrabber, TestCardGrabber
from inkboard.services.sources import Route
from inkboard.services.strokes import PenPoint
from tests.fake_device import TAP_POINTS
from tests.helpers import down, encode, move, up
from tests.test_probe import DEVICES, NEAR_CORNERS

FAKE = str(Path(__file__).with_name("fake_device.py"))


def _stream() -> bytes:
    events = down(100, 200, 1000) + move(110, 210, 1100) + move(120, 220, 1200) + up()
    return encode([InputEvent(7000 + i, 991 * i, e.type, e.code, e.value) for i, e in enumerate(events)])


def _taps(orientation: Orientation, page_points: list[tuple[float, float]] = NEAR_CORNERS) -> list[PenPoint]:
    return [PenPoint(*to_raw(u, v, orientation), 900) for u, v in page_points]


@pytest.fixture
def tablet(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the three things probe touches: the device listing, the raw sample and the taps."""
    state: dict[str, Any] = {
        "devices": DEVICES,
        "rc": 0,
        "raw": _stream(),
        "taps": _taps(Orientation(True, False, True)),
        "read_raw": [],
    }

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert argv[-1] == "cat /proc/bus/input/devices"
        return subprocess.CompletedProcess(
            argv, state["rc"], state["devices"].encode(), b"Permission denied (publickey)."
        )

    def fake_read_raw(argv: list[str], seconds: float, enough: int | None = None) -> tuple[bytes, str]:
        state["read_raw"].append((seconds, enough))
        return state["raw"], "inkboard-ready\n"

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "read_raw", fake_read_raw)
    monkeypatch.setattr(cli, "wait_for_taps", lambda settings, corners, timeout: state["taps"])
    return state


def test_probe_prints_the_settings(tablet: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["probe", "--wait", "0.1"]) == 0
    out = capsys.readouterr().out
    assert "INKBOARD_PEN_DEVICE=/dev/input/event1" in out
    assert "INKBOARD_EVENT_SIZE=16" in out
    assert "INKBOARD_ORIENTATION=swap_xy,flip_y" in out
    assert "fit error" in out
    assert "within the known ranges" in out
    assert tablet["read_raw"] == [(0.1, cli.SAMPLE_BYTES)]


def test_probe_waits_twenty_seconds_for_pen_data_by_default(tablet: dict[str, Any]) -> None:
    assert cli.main(["probe"]) == 0
    assert tablet["read_raw"] == [(20.0, cli.SAMPLE_BYTES)]


def test_probe_rejects_taps_that_match_no_orientation(
    tablet: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    z_order = [NEAR_CORNERS[0], NEAR_CORNERS[1], NEAR_CORNERS[3], NEAR_CORNERS[2]]
    tablet["taps"] = _taps(Orientation(True, False, True), z_order)
    assert cli.main(["probe"]) == 1
    out = capsys.readouterr().out
    assert "match no orientation" in out
    assert "INKBOARD_ORIENTATION" not in out


def test_probe_stops_when_ssh_fails(tablet: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    tablet["rc"] = 255
    assert cli.main(["probe"]) == 1
    assert "Permission denied (publickey)" in capsys.readouterr().out


def test_probe_stops_without_a_digitizer(tablet: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    tablet["devices"] = DEVICES.replace("Wacom", "Other")
    assert cli.main(["probe"]) == 1
    assert "NOT FOUND" in capsys.readouterr().out


def test_probe_stops_when_frame_size_is_undecided(tablet: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    tablet["raw"] = b""
    assert cli.main(["probe"]) == 1
    assert "UNDECIDED" in capsys.readouterr().out


def test_probe_stops_without_taps(tablet: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    tablet["taps"] = None
    assert cli.main(["probe"]) == 1
    assert "no tap seen" in capsys.readouterr().out


def test_probe_warns_on_values_above_the_ranges(tablet: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    events = down(30000, 200, 1000) + up()
    tablet["raw"] = encode([InputEvent(8000 + i, 13 * i, e.type, e.code, e.value) for i, e in enumerate(events)])
    assert cli.main(["probe"]) == 1
    assert "WARNING: values above the known ranges" in capsys.readouterr().out


def test_probe_reports_an_unreachable_tablet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def timeout(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("ssh", 20)

    monkeypatch.setattr(cli.subprocess, "run", timeout)
    assert cli.main(["probe"]) == 1
    assert "could not reach the tablet" in capsys.readouterr().out


def _fake_stream(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(cli, "pen_stream_argv", lambda settings, reader="dd": list(argv))


def test_wait_for_taps_returns_one_point_per_corner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_stream(monkeypatch, sys.executable, FAKE, "taps")
    corners = ["TOP-LEFT", "TOP-RIGHT", "BOTTOM-RIGHT", "BOTTOM-LEFT"]
    assert cli.wait_for_taps(cli.Settings(), corners, timeout=10) == [PenPoint(x, y, 900) for x, y in TAP_POINTS]
    out = capsys.readouterr().out
    assert [line.strip() for line in out.splitlines()] == [f"tap the {corner} corner" for corner in corners]


def test_wait_for_taps_gives_up_when_a_tap_does_not_come(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_stream(monkeypatch, sys.executable, FAKE, "hold-open")  # connected, one stroke, then nothing
    assert cli.wait_for_taps(cli.Settings(), ["A"], timeout=10) == [PenPoint(100, 200, 1000)]
    assert cli.wait_for_taps(cli.Settings(), ["A", "B"], timeout=1.0) is None


def test_wait_for_taps_gives_up_on_a_fatal_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_stream(monkeypatch, r"C:\nowhere\ssh.exe")
    assert cli.wait_for_taps(cli.Settings(), ["A"], timeout=10) is None


def test_wait_for_taps_times_out_without_a_connection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_stream(monkeypatch, sys.executable, FAKE, "silent")
    assert cli.wait_for_taps(cli.Settings(), ["A"], timeout=0.5) is None
    assert "tap the" not in capsys.readouterr().out


def test_serve_binds_loopback_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[dict[str, Any]] = []
    apps: list[Any] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: (apps.append(app), calls.append(kwargs)))
    fixture = Path(__file__).parent / "fixtures" / "demo.bin"
    assert cli.main(["serve", "--source", "replay", "--replay", str(fixture), "--port", "8123"]) == 0
    (kwargs,) = calls
    assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8123
    assert kwargs["timeout_graceful_shutdown"] == 3
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8123" in out and "Screen view shows a test card" in out
    assert isinstance(apps[0].state.screen._grabber, TestCardGrabber)  # replay never reads a tablet


def test_serve_over_ssh_uses_the_link(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    apps: list[Any] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: apps.append(app))
    assert cli.main(["serve", "--event-size", "24"]) == 0
    assert "/dev/input/event1 on remarkable, over USB or Wi-Fi" in capsys.readouterr().out
    app = apps[0]
    # The real serve path, not only create_app's default: the pen source and the grab share one link.
    assert isinstance(app.state.link, TabletLink)
    grabber = app.state.screen._grabber
    assert isinstance(grabber, SshGrabber) and grabber._argv_for is not None
    assert grabber._argv_for() is None  # not connected yet: no grab
    route = Route("wifi", IPv4Address("192.168.1.42"))
    app.state.link._current = route
    argv = grabber._argv_for()
    assert "HostName=192.168.1.42" in argv and "HostKeyAlias=remarkable" in argv


def test_serve_replay_needs_a_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve", "--source", "replay"]) == 2
    assert "needs --replay FILE" in capsys.readouterr().out
