import struct

import pytest

from inkboard.services.events import (
    ABS_X,
    EV_ABS,
    EventDecoder,
    InputEvent,
    MisalignedStreamError,
    encode_event,
    frame_format,
)
from tests.helpers import down, encode, move, up


def test_16_byte_frame_matches_the_device_layout() -> None:
    raw = struct.pack("<IIHHi", 12, 345678, EV_ABS, ABS_X, 20000)
    assert len(raw) == 16
    (event,) = EventDecoder(16).feed(raw)
    assert event == InputEvent(12, 345678, EV_ABS, ABS_X, 20000)
    assert event.timestamp == pytest.approx(12.345678)


def test_negative_values_decode_signed() -> None:
    raw = struct.pack("<IIHHi", 0, 0, EV_ABS, 0x1A, -6400)
    assert EventDecoder().feed(raw)[0].value == -6400


@pytest.mark.parametrize("size", [16, 24])
def test_round_trip_for_each_frame_size(size: int) -> None:
    events = down(100, 200, 300) + move(110, 210, 310) + up()
    assert EventDecoder(size).feed(encode(events, size)) == events


@pytest.mark.parametrize("size", [16, 24])
def test_partial_chunks_split_at_every_offset(size: int) -> None:
    events = down(100, 200, 300) + move(110, 210, 310) + up()
    raw = encode(events, size)
    for cut in range(len(raw) + 1):
        decoder = EventDecoder(size)
        decoded = decoder.feed(raw[:cut]) + decoder.feed(raw[cut:])
        assert decoded == events
        assert decoder.pending == 0


def test_byte_by_byte_feed_holds_back_partial_frames() -> None:
    raw = encode(down(1, 2, 3))
    decoder = EventDecoder()
    out = []
    for i in range(len(raw)):
        out += decoder.feed(raw[i : i + 1])
        assert decoder.pending == (i + 1) % 16
    assert len(out) == len(down(1, 2, 3))


def _timestamped(events: list[InputEvent]) -> list[InputEvent]:
    # Real frames carry non-zero timestamps (the tablet's clock); zeros would hide a misalignment.
    return [InputEvent(5123 + i, 100_000 + 997 * i, e.type, e.code, e.value) for i, e in enumerate(events)]


@pytest.mark.parametrize(("written", "read"), [(24, 16), (16, 24)])
def test_wrong_frame_size_is_reported_as_misaligned(written: int, read: int) -> None:
    raw = encode(_timestamped(down(100, 200, 300) + move(110, 210, 310) + up()), written)
    with pytest.raises(MisalignedStreamError):
        EventDecoder(read).feed(raw)


def test_implausible_code_is_reported() -> None:
    raw = struct.pack("<IIHHi", 1, 2, EV_ABS, 0x99, 0)
    with pytest.raises(MisalignedStreamError, match="implausible"):
        EventDecoder().feed(raw)


def test_unsupported_frame_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported event size"):
        frame_format(20)


def test_encode_event_uses_requested_size() -> None:
    event = InputEvent(1, 2, EV_ABS, ABS_X, 3)
    assert len(encode_event(event, 16)) == 16
    assert len(encode_event(event, 24)) == 24
