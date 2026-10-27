import itertools

import pytest

from inkboard.services.events import ABS_PRESSURE, ABS_X, InputEvent
from inkboard.services.geometry import Orientation, to_raw
from inkboard.services.probe import (
    MAX_FIT_ERROR,
    detect_event_size,
    find_pen_device,
    fit_orientation,
    observed_maxima,
)
from inkboard.services.strokes import PenPoint
from tests.helpers import down, encode, move, up

DEVICES = """I: Bus=0000 Vendor=0000 Product=0000 Version=0000
N: Name="30370000.snvs:snvs-powerkey"
H: Handlers=kbd event0

I: Bus=0018 Vendor=056a Product=0000 Version=0036
N: Name="Wacom I2C Digitizer"
H: Handlers=event1

I: Bus=0000 Vendor=0000 Product=0000 Version=0000
N: Name="pt_mt"
H: Handlers=event2
"""


def test_find_pen_device() -> None:
    assert find_pen_device(DEVICES) == "/dev/input/event1"


def test_find_pen_device_missing() -> None:
    assert find_pen_device(DEVICES.replace("Wacom", "Other")) is None


def _stream() -> list[InputEvent]:
    events = down(100, 200, 1000) + move(110, 210, 1100) + move(120, 220, 1200) + up()
    return [InputEvent(7000 + i // 4, 1234 * i % 1_000_000, e.type, e.code, e.value) for i, e in enumerate(events)]


@pytest.mark.parametrize("size", [16, 24])
def test_detect_event_size(size: int) -> None:
    assert detect_event_size(encode(_stream(), size)) == size


def test_detect_event_size_needs_enough_frames() -> None:
    assert detect_event_size(encode(_stream()[:3])) is None


def test_detect_event_size_rejects_garbage() -> None:
    assert detect_event_size(bytes(range(256)) * 3) is None


@pytest.mark.parametrize("size", [16, 24])
def test_detect_event_size_drops_a_partial_last_frame(size: int) -> None:
    assert detect_event_size(encode(_stream(), size) + b"\x01\x02\x03\x04\x05") == size


# Taps a little inside each corner, in CORNERS order (top-left, top-right, bottom-right, bottom-left).
NEAR_CORNERS = [(0.04, 0.03), (0.95, 0.05), (0.97, 0.94), (0.05, 0.96)]


def _taps(orientation: Orientation, page_points: list[tuple[float, float]]) -> list[PenPoint]:
    return [PenPoint(*to_raw(u, v, orientation), 1000) for u, v in page_points]


@pytest.mark.parametrize("flags", list(itertools.product([False, True], repeat=3)))
def test_fit_orientation_recovers_each_orientation(flags: tuple[bool, bool, bool]) -> None:
    truth = Orientation(*flags)
    fit = fit_orientation(_taps(truth, NEAR_CORNERS))
    assert fit.orientation == truth
    assert fit.clear
    assert fit.runner_up > MAX_FIT_ERROR


def test_fit_orientation_rejects_taps_out_of_order() -> None:
    # Top-left, top-right, bottom-left, bottom-right: no turn or mirror of the page does that.
    z_order = [NEAR_CORNERS[0], NEAR_CORNERS[1], NEAR_CORNERS[3], NEAR_CORNERS[2]]
    fit = fit_orientation(_taps(Orientation(True, False, True), z_order))
    assert not fit.clear
    assert fit.error > MAX_FIT_ERROR


def test_fit_orientation_needs_one_tap_per_corner() -> None:
    with pytest.raises(ValueError, match="need 4 taps"):
        fit_orientation(_taps(Orientation(), NEAR_CORNERS[:2]))


def test_observed_maxima() -> None:
    maxima = observed_maxima(_stream())
    assert maxima[ABS_X] == 120
    assert maxima[ABS_PRESSURE] == 1200
