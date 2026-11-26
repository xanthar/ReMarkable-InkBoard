"""``inkboard snapshot``: the Screen view as a PNG, pulled from a running ``inkboard serve``.

The command is a local client of the page's own WebSocket (ADR 0004). It asks for the Screen view,
waits for a frame grabbed after it asked, fetches that frame once, leaves the Screen view and
closes the connection; only then does it paint the menu white, turn a landscape frame and write an
RGB PNG to the file the user named. The frame and the strokes that arrive on the way are
handwriting: nothing here logs, and the only output is the line the command prints.
"""

import contextlib
import http.client
import json
import os
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.sync.client import ClientConnection, connect
from websockets.typing import Origin

from inkboard.services.screen import FRAME_BYTES, FRAME_H, FRAME_W, Rect

# Exit codes: the contract a script or an agent reads (README, "inkboard snapshot for scripts").
OK, UNEXPECTED, USAGE, UNREACHABLE, NOT_CONNECTED, GRAB_FAILED, TIMED_OUT = range(7)

OPEN_TIMEOUT_S = 5.0
CLOSE_TIMEOUT_S = 2.0
FRAME_PATH = "/api/v1/screen/frame"
WHITE = b"\xff\xff\xff\xff"  # the page's export paper (--export-paper), opaque
NOT_CONNECTED_KINDS = frozenset({"no_link", "timeout"})  # ScreenError kinds that mean "wake the tablet"
LINKS = ("usb", "wifi")
UNREADABLE = "the frame could not be read"

Turn = Literal["auto", "none", "cw"]


class PullError(Exception):
    """A failed snapshot: ``code`` is the exit code, ``str(error)`` the line after ``snapshot:``."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Pulled:
    rgba: bytes = field(repr=False)  # never in a repr, a log line or a traceback
    n: int
    at: datetime
    layout: str
    menu: tuple[Rect, ...]
    source: str


# ---- pulling one frame from serve ----------------------------------------------------------------


def pull_frame(port: int, timeout_s: float) -> Pulled:
    """A frame grabbed after this call started, or PullError. One deadline bounds the whole call."""
    deadline = time.monotonic() + timeout_s

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    origin = Origin(f"http://127.0.0.1:{port}")
    try:
        with connect(
            f"ws://127.0.0.1:{port}/api/v1/ws",
            origin=origin,
            max_size=None,  # the first message is the whole stroke history, which can pass 1 MiB
            proxy=None,
            open_timeout=min(OPEN_TIMEOUT_S, left()),
            close_timeout=min(CLOSE_TIMEOUT_S, left()),
        ) as ws:
            n, source = await_frame(ws, left)
            pulled = fetch_frame(port, n, source, left())
            with contextlib.suppress(ConnectionClosed):  # the frame is in hand either way
                ws.send(json.dumps({"type": "screen", "action": "off"}))
    except TimeoutError:  # before OSError: TimeoutError is one
        raise PullError(TIMED_OUT, f"no frame within {timeout_s:g} s") from None
    except ConnectionClosed as closed:
        code = closed.rcvd.code if closed.rcvd is not None else "no close code"
        raise PullError(UNREACHABLE, f"inkboard serve closed the connection ({code}) - run snapshot again") from None
    except (InvalidHandshake, InvalidURI, OSError):
        raise PullError(
            UNREACHABLE, f"cannot reach inkboard serve on 127.0.0.1:{port} - start it with: uv run inkboard serve"
        ) from None
    return pulled


def await_frame(ws: ClientConnection, left: Callable[[], float]) -> tuple[int, str]:
    """Ask for the Screen view and wait for a fresh ``ready``: the frame number and its link.

    A ``ready`` or ``error`` counts only after an ``updating`` that reached this client, i.e. a
    grab that started after it connected. One without (a grab already running when it asked) is
    answered with a single ``refresh``. The link is the last one any status named during the run;
    a run that never saw one is a replay, whose Screen view is the test card.
    """
    ws.send(json.dumps({"type": "screen", "action": "on"}))
    link: str | None = None
    fresh = refreshed = False
    while True:
        message = parse(ws.recv(timeout=left()))
        if message.get("type") == "status":
            if message.get("link") in LINKS:
                link = message["link"]
            continue
        if message.get("type") != "screen":
            continue  # strokes, snapshot, cleared: not ours to read
        state = message.get("state")
        if state == "updating":
            fresh = True
        elif state in ("ready", "error") and not fresh:
            if not refreshed:
                ws.send(json.dumps({"type": "screen", "action": "refresh"}))
                refreshed = True
        elif state == "error":
            raise grab_error(message)
        elif state == "ready":
            n = message.get("n")
            if not isinstance(n, int):
                raise PullError(GRAB_FAILED, UNREADABLE)
            return n, link or "test-card"


def parse(raw: str | bytes) -> dict[str, Any]:
    """A server message as a plain dict, read by key, never validated into a model (a validation
    error would echo its input, which can be handwriting)."""
    if not isinstance(raw, str):
        return {}
    message = json.loads(raw)
    return message if isinstance(message, dict) else {}


def grab_error(message: dict[str, Any]) -> PullError:
    kind, detail = message.get("kind"), str(message.get("detail", ""))
    if kind in NOT_CONNECTED_KINDS:
        return PullError(NOT_CONNECTED, f"tablet not connected - wake it or check the cable or Wi-Fi ({detail})")
    if kind == "ssh":
        return PullError(NOT_CONNECTED, f"tablet not reachable ({detail})")
    return PullError(GRAB_FAILED, f"screen grab failed: {detail}")


def fetch_frame(port: int, n: int, source: str, timeout_s: float) -> Pulled:
    """GET the frame once; it must be at least frame ``n`` and exactly one screen of pixels."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
    try:
        connection.request("GET", FRAME_PATH)
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    headers = response.headers
    if (
        response.status != 200
        or len(body) != FRAME_BYTES
        or headers.get("X-Inkboard-Frame-Size") != f"{FRAME_W}x{FRAME_H}"
    ):
        raise PullError(GRAB_FAILED, UNREADABLE)
    try:
        frame_n = int(headers["X-Inkboard-Frame-N"])
        at = datetime.fromisoformat(headers["X-Inkboard-Frame-At"])
        layout = headers["X-Inkboard-Frame-Layout"]
        menu = parse_menu(headers.get("X-Inkboard-Frame-Menu", ""))
    except (KeyError, TypeError, ValueError):
        raise PullError(GRAB_FAILED, UNREADABLE) from None
    if frame_n < n or not layout:
        raise PullError(GRAB_FAILED, UNREADABLE)
    return Pulled(body, frame_n, at, layout, menu, source)


