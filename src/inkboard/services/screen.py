"""The Screen view: the tablet's own screen image, read over SSH.

xochitl, reMarkable's app, keeps the screen image in its own memory: on firmware 3.24 and
later a 1404 x 1872 BGRA frame in the anonymous map that follows the ``/dev/fb0`` map in
``/proc/<pid>/maps``, at a fixed offset found by measurement (see ADR 0002 and
docs/operations/device-setup.md, D7). One grab is one SSH run that

- checks the firmware on the tablet first and exits 5 on a version that was never measured, so
  no memory is read there;
- copies one frame with busybox ``dd`` and compresses it with ``gzip -1`` (a blank page is a few
  kilobytes instead of 10.5 MB).

The tablet is only read. Every frame is checked before it is used: exact size, and alpha 255 on
every pixel, which a moved offset breaks. A frame is handwriting-grade: it is never logged and
never written to disk here.

ScreenService decides when to grab: when a page shows the Screen view, ``delay_s`` after the pen
lifts (never while it is down), and at once on Refresh, on subscribe and after the pen source
reconnects. One grab runs at a time.
"""

import asyncio
import contextlib
import re
import subprocess  # nosec B404 - runs the user's own ssh client with a fixed argv
import threading
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import IPv4Address
from typing import Any, Literal, Protocol

import structlog

from inkboard.core.config import Settings
from inkboard.services.sources import NEW_SESSION, SPAWN_FLAGS, _terminate, ssh_base_argv

log = structlog.get_logger(__name__)

FRAME_W, FRAME_H = 1404, 1872
FRAME_PIXELS = FRAME_W * FRAME_H
FRAME_BYTES = FRAME_PIXELS * 4  # BGRA
# Measured on firmware 3.24.0.149 and 3.29.0.153 (alpha 255 on every pixel from here).
FRAME_OFFSET = 2_629_640
SUPPORTED_FIRMWARE = ("3.24", "3.29")
GRAB_DELAY_S = 0.5
GRAB_TIMEOUT_S = 10.0
FIRMWARE_LINE = re.compile(r"^REMARKABLE_RELEASE_VERSION=(\S+)$")

Rect = tuple[int, int, int, int]  # x, y, width, height in portrait frame pixels


def remote_command(offset: int = FRAME_OFFSET, size: int = FRAME_BYTES) -> str:
    """The shell text run on the tablet. Fixed text plus two integers, no double quotes,
    backslashes, percent signs or newlines, so Windows argv quoting cannot change it."""
    versions = "|".join(v.replace(".", "[.]") for v in SUPPORTED_FIRMWARE)
    return (
        "grep '^REMARKABLE_RELEASE_VERSION=' /usr/share/remarkable/update.conf >&2; "
        f"grep -qE '^REMARKABLE_RELEASE_VERSION=({versions})[.]' /usr/share/remarkable/update.conf || exit 5; "
        "set -- $(pidof xochitl); pid=$1; [ ${#pid} -gt 0 ] || exit 3; "
        "a=$(grep -A1 /dev/fb0 /proc/$pid/maps | tail -n1 | cut -d- -f1); [ ${#a} -gt 0 ] || exit 4; "
        f"{{ dd bs=1 skip=$((0x$a + {int(offset)})) count=0 && dd bs={int(size)} count=1; }} "
        "< /proc/$pid/mem 2>/dev/null | gzip -1"
    )


def screen_argv(settings: Settings, address: IPv4Address | None = None) -> list[str]:
    return [*ssh_base_argv(settings, address), remote_command()]


