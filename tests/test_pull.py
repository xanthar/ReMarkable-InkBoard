"""``inkboard snapshot`` against a real uvicorn server with a fake pen source and a fake grabber.

The server never reads a tablet: ``FakeSource`` and ``FakeGrabber`` stand in, and netguard fails
the test on any connection beyond loopback or any child process.
"""

import hashlib
import http.client
import json
import logging
import os
import re
import socket
import struct
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from websockets.exceptions import ConnectionClosedError
from websockets.sync.client import ClientConnection, connect
from websockets.typing import Origin

from inkboard import cli
from inkboard.app import create_app
from inkboard.core.config import Settings
from inkboard.core.logging import configure_logging
from inkboard.services import pull
from inkboard.services.screen import FRAME_H, FRAME_W, ScreenError, decode, make_test_card
from inkboard.services.sources import SourceStatus
from tests import netguard
from tests.fakes import FakeGrabber, FakeSource, frame_with
from tests.helpers import down, move, up

LINE = re.compile(
    r"^saved n=(?P<n>\d+) at=(?P<at>\S+) layout=(?P<layout>\S+) turn=(?P<turn>none|cw)"
    r" source=(?P<source>usb|wifi|test-card) file=(?P<file>.+)$"
)
GREY = b"\x80\x80\x80\xff"  # BGRA; grey 128 is not "dark" to the icon masks, so layouts still match
MARKER = (700, 900)  # a frame pixel outside every layout's menu


# ---- a server in a thread ----------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@contextmanager
def serving(
    grabber: FakeGrabber | None = None,
    source: FakeSource | None = None,
    before: Callable[[FastAPI], None] | None = None,
) -> Iterator[tuple[int, FastAPI]]:
    configure_logging()  # as serve does: the server's log lines go to stderr, never stdout
    port = free_port()
    app = create_app(Settings(port=port, batch_ms=4), source or FakeSource(), grabber or FakeGrabber())
    if before is not None:
        before(app)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    try:
        yield port, app
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn did not shut down"


@pytest.fixture(autouse=True)
def guarded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    netguard.install(monkeypatch)
    yield
    logging.getLogger("websockets").setLevel(logging.NOTSET)  # the command sets it; keep tests apart


def page_socket(port: int) -> ClientConnection:
    """A second client, as a browser page connects: same URL, the page's own origin."""
    return connect(f"ws://127.0.0.1:{port}/api/v1/ws", origin=Origin(f"http://127.0.0.1:{port}"), open_timeout=5)


def snapshot(port: int, out: Path, *extra: str) -> int:
    return cli.main(["snapshot", "--port", str(port), "--out", str(out), *extra])


def in_thread(fn: Callable[[], int]) -> tuple[threading.Thread, list[int]]:
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(fn()), daemon=True)
    thread.start()
    return thread, result