def parse_menu(header: str) -> tuple[Rect, ...]:
    """``x,y,w,h;…`` in frame pixels; empty for a layout that was not recognised (nothing to paint)."""
    if not header:
        return ()
    rects: list[Rect] = []
    for part in header.split(";"):
        values = tuple(int(value) for value in part.split(","))
        if len(values) != 4:
            raise ValueError("a menu rectangle has four numbers")
        x, y, w, h = values
        if min(values) < 0 or x + w > FRAME_W or y + h > FRAME_H:
            raise ValueError("a menu rectangle lies outside the frame")
        rects.append((x, y, w, h))
    return tuple(rects)


# ---- the image ------------------------------------------------------------------------------------


def paint_menu(rgba: bytearray, menu: tuple[Rect, ...]) -> None:
    """Fill the menu areas with the page's paper white, in the frame's own (portrait) coordinates."""
    for x, y, w, h in menu:
        line = WHITE * w
        for row in range(y, y + h):
            start = (row * FRAME_W + x) * 4
            rgba[start : start + len(line)] = line


def turn_cw(rgba: bytes, width: int, height: int) -> bytes:
    """A quarter turn clockwise, as the page's Horizontal view: pixel (x, y) lands at
    (height - 1 - y, x). Output row x is input column x read bottom-up."""
    pixels = memoryview(rgba).cast("I")
    return b"".join(pixels[x::width][::-1].tobytes() for x in range(width))


def rgb_from_rgba(rgba: bytes) -> bytes:
    """Drop alpha, which is 255 on every pixel of a checked frame."""
    rgb = bytearray(len(rgba) // 4 * 3)
    for channel in range(3):
        rgb[channel::3] = rgba[channel::4]
    return bytes(rgb)


def encode_png(rgb: bytes, width: int, height: int) -> bytes:
    """An 8-bit RGB PNG with only IHDR, IDAT and IEND: no text, time or colour-profile chunks."""
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[row * stride : (row + 1) * stride] for row in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def render(pulled: Pulled, turn: Turn) -> tuple[bytes, str]:
    """The PNG and the turn applied: ``auto`` turns a landscape layout, never an unknown one."""
    rgba = bytearray(pulled.rgba)
    paint_menu(rgba, pulled.menu)
    clockwise = turn == "cw" or (turn == "auto" and pulled.layout.startswith("landscape-"))
    if clockwise:
        return encode_png(rgb_from_rgba(turn_cw(bytes(rgba), FRAME_W, FRAME_H)), FRAME_H, FRAME_W), "cw"
    return encode_png(rgb_from_rgba(bytes(rgba)), FRAME_W, FRAME_H), "none"


# ---- the file -------------------------------------------------------------------------------------


def inside_git_work_tree(path: Path) -> Path | None:
    """The repository root that ``path`` would land in, or None. A screen image is handwriting, so
    it must never sit where a ``git add`` could pick it up."""
    for folder in path.parents:
        if (folder / ".git").exists():
            return folder
    return None


def write_atomic(out: Path, data: bytes) -> None:
    """Write beside ``out`` (owner-only), then replace it in one step; no partial file is left."""
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(temp, out)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def snapshot(port: int, out: Path, timeout_s: float, turn: Turn) -> str:
    """Pull, render and write; the line to print. Raises PullError."""
    pulled = pull_frame(port, timeout_s)
    png, turned = render(pulled, turn)
    try:
        write_atomic(out, png)
    except OSError as exc:
        raise PullError(UNEXPECTED, f"could not write {out}: {exc.strerror or type(exc).__name__}") from None
    at = pulled.at.astimezone().isoformat(timespec="seconds")
    return f"saved n={pulled.n} at={at} layout={pulled.layout} turn={turned} source={pulled.source} file={out}"
