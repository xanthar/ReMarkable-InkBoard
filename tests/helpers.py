"""Build synthetic pen-event streams the way the rM2 writes them."""

from inkboard.services.events import (
    ABS_DISTANCE,
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
    encode_event,
)


def ev(ev_type: int, code: int, value: int, sec: int = 0, usec: int = 0) -> InputEvent:
    return InputEvent(sec, usec, ev_type, code, value)


def syn() -> InputEvent:
    return ev(EV_SYN, SYN_REPORT, 0)


def dropped() -> InputEvent:
    return ev(EV_SYN, SYN_DROPPED, 0)


def pen_in(tool: str = "pen") -> list[InputEvent]:
    code = BTN_TOOL_RUBBER if tool == "rubber" else BTN_TOOL_PEN
    return [ev(EV_KEY, code, 1), ev(EV_ABS, ABS_DISTANCE, 40), syn()]


def hover(x: int, y: int) -> list[InputEvent]:
    return [ev(EV_ABS, ABS_X, x), ev(EV_ABS, ABS_Y, y), ev(EV_ABS, ABS_DISTANCE, 20), syn()]


def down(x: int, y: int, pressure: int) -> list[InputEvent]:
    return [
        ev(EV_KEY, BTN_TOUCH, 1),
        ev(EV_ABS, ABS_X, x),
        ev(EV_ABS, ABS_Y, y),
        ev(EV_ABS, ABS_PRESSURE, pressure),
        ev(EV_ABS, ABS_DISTANCE, 0),
        syn(),
    ]


def move(x: int, y: int, pressure: int) -> list[InputEvent]:
    return [ev(EV_ABS, ABS_X, x), ev(EV_ABS, ABS_Y, y), ev(EV_ABS, ABS_PRESSURE, pressure), syn()]


def up() -> list[InputEvent]:
    return [ev(EV_KEY, BTN_TOUCH, 0), ev(EV_ABS, ABS_PRESSURE, 0), ev(EV_ABS, ABS_DISTANCE, 10), syn()]


def encode(events: list[InputEvent], event_size: int = 16) -> bytes:
    return b"".join(encode_event(event, event_size) for event in events)