class ScreenError(Exception):
    """A failed grab; ``str(error)`` is the text the page shows, ``kind`` goes to the log."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# ---- reading a frame -----------------------------------------------------------------------


class Grabber(Protocol):
    def grab(self) -> bytes: ...

    def close(self) -> None: ...


class SshGrabber:
    """One ssh run per grab; returns one BGRA frame or raises ScreenError.

    With ``argv_for`` every grab asks for the argv of the link the pen stream is on (None while it
    is not connected); otherwise the fixed ``argv`` is run.
    """

    def __init__(
        self,
        argv: Sequence[str],
        timeout_s: float = GRAB_TIMEOUT_S,
        *,
        argv_for: Callable[[], Sequence[str] | None] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._argv_for = argv_for
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = False

    def grab(self) -> bytes:
        argv = self._argv
        if self._argv_for is not None:
            chosen = self._argv_for()
            if chosen is None:
                raise ScreenError("no_link", "the tablet is not connected")
            argv = list(chosen)
        with self._lock:
            if self._closed:
                raise ScreenError("closed", "inkboard is stopping")
            try:
                process = subprocess.Popen(  # nosec B603 - argv list, no shell
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=SPAWN_FLAGS,
                    start_new_session=NEW_SESSION,
                )
            except OSError as exc:
                raise ScreenError("ssh_missing", f"could not start ssh: {exc}") from None
            self._process = process
        try:
            wire, err = process.communicate(timeout=self._timeout_s)
        except subprocess.TimeoutExpired:
            _terminate(process)
            raise ScreenError("timeout", f"the tablet did not answer within {self._timeout_s:g} s") from None
        finally:
            with self._lock:
                self._process = None
        return unpack(process.returncode, wire, err.decode("utf-8", errors="replace"))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
        if process is not None:
            _terminate(process)


def unpack(returncode: int, wire: bytes, stderr: str) -> bytes:
    """Map an ssh run's outcome to a BGRA frame or a ScreenError (most specific cause first)."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    firmware = next((m.group(1) for line in lines if (m := FIRMWARE_LINE.match(line))), None)
    if returncode == 255:
        other = [line for line in lines if not FIRMWARE_LINE.match(line)]
        raise ScreenError("ssh", other[-1] if other else "ssh failed")
    if returncode == 5:
        supported = " and ".join(SUPPORTED_FIRMWARE)
        raise ScreenError("firmware", f"Screen view supports firmware {supported}; the tablet has {firmware or '?'}")
    if returncode == 3:
        raise ScreenError("no_xochitl", "the tablet app is not running (restarting?)")
    if returncode == 4:
        raise ScreenError("no_map", "screen memory not found")
    if returncode != 0:
        raise ScreenError("read", "screen memory could not be read")
    try:
        inflater = zlib.decompressobj(wbits=31)
        frame = inflater.decompress(wire, FRAME_BYTES + 1)
    except zlib.error:
        raise ScreenError("read", "screen memory could not be read") from None
    if not frame:
        raise ScreenError("read", "screen memory could not be read")
    if len(frame) != FRAME_BYTES or inflater.unconsumed_tail:
        raise ScreenError("size", f"unexpected frame size ({len(frame)} bytes)")
    return frame


# ---- checking and decoding a frame -------------------------------------------------------------