def wait_for(condition: Callable[[], bool], seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


# ---- reading the PNG back ------------------------------------------------------------------------


def read_png(path: Path) -> tuple[list[bytes], int, int, bytes]:
    """Chunk names, width, height and RGB pixels of a PNG written by the command (filter 0 only)."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    names, idat, pos = [], b"", 8
    width = height = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        name, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        assert struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0] == zlib.crc32(name + body)
        names.append(name)
        if name == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert (depth, colour, interlace) == (8, 2, 0)
        elif name == b"IDAT":
            idat += body
        pos += 12 + length
    raw, stride = zlib.decompress(idat), width * 3
    rows = [raw[r * (stride + 1) : (r + 1) * (stride + 1)] for r in range(height)]
    assert all(row[0] == 0 for row in rows)
    return names, width, height, b"".join(row[1:] for row in rows)


def rgb_of(bgra: bytes, *, paint: bool = True) -> bytes:
    """What the command should write for a portrait frame: menu painted, alpha dropped."""
    rgba, _, menu = decode(bgra)
    painted = bytearray(rgba)
    if paint:
        pull.paint_menu(painted, menu)
    return pull.rgb_from_rgba(bytes(painted))


def pixel(rgb: bytes, width: int, x: int, y: int) -> bytes:
    start = (y * width + x) * 3
    return rgb[start : start + 3]


def with_marker(frame: bytes) -> bytes:
    out = bytearray(frame)
    start = (MARKER[1] * FRAME_W + MARKER[0]) * 4
    out[start : start + 4] = b"\x00\x00\x00\xff"
    return bytes(out)


# ---- the happy path ------------------------------------------------------------------------------


def test_snapshot_writes_the_current_screen_as_a_png(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "screen.png"
    with serving() as (port, _):
        assert snapshot(port, out) == 0
    stdout = capfd.readouterr().out.strip().splitlines()
    assert len(stdout) == 1
    line = LINE.match(stdout[0])
    assert line is not None
    assert line["n"] == "1" and line["layout"] == "portrait-open" and line["turn"] == "none"
    assert line["source"] == "test-card"  # a run that never saw a link is a replay
    assert Path(line["file"]) == out.resolve()
    names, width, height, rgb = read_png(out)
    assert names == [b"IHDR", b"IDAT", b"IEND"]  # no text, time or colour-profile chunks
    assert (width, height) == (FRAME_W, FRAME_H)
    assert rgb == rgb_of(make_test_card(1))
    assert [p.name for p in tmp_path.iterdir()] == ["screen.png"]  # no temp file left


def test_the_readme_documents_the_same_line() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    documented = re.search(r"^\s*(\^saved .*\$)$", readme, re.MULTILINE)
    example = re.search(r"^\s*(saved n=\d+ .*)$", readme, re.MULTILINE)
    assert documented is not None and example is not None
    assert documented[1] == re.sub(r"\?P<\w+>", "", LINE.pattern)  # the README is the contract
    assert LINE.match(example[1])


def test_the_parent_folder_is_created(tmp_path: Path) -> None:
    out = tmp_path / "new" / "deeper" / "screen.png"
    with serving() as (port, _):
        assert snapshot(port, out) == 0
    assert out.is_file()


# ---- a fresh frame ---------------------------------------------------------------------------------


def test_a_grab_already_running_is_not_taken_one_started_after_is(tmp_path: Path) -> None:
    gate = threading.Event()
    grabber = FakeGrabber(gate=gate)
    out = tmp_path / "screen.png"
    with serving(grabber) as (port, app), page_socket(port) as page:
        page.send(json.dumps({"type": "screen", "action": "on"}))
        assert grabber.started.wait(5)  # grab 1 runs for the page, held at the gate
        thread, result = in_thread(lambda: snapshot(port, out))
        wait_for(lambda: app.state.screen.subscriber_count == 2)
        gate.set()
        thread.join(10)
    assert result == [0]
    assert grabber.calls == 2
    assert read_png(out)[3] == rgb_of(make_test_card(2))  # the frame grabbed after it asked


def test_with_no_grab_running_one_grab_is_enough(tmp_path: Path) -> None:
    grabber = FakeGrabber()
    with serving(grabber) as (port, _):
        assert snapshot(port, tmp_path / "screen.png") == 0
    assert grabber.calls == 1


class FakeSocket:
    """Just enough of a ClientConnection for ``await_frame``: scripted messages in, sends recorded."""

    def __init__(self, *messages: dict[str, Any] | bytes) -> None:
        self.messages = [m if isinstance(m, bytes) else json.dumps(m) for m in messages]
        self.sent: list[dict[str, Any]] = []

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv(self, timeout: float | None = None) -> str | bytes:
        return self.messages.pop(0)


def screen(state: str, **fields: Any) -> dict[str, Any]:
    return {"type": "screen", "state": state, **fields}


def test_a_stale_ready_is_answered_with_one_refresh_only() -> None:
    ws = FakeSocket(screen("ready", n=1), screen("ready", n=2), screen("updating"), screen("ready", n=3))
    assert pull.await_frame(ws, lambda: 5.0) == (3, "test-card")  # type: ignore[arg-type]
    assert [m["action"] for m in ws.sent] == ["on", "refresh"]


def test_a_fresh_ready_needs_no_refresh() -> None:
    ws = FakeSocket({"type": "snapshot", "strokes": []}, b"\x00binary", screen("updating"), screen("ready", n=1))
    assert pull.await_frame(ws, lambda: 5.0) == (1, "test-card")  # type: ignore[arg-type]
    assert [m["action"] for m in ws.sent] == ["on"]


def test_a_stale_error_is_not_taken_for_this_run() -> None:
    ws = FakeSocket(screen("error", kind="no_link", detail="x"), screen("updating"), screen("ready", n=2))
    assert pull.await_frame(ws, lambda: 5.0)[0] == 2  # type: ignore[arg-type]


def test_a_ready_without_a_frame_number_is_refused() -> None:
    ws = FakeSocket(screen("updating"), screen("ready", n="1"))
    with pytest.raises(pull.PullError) as refused:
        pull.await_frame(ws, lambda: 5.0)  # type: ignore[arg-type]
    assert refused.value.code == pull.GRAB_FAILED


def test_messages_that_are_not_objects_are_ignored() -> None:
    assert pull.parse(b"\x00") == {} and pull.parse("[1, 2]") == {}
    assert pull.parse('{"type": "cleared"}') == {"type": "cleared"}  # control


# ---- the link label ------------------------------------------------------------------------------


def test_the_last_link_seen_labels_the_frame_even_after_the_link_drops(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    gate = threading.Event()
    grabber = FakeGrabber(gate=gate)
    source = FakeSource(SourceStatus("connected", link="wifi", address="192.168.1.20"))
    out = tmp_path / "screen.png"
    with serving(grabber, source) as (port, app):
        thread, result = in_thread(lambda: snapshot(port, out))
        assert grabber.started.wait(5)
        source.status(SourceStatus("down", "lost", retrying=True))  # link None, before the frame
        wait_for(lambda: app.state.hub.status.state == "down")
        gate.set()
        thread.join(10)
    assert result == [0]
    line = LINE.match(capfd.readouterr().out.strip())
    assert line is not None and line["source"] == "wifi"  # control: a run with no link says test-card


# ---- turning and the menu ------------------------------------------------------------------------


def landscape(_n: int) -> bytes:
    return with_marker(frame_with((36, 36, "close"), (36, 1796, "toggle-landscape-open")))


def test_a_landscape_screen_is_turned_clockwise(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "screen.png"
    with serving(FakeGrabber(frames=landscape)) as (port, _):
        assert snapshot(port, out) == 0
    line = LINE.match(capfd.readouterr().out.strip())
    assert line is not None and line["layout"] == "landscape-open" and line["turn"] == "cw"
    _, width, height, rgb = read_png(out)
    assert (width, height) == (FRAME_H, FRAME_W)
    x, y = MARKER
    assert pixel(rgb, width, FRAME_H - 1 - y, x) == b"\x00\x00\x00"  # (x, y) -> (H-1-y, x)
    assert pixel(rgb, width, x, y) == b"\xff\xff\xff"


def test_turn_none_keeps_a_landscape_screen_upright_in_the_frame(tmp_path: Path) -> None:
    out = tmp_path / "screen.png"
    with serving(FakeGrabber(frames=landscape)) as (port, _):
        assert snapshot(port, out, "--turn", "none") == 0
    _, width, height, rgb = read_png(out)
    assert (width, height) == (FRAME_W, FRAME_H)
    assert pixel(rgb, width, *MARKER) == b"\x00\x00\x00"


def test_turn_cw_turns_a_portrait_screen_too(tmp_path: Path) -> None:
    out = tmp_path / "screen.png"
    with serving() as (port, _):
        assert snapshot(port, out, "--turn", "cw") == 0
    assert read_png(out)[1:3] == (FRAME_H, FRAME_W)


def test_an_unknown_layout_is_written_as_is(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    unknown = with_marker(frame_with())  # no icons: a pop-up, the library, a left-handed toolbar
    out = tmp_path / "screen.png"
    with serving(FakeGrabber(frames=lambda _n: unknown)) as (port, _):
        assert snapshot(port, out) == 0
    line = LINE.match(capfd.readouterr().out.strip())
    assert line is not None and line["layout"] == "unknown" and line["turn"] == "none"
    assert read_png(out)[3] == rgb_of(unknown, paint=False)


def test_the_menu_is_painted_white_and_nothing_around_it(tmp_path: Path) -> None:
    grey = frame_with((1328, 36, "close"), (36, 36, "toggle-portrait-open"), paper=GREY * (FRAME_W * FRAME_H))
    out = tmp_path / "screen.png"
    with serving(FakeGrabber(frames=lambda _n: grey)) as (port, _):
        assert snapshot(port, out) == 0
    _, width, _, rgb = read_png(out)
    white, kept = b"\xff\xff\xff", b"\x80\x80\x80"
    # portrait-open's menu: (0, 0, 116, 1872) and (1320, 28, 56, 56)
    assert pixel(rgb, width, 115, 500) == white and pixel(rgb, width, 116, 500) == kept
    assert pixel(rgb, width, 1320, 50) == white and pixel(rgb, width, 1319, 50) == kept
    assert pixel(rgb, width, 1350, 28) == white and pixel(rgb, width, 1350, 27) == kept
    assert pixel(rgb, width, 1350, 83) == white and pixel(rgb, width, 1350, 84) == kept


def test_the_menu_header_is_read_strictly() -> None:
    assert pull.parse_menu("") == ()
    assert pull.parse_menu("0,0,116,1872;1320,28,56,56") == ((0, 0, 116, 1872), (1320, 28, 56, 56))
    for bad in ("1,2,3", "0,0,1405,10", "0,1800,10,100", "-1,0,1,1", "a,b,c,d"):
        with pytest.raises(ValueError):
            pull.parse_menu(bad)


# ---- the frame route -----------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status, self._body, self.headers = status, body, headers

    def read(self) -> bytes:
        return self._body


def good_headers(**changes: str) -> dict[str, str]:
    headers = {
        "X-Inkboard-Frame-N": "3",
        "X-Inkboard-Frame-At": "2026-01-01T12:00:00+00:00",
        "X-Inkboard-Frame-Size": f"{FRAME_W}x{FRAME_H}",
        "X-Inkboard-Frame-Layout": "portrait-open",
        "X-Inkboard-Frame-Menu": "0,0,116,1872",
    }
    headers.update(changes)
    return {k: v for k, v in headers.items() if v != "-"}


def answer(monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, headers: dict[str, str]) -> None:
    class Connection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def request(self, method: str, path: str) -> None:
            assert (method, path) == ("GET", pull.FRAME_PATH)

        def getresponse(self) -> FakeResponse:
            return FakeResponse(status, body, headers)

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)


FRAME = make_test_card(3)


@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        (404, b"{}", {}),
        (200, FRAME[:-4], good_headers()),
        (200, FRAME, good_headers(**{"X-Inkboard-Frame-Size": "1872x1404"})),
        (200, FRAME, good_headers(**{"X-Inkboard-Frame-N": "1"})),  # older than the ready's n=2
        (200, FRAME, good_headers(**{"X-Inkboard-Frame-N": "-"})),
        (200, FRAME, good_headers(**{"X-Inkboard-Frame-At": "yesterday"})),
        (200, FRAME, good_headers(**{"X-Inkboard-Frame-Layout": ""})),
        (200, FRAME, good_headers(**{"X-Inkboard-Frame-Menu": "1,2,3"})),
    ],
    ids=["404", "short-body", "size-header", "older-n", "no-n", "bad-time", "no-layout", "bad-menu"],
)
def test_a_frame_that_does_not_check_out_is_refused(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, headers: dict[str, str]
) -> None:
    answer(monkeypatch, status, body, headers)
    with pytest.raises(pull.PullError) as refused:
        pull.fetch_frame(8000, 2, "usb", 5.0)
    assert refused.value.code == pull.GRAB_FAILED


def test_a_frame_that_checks_out_is_taken_with_its_own_number(monkeypatch: pytest.MonkeyPatch) -> None:
    answer(monkeypatch, 200, FRAME, good_headers())
    pulled = pull.fetch_frame(8000, 2, "usb", 5.0)
    assert (pulled.n, pulled.layout, pulled.menu, pulled.source) == (3, "portrait-open", ((0, 0, 116, 1872),), "usb")
    assert pulled.rgba == FRAME and "rgba" not in repr(pulled)


# ---- a large page --------------------------------------------------------------------------------


def large_history(app: FastAPI) -> None:
    events = down(1000, 1000, 2000)
    for i in range(60_000):
        events += move(1000 + i % 15000, 1000 + i // 15000 * 3000 + i % 7, 2000)
    app.state.hub.handle_events(events + up())
    assert len(json.dumps(app.state.hub.snapshot())) > 2**20


def test_a_stroke_history_over_one_mebibyte_is_read(tmp_path: Path) -> None:
    with serving(before=large_history) as (port, _):
        assert snapshot(port, tmp_path / "screen.png") == 0
        # control: the library's default limit would refuse that first message
        with page_socket(port) as ws, pytest.raises(ConnectionClosedError) as closed:
            ws.recv(timeout=5)
    assert closed.value.sent is not None and closed.value.sent.code == 1009


# ---- failures and their exit codes ---------------------------------------------------------------


def only_error_line(capfd: pytest.CaptureFixture[str]) -> str:
    captured = capfd.readouterr()
    assert captured.out == ""
    lines = [line for line in captured.err.splitlines() if line.startswith("snapshot:")]
    assert len(lines) == 1  # the server's own log lines share this process's stderr
    return lines[0]


@pytest.mark.parametrize(
    ("kind", "code", "says"),
    [
        ("no_link", pull.NOT_CONNECTED, "tablet not connected - wake it"),
        ("timeout", pull.NOT_CONNECTED, "tablet not connected - wake it"),
        ("ssh", pull.NOT_CONNECTED, "tablet not reachable (the detail)"),
        ("firmware", pull.GRAB_FAILED, "screen grab failed: the detail"),
        ("not_image", pull.GRAB_FAILED, "screen grab failed: the detail"),
    ],
)
def test_a_failed_grab_maps_to_its_exit_code(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], kind: str, code: int, says: str
) -> None:
    out = tmp_path / "screen.png"
    with serving(FakeGrabber(error=ScreenError(kind, "the detail"))) as (port, _):
        assert snapshot(port, out) == code
    assert says in only_error_line(capfd)
    assert not out.exists()


def test_an_error_without_a_kind_is_a_grab_error() -> None:  # an older serve sends no kind
    assert pull.grab_error({"detail": "x"}).code == pull.GRAB_FAILED


def test_no_server_is_exit_3(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert snapshot(free_port(), tmp_path / "screen.png") == pull.UNREACHABLE
    assert "start it with: uv run inkboard serve" in only_error_line(capfd)


def test_a_server_that_closes_the_connection_is_exit_3(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    gate = threading.Event()
    grabber = FakeGrabber(gate=gate)
    with serving(grabber) as (port, app):
        thread, result = in_thread(lambda: snapshot(port, tmp_path / "screen.png"))
        assert grabber.started.wait(5)

        def overflow() -> None:  # what the hub does to a client that falls behind
            for client in app.state.hub._clients:
                client.overflowed = True
                client.queue.put_nowait({"type": "cleared"})

        app.state.screen._loop.call_soon_threadsafe(overflow)
        thread.join(10)
        gate.set()
    assert result == [pull.UNREACHABLE]
    assert "closed the connection (1013)" in only_error_line(capfd)


def test_no_frame_in_time_is_exit_6(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    with serving(FakeGrabber(gate=threading.Event())) as (port, _):
        started = time.monotonic()
        assert snapshot(port, tmp_path / "screen.png", "--timeout", "1") == pull.TIMED_OUT
        assert time.monotonic() - started < 3
    assert "no frame within 1 s" in only_error_line(capfd)


def test_a_file_that_cannot_be_written_is_exit_1_and_leaves_nothing(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refuse)
    out = tmp_path / "screen.png"
    with serving() as (port, _):
        assert snapshot(port, out) == pull.UNEXPECTED
    assert f"could not write {out.resolve()}: Permission denied" in only_error_line(capfd)
    assert list(tmp_path.iterdir()) == []  # the temp file is gone too


@pytest.mark.parametrize(
    ("extra", "says"),
    [
        (("--timeout", "0"), "--timeout must be more than 0"),
        (("--port", "80"), "invalid setting: port"),
    ],
)
def test_bad_options_are_usage_errors(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], extra: tuple[str, ...], says: str
) -> None:
    assert cli.main(["snapshot", "--out", str(tmp_path / "screen.png"), *extra]) == pull.USAGE
    assert says in only_error_line(capfd)


def test_a_folder_as_out_is_a_usage_error(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert snapshot(free_port(), tmp_path) == pull.USAGE
    assert "is a folder" in only_error_line(capfd)


def test_an_out_inside_a_git_work_tree_is_refused(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    out = tmp_path / "repo" / "notes" / "screen.png"
    with serving() as (port, _):
        assert snapshot(port, out) == pull.USAGE
        assert snapshot(port, tmp_path / "outside" / "screen.png") == 0  # control: no .git above
    assert "inside the git work tree" in capfd.readouterr().err
    assert not out.exists() and not out.parent.exists()


# ---- privacy -------------------------------------------------------------------------------------


def one_stroke(app: FastAPI) -> None:
    app.state.hub.handle_events(down(4321, 8765, 2000) + move(4400, 8800, 2000) + up())


def marker_of(app: FastAPI) -> str:
    value = str(app.state.hub.snapshot()["strokes"][0]["pts"][0][0])
    assert len(value) >= 5  # distinctive enough not to appear by chance
    return value


def test_no_handwriting_reaches_the_output_even_on_a_failure(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(FakeGrabber(frames=lambda _n: with_marker(make_test_card(1))), before=one_stroke) as (port, app):
        marker = marker_of(app)
        assert snapshot(port, tmp_path / "ok.png") == 0
        ok = capfd.readouterr()

        def leak(*_args: Any) -> bytes:
            raise ValueError(f"pixels near {marker}")  # control: the marker is in the message

        monkeypatch.setattr(pull, "encode_png", leak)
        assert snapshot(port, tmp_path / "failed.png") == pull.UNEXPECTED
        failed = capfd.readouterr()
    assert "snapshot: unexpected ValueError" in failed.err
    for captured in (ok, failed):
        assert marker not in captured.out and marker not in captured.err


def test_the_websockets_logger_stays_quiet(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with serving() as (port, _):
        assert snapshot(port, tmp_path / "screen.png") == 0
    assert not [r for r in caplog.records if r.name.startswith("websockets") and r.levelno < logging.WARNING]


def test_the_websockets_logger_would_talk_at_debug(caplog: pytest.LogCaptureFixture) -> None:  # control
    caplog.set_level(logging.DEBUG)
    with serving() as (port, _), page_socket(port):
        pass
    assert [r for r in caplog.records if r.name.startswith("websockets") and r.levelno < logging.WARNING]


# ---- the page's export ---------------------------------------------------------------------------


# SHA-256 of the RGBA pixels of the page's own Save PNG of the replay test card (frame 1, Vertical,
# Copy menu off), taken with the snippet in CONTRIBUTING.md, "Checking that the Screen export
# matches". The command writes the same pixels without alpha; the happy-path test ties the two.
PAGE_TEST_CARD_RGBA_SHA256 = "4a0bdf194e6367d3bdf852658a3415dacddd17281f5c809458a768a8919828e4"  # gitleaks:allow


def test_the_painted_test_card_matches_the_page_export() -> None:
    rgba, _, menu = decode(make_test_card(1))
    painted = bytearray(rgba)
    pull.paint_menu(painted, menu)
    assert hashlib.sha256(painted).hexdigest() == PAGE_TEST_CARD_RGBA_SHA256
