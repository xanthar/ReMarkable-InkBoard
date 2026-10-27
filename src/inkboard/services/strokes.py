"""Turn pen input events into strokes.

evdev reports only values that changed, grouped into packets closed by ``SYN_REPORT``.
A stroke runs from the packet that sets ``BTN_TOUCH`` to 1 until the packet that sets it
back to 0. Packets without touch are hover and are ignored.
"""

from dataclasses import dataclass
from typing import Literal

from inkboard.services.events import (
    ABS_PRESSURE,
    ABS_X,
    ABS_Y,
    BTN_TOOL_PEN,
    BTN_TOOL_RUBBER,
    BTN_TOUCH,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    SYN_DROPPED,
    SYN_REPORT,
    InputEvent,
)

Tool = Literal["pen", "rubber"]


@dataclass(frozen=True, slots=True)
class PenPoint:
    """One sample in raw digitizer units."""

    x: int
    y: int
    pressure: int


@dataclass(frozen=True, slots=True)
class StrokeStarted:
    stroke_id: int
    tool: Tool
    point: PenPoint


@dataclass(frozen=True, slots=True)
class StrokePoint:
    stroke_id: int
    point: PenPoint


@dataclass(frozen=True, slots=True)
class StrokeEnded:
    stroke_id: int


StrokeEvent = StrokeStarted | StrokePoint | StrokeEnded


class StrokeBuilder:
    """A state machine over input events; emits stroke events at each ``SYN_REPORT``."""

    def __init__(self, first_stroke_id: int = 1) -> None:
        self._next_id = first_stroke_id
        self._x: int | None = None
        self._y: int | None = None
        self._pressure = 0
        self._tool: Tool = "pen"
        self._touch = False
        self._pending_touch: bool | None = None
        self._moved = False
        self._open_id: int | None = None
        self._dropping = False
        # After SYN_DROPPED the touch state is unknown; pressure decides whether the pen is down.
        self._resume_on_pressure = False
        self._pressure_seen = False

    @property
    def open_stroke(self) -> int | None:
        return self._open_id

    def feed(self, event: InputEvent) -> list[StrokeEvent]:
        if self._dropping:
            if event.type == EV_SYN and event.code == SYN_REPORT:
                self._dropping = False
                self._resume_on_pressure = True
                self._pending_touch = None
                self._moved = False
                self._pressure_seen = False
            return []
        if event.type == EV_SYN:
            if event.code == SYN_DROPPED:
                self._dropping = True
                self._touch = False
                return self.close()
            if event.code == SYN_REPORT:
                return self._commit()
            return []
        if event.type == EV_KEY:
            self._on_key(event)
        elif event.type == EV_ABS:
            self._on_abs(event)
        return []

    def lost(self) -> list[StrokeEvent]:
        """The reader on the tablet was replaced (a drop, a link switch): end an open stroke and
        treat the touch state as unknown, as after ``SYN_DROPPED``. A new reader gets no replay
        of ``BTN_TOUCH``, so a pen lifted during the gap would otherwise draw its hover path."""
        self._touch = False
        self._pending_touch = None
        self._resume_on_pressure = True
        self._pressure_seen = False
        self._moved = False
        return self.close()

    def close(self) -> list[StrokeEvent]:
        """End an open stroke, for example when the source drops."""
        if self._open_id is None:
            return []
        ended = StrokeEnded(self._open_id)
        self._open_id = None
        return [ended]

    def _on_key(self, event: InputEvent) -> None:
        if event.code == BTN_TOUCH:
            self._pending_touch = event.value != 0
            self._resume_on_pressure = False
        elif event.code == BTN_TOOL_PEN and event.value:
            self._tool = "pen"
        elif event.code == BTN_TOOL_RUBBER and event.value:
            self._tool = "rubber"

    def _on_abs(self, event: InputEvent) -> None:
        if event.code == ABS_X:
            self._x = event.value
            self._moved = True
        elif event.code == ABS_Y:
            self._y = event.value
            self._moved = True
        elif event.code == ABS_PRESSURE:
            self._pressure = event.value
            self._pressure_seen = True
            self._moved = True

    def _commit(self) -> list[StrokeEvent]:
        if self._pending_touch is not None:
            self._touch = self._pending_touch
        elif self._resume_on_pressure and self._pressure_seen and self._pressure > 0:
            self._touch = True
            self._resume_on_pressure = False
        moved = self._moved
        self._pending_touch = None
        self._moved = False
        self._pressure_seen = False

        if self._x is None or self._y is None:
            return []
        if self._touch and self._open_id is None:
            stroke_id = self._next_id
            self._next_id += 1
            self._open_id = stroke_id
            return [StrokeStarted(stroke_id, self._tool, PenPoint(self._x, self._y, self._pressure))]
        if self._touch and self._open_id is not None:
            if moved:
                return [StrokePoint(self._open_id, PenPoint(self._x, self._y, self._pressure))]
            return []
        return self.close()
