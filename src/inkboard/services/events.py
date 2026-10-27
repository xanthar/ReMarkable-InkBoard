"""Decode raw Linux ``input_event`` frames read from the tablet's pen device.

The reMarkable 2 is a 32-bit ARM device, so one frame is 16 bytes: ``struct timeval``
(two u32) + type (u16) + code (u16) + value (s32), little-endian. A kernel built with
64-bit time fields would use 24-byte frames; that size is supported so the probe on the
device decides, not a code change.
"""

import struct
from dataclasses import dataclass

# Event types (linux/input-event-codes.h)
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
EV_MSC = 0x04

# EV_SYN codes
SYN_REPORT = 0
SYN_DROPPED = 3

# EV_ABS codes
ABS_X = 0x00
ABS_Y = 0x01
ABS_PRESSURE = 0x18
ABS_DISTANCE = 0x19
ABS_TILT_X = 0x1A
ABS_TILT_Y = 0x1B

# EV_KEY codes
BTN_TOOL_PEN = 0x140
BTN_TOOL_RUBBER = 0x141
BTN_TOUCH = 0x14A

# Plausibility check per type: the exclusive upper bound of codes that type can carry
# (SYN_* < 4, KEY_*/BTN_* < 0x300, ABS_* < 0x40, MSC_* < 8). A frame outside these
# bounds means the stream is not split into frames the way the settings say.
CODE_LIMITS: dict[int, int] = {EV_SYN: 4, EV_KEY: 0x300, EV_ABS: 0x40, EV_MSC: 8}

FRAME_FORMATS: dict[int, struct.Struct] = {
    16: struct.Struct("<IIHHi"),
    24: struct.Struct("<qqHHi"),
}


class MisalignedStreamError(ValueError):
    """A frame carried an event type the pen device never produces.

    A fresh ``dd`` always starts on a frame boundary, so this means the configured frame
    size or device is wrong. Retrying with the same settings cannot fix it.
    """


@dataclass(frozen=True, slots=True)
class InputEvent:
    """One decoded ``input_event``."""

    sec: int
    usec: int
    type: int
    code: int
    value: int

    @property
    def timestamp(self) -> float:
        return self.sec + self.usec / 1_000_000


def frame_format(event_size: int) -> struct.Struct:
    """Return the struct layout for a frame size, or raise for an unsupported size."""
    try:
        return FRAME_FORMATS[event_size]
    except KeyError:
        raise ValueError(f"unsupported event size {event_size}; expected one of {sorted(FRAME_FORMATS)}") from None


def encode_event(event: InputEvent, event_size: int = 16) -> bytes:
    """Encode one event as the device would write it (used by the demo generator and tests)."""
    return frame_format(event_size).pack(event.sec, event.usec, event.type, event.code, event.value)


class EventDecoder:
    """Turn an arbitrary byte stream into whole events, buffering partial frames.

    Pipe reads are not guaranteed to align with frame boundaries, so bytes that do not yet
    form a whole frame are kept for the next ``feed``. One decoder serves one connection.
    """

    def __init__(self, event_size: int = 16) -> None:
        self._format = frame_format(event_size)
        self._size = event_size
        self._buffer = bytearray()

    @property
    def pending(self) -> int:
        """Bytes held back because they do not yet form a whole frame."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[InputEvent]:
        self._buffer.extend(data)
        whole = len(self._buffer) // self._size
        events: list[InputEvent] = []
        for index in range(whole):
            sec, usec, ev_type, code, value = self._format.unpack_from(self._buffer, index * self._size)
            limit = CODE_LIMITS.get(ev_type)
            if limit is None or code >= limit:
                raise MisalignedStreamError(
                    f"implausible event type {ev_type:#x} code {code:#x} at frame {index}; "
                    f"frame size {self._size} or device is wrong"
                )
            events.append(InputEvent(sec, usec, ev_type, code, value))
        del self._buffer[: whole * self._size]
        return events
