"""In-memory stand-ins for app tests: a pen source tests push events into, a screen grabber, and
synthetic screen frames."""

import threading
from collections.abc import Callable

from inkboard.services.events import InputEvent
from inkboard.services.screen import FRAME_BYTES, FRAME_W, ICON, MASKS, ScreenError, make_test_card
from inkboard.services.sources import EventsCallback, SourceStatus, StatusCallback

CONNECTED = SourceStatus("connected")
WHITE = b"\xff" * FRAME_BYTES
BLACK_PIXEL = b"\x00\x00\x00\xff"  # BGRA


def stamp(frame: bytearray, x0: int, y0: int, mask: bytes) -> None:
    bits = int.from_bytes(mask, "big")
    for i in range(ICON * ICON):
        if bits >> (ICON * ICON - 1 - i) & 1:
            start = ((y0 + i // ICON) * FRAME_W + x0 + i % ICON) * 4
            frame[start : start + 4] = BLACK_PIXEL


def frame_with(*icons: tuple[int, int, str], paper: bytes = WHITE) -> bytes:
    """A BGRA frame of ``paper`` with the tablet's UI icons stamped in black at the given boxes."""
    frame = bytearray(paper)
    for x, y, name in icons:
        stamp(frame, x, y, MASKS[name])
    return bytes(frame)


class FakeSource:
    def __init__(self, initial: SourceStatus | None = CONNECTED) -> None:
        self._initial = initial
        self._on_events: EventsCallback | None = None
        self._on_status: StatusCallback | None = None
        self.started = threading.Event()
        self.stopped = threading.Event()

    def start(self, on_events: EventsCallback, on_status: StatusCallback) -> None:
        self._on_events, self._on_status = on_events, on_status
        if self._initial is not None:
            on_status(self._initial)
        self.started.set()

    def stop(self) -> None:
        self.stopped.set()

    def push(self, events: list[InputEvent]) -> None:
        assert self._on_events is not None
        self._on_events(events)

    def status(self, status: SourceStatus) -> None:
        assert self._on_status is not None
        self._on_status(status)


class FakeGrabber:
    """Returns synthetic test cards (or ``frames(n)``); ``gate`` holds a grab until set; ``error``
    makes grabs fail."""

    def __init__(
        self,
        error: ScreenError | None = None,
        gate: threading.Event | None = None,
        frames: Callable[[int], bytes] = make_test_card,
    ) -> None:
        self.error = error
        self.gate = gate
        self.frames = frames
        self.calls = 0
        self.started = threading.Event()
        self.closed = threading.Event()

    def grab(self) -> bytes:
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.closed.is_set():
            raise ScreenError("closed", "inkboard is stopping")
        if self.error is not None:
            raise self.error
        return self.frames(self.calls)

    def close(self) -> None:
        self.closed.set()
        if self.gate is not None:
            self.gate.set()