# 40 x 40 one-bit masks of the tablet's own UI icons (grey < 128), measured on firmware 3.29.0.153;
# row-major, most significant bit first. Not handwriting: buttons only.
ICON = 40
_MASK_HEX = {
    "close": (
        "00000000000003ffc000000ffff000003ffffc0000fe007f0001f0000f8003e00007c007800001e00f000000f00e00000070"
        "1e000000781c00000038380600601c380f00f01c780f81f01e7007c3e00e7003e7c00e7001ff800e7000ff000e70007e000e"
        "70007e000e7000ff000e7001ff800e7003e7c00e7007c3e00e780f81f01e380f00f01c380600601c1c000000381e00000078"
        "0e000000700f000000f007800001e003e00007c001f0000f8000fe007f00003ffffc00000ffff0000003ffc0000000000000"
    ),
    "toggle-portrait-open": (
        "00000000000003ffc000000ffff000003ffffc0000fe007f0001f0000f8003e00007c007800001e00f000000f00e00000070"
        "1e070000781c1fc00038381fc0001c383fe0001c783fe0001e703fe0000e701fc0000e701fc0000e700700000e700000000e"
        "700000000e700000000e700000000e700000000e700000000e780000001e380000001c380000001c1c000000381e00000078"
        "0e000000700f000000f007800001e003e00007c001f0000f8000fe007f00003ffffc00000ffff0000003ffc0000000000000"
    ),
    "toggle-portrait-closed": (
        "00000000000003ffc000000ffff000003ffffc0000fe007f0001f0000f8003e00007c007800001e00f000000f00e00000070"
        "1e000000781c00000038380000001c380000001c780000001e700000000e700000000e700000000e700000000e700000000e"
        "700000000e700000e00e700003f80e700003f80e700007fc0e780007fc1e380007fc1c380003f81c1c0003f8381e0000e078"
        "0e000000700f000000f007800001e003e00007c001f0000f8000fe007f00003ffffc00000ffff0000003ffc0000000000000"
    ),
    "toggle-landscape-open": (
        "00000000000003ffc000000ffff000003ffffc0000fe007f0001f0000f8003e00007c007800001e00f000000f00e00000070"
        "1e000000781c00000038380000001c380000001c780000001e700000000e700000000e700000000e700000000e700000000e"
        "700000000e700700000e701fc0000e701fc0000e703fe0000e783fe0001e383fe0001c381fc0001c1c1fc000381e07000078"
        "0e000000700f000000f007800001e003e00007c001f0000f8000fe007f00003ffffc00000ffff0000003ffc0000000000000"
    ),
    "toggle-landscape-closed": (
        "00000000000003ffc000000ffff000003ffffc0000fe007f0001f0000f8003e00007c007800001e00f000000f00e00000070"
        "1e0000e0781c0003f838380003f81c380007fc1c780007fc1e700007fc0e700003f80e700003f80e700000e00e700000000e"
        "700000000e700000000e700000000e700000000e700000000e780000001e380000001c380000001c1c000000381e00000078"
        "0e000000700f000000f007800001e003e00007c001f0000f8000fe007f00003ffffc00000ffff0000003ffc0000000000000"
    ),
}
MASKS = {name: bytes.fromhex(value) for name, value in _MASK_HEX.items()}
EMPTY = bytes(ICON * ICON // 8)


@dataclass(frozen=True, slots=True)
class Layout:
    """A tablet UI state, told apart by icons at fixed boxes; ``menu`` is what Copy menu leaves out."""

    name: str
    icons: tuple[tuple[int, int, str], ...]  # x, y of a 40 x 40 box, and the mask expected there
    empty: tuple[tuple[int, int], ...]  # boxes that must show nothing
    menu: tuple[Rect, ...]


# Portrait-frame coordinates. In landscape mode xochitl draws its UI turned inside the same
# portrait frame: toolbar along the bottom, close button top left, toggle bottom left.
LAYOUTS = (
    Layout(
        "portrait-open",
        icons=((1328, 36, "close"), (36, 36, "toggle-portrait-open")),
        empty=(),
        menu=((0, 0, 116, FRAME_H), (1320, 28, 56, 56)),
    ),
    Layout(
        "landscape-open",
        icons=((36, 36, "close"), (36, 1796, "toggle-landscape-open")),
        empty=(),
        menu=((0, 1756, FRAME_W, FRAME_H - 1756), (28, 28, 56, 56)),
    ),
    Layout(
        "portrait-closed",
        icons=((36, 36, "toggle-portrait-closed"),),
        empty=((1328, 36),),
        menu=((28, 28, 56, 56),),
    ),
    Layout(
        "landscape-closed",
        icons=((36, 1796, "toggle-landscape-closed"),),
        empty=((36, 36),),
        menu=((28, 1788, 56, 56),),
    ),
)


def icon_mask(bgra: bytes, x0: int, y0: int) -> bytes:
    """The one-bit mask of a 40 x 40 box, grey as Pillow's "L" conversion computes it."""
    bits = 0
    for y in range(y0, y0 + ICON):
        row = (y * FRAME_W + x0) * 4
        for i in range(row, row + ICON * 4, 4):
            b, g, r = bgra[i], bgra[i + 1], bgra[i + 2]
            grey = (r * 19595 + g * 38470 + b * 7471 + 0x8000) >> 16
            bits = (bits << 1) | (grey < 128)
    return bits.to_bytes(ICON * ICON // 8, "big")


def detect_layout(bgra: bytes) -> Layout | None:
    """The layout whose icons all match exactly, or None (left-handed, a pop-up, the library…)."""
    for layout in LAYOUTS:
        if all(icon_mask(bgra, x, y) == MASKS[name] for x, y, name in layout.icons) and all(
            icon_mask(bgra, x, y) == EMPTY for x, y in layout.empty
        ):
            return layout
    return None


@dataclass(frozen=True, slots=True)
class Frame:
    n: int
    at: datetime
    rgba: bytes = field(repr=False)  # never in a repr, a log line or a traceback
    layout: str
    menu: tuple[Rect, ...]


def decode(bgra: bytes) -> tuple[bytes, str, tuple[Rect, ...]]:
    """Check a frame, turn BGRA into RGBA and find its layout."""
    if len(bgra) != FRAME_BYTES:
        raise ScreenError("size", f"unexpected frame size ({len(bgra)} bytes)")
    if bgra[3::4].count(255) != FRAME_PIXELS:
        raise ScreenError(
            "not_image",
            "the frame does not look like a screen image - re-measure (see docs/operations/device-setup.md, D7)",
        )
    rgba = bytearray(bgra)
    rgba[0::4] = bgra[2::4]
    rgba[2::4] = bgra[0::4]
    layout = detect_layout(bgra)
    return bytes(rgba), layout.name if layout else "unknown", layout.menu if layout else ()


# ---- the offline test card ---------------------------------------------------------------------


class TestCardGrabber:
    """Replay mode has no tablet: a synthetic frame in the portrait-open layout, with an arrow
    pointing up for orientation and a bar that grows with each grab. Never real screen data."""

    __test__ = False  # not a pytest class

    ARROW = (702, 300)  # tip of the arrow, frame pixels
    BAR_Y = 1500

    def __init__(self) -> None:
        self.count = 0

    def grab(self) -> bytes:
        self.count += 1
        return make_test_card(self.count)

    def close(self) -> None:
        pass


def make_test_card(n: int) -> bytes:
    frame = bytearray(b"\xff" * FRAME_BYTES)

    def fill(x: int, y: int, w: int, h: int, value: int = 0) -> None:
        line = bytes((value, value, value, 255)) * w
        for row in range(y, y + h):
            start = (row * FRAME_W + x) * 4
            frame[start : start + len(line)] = line

    def stamp(x0: int, y0: int, mask: bytes) -> None:
        bits = int.from_bytes(mask, "big")
        for i in range(ICON * ICON):
            if bits >> (ICON * ICON - 1 - i) & 1:
                fill(x0 + i % ICON, y0 + i // ICON, 1, 1)

    fill(0, 112, 112, 112)  # the selected tool's box in the toolbar strip
    stamp(36, 36, MASKS["toggle-portrait-open"])
    stamp(1328, 36, MASKS["close"])
    tip_x, tip_y = TestCardGrabber.ARROW
    for row in range(120):  # a solid triangle pointing up
        fill(tip_x - row // 2, tip_y + row, row + 1, 1)
    fill(tip_x - 10, tip_y + 120, 20, 300)  # the shaft
    fill(300, TestCardGrabber.BAR_Y, min(800, 40 * n), 40, 96)  # grows with every grab
    return bytes(frame)


# ---- when to grab ------------------------------------------------------------------------------

Publish = Callable[[dict[str, Any]], None]
Urgency = Literal["now", "idle"]


class ScreenService:
    """Runs on the asyncio loop; the blocking grab runs in a worker thread."""

    def __init__(self, grabber: Grabber, publish: Publish, delay_s: float = GRAB_DELAY_S) -> None:
        self._grabber = grabber
        self._publish = publish
        self._delay_s = delay_s
        self._subscribers: set[object] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._again: Urgency | None = None
        self._pen_down = False
        self._closed = False
        self._n = 0
        self.latest: Frame | None = None

    # -- lifecycle --------------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def close(self) -> None:
        self._closed = True
        self._cancel_timer()
        self._grabber.close()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # -- inputs -----------------------------------------------------------------------------

    def subscribe(self, client: object) -> None:
        self._subscribers.add(client)
        if self._task is None or self._task.done():  # a grab already running reaches this page too
            self._want("now")

    def unsubscribe(self, client: object) -> None:
        self._subscribers.discard(client)
        if not self._subscribers:
            self._cancel_timer()
            self._again = None
            self.latest = None  # nobody looks: keep no copy of the screen

    def refresh(self, client: object) -> None:
        if client in self._subscribers:
            self._want("now")

    def pen(self, down: bool) -> None:
        self._pen_down = down
        if down:
            self._cancel_timer()
        elif self._subscribers and self._loop is not None and not self._closed:
            self._cancel_timer()
            self._timer = self._loop.call_later(self._delay_s, self._timer_fired)

    def source_connected(self) -> None:
        self._want("now")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- internals --------------------------------------------------------------------------

    def _timer_fired(self) -> None:
        self._timer = None
        self._want("idle")

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _want(self, urgency: Urgency) -> None:
        if not self._subscribers or self._loop is None or self._closed:
            return
        if self._task is not None and not self._task.done():
            self._again = "now" if urgency == "now" or self._again == "now" else "idle"
            return
        self._task = self._loop.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await self._grab_once()
            again, self._again = self._again, None
            if self._closed or not self._subscribers or again is None:
                return
            if again == "idle" and self._pen_down:
                return  # the next pen-up arms the timer again

    async def _grab_once(self) -> None:
        self._send({"type": "screen", "state": "updating"})
        started = time.monotonic()
        try:
            bgra = await asyncio.to_thread(self._grabber.grab)
            rgba, layout, menu = await asyncio.to_thread(decode, bgra)
        except ScreenError as error:
            log.info("screen.grab", ms=round((time.monotonic() - started) * 1000), result=error.kind)
            if error.kind != "closed":
                self._send({"type": "screen", "state": "error", "detail": str(error)})
            return
        ms = round((time.monotonic() - started) * 1000)
        log.info("screen.grab", ms=ms, result="ok", layout=layout)
        if not self._subscribers or self._closed:
            return
        self._n += 1
        self.latest = Frame(self._n, datetime.now(UTC), rgba, layout, menu)
        self._send({"type": "screen", "state": "ready", "n": self._n, "at": self.latest.at.isoformat()})

    def _send(self, message: dict[str, Any]) -> None:
        if self._subscribers:
            self._publish(message)
