"""In-memory stand-ins for app tests: a pen source tests push events into, and a screen grabber."""

import threading

from inkboard.services.events import InputEvent
from inkboard.services.screen import ScreenError, make_test_card
from inkboard.services.sources import EventsCallback, SourceStatus, StatusCallback

CONNECTED = SourceStatus("connected")


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
    """Returns synthetic test cards; ``gate`` holds a grab until set; ``error`` makes grabs fail."""

    def __init__(self, error: ScreenError | None = None, gate: threading.Event | None = None) -> None:
        self.error = error
        self.gate = gate
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
        return make_test_card(self.calls)

    def close(self) -> None:
        self.closed.set()
        if self.gate is not None:
            self.gate.set()
