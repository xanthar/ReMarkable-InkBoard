"""Measurements for ``inkboard probe``: which input node, which frame size, which orientation."""

import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass

from inkboard.services.events import (
    EV_ABS,
    EV_SYN,
    FRAME_FORMATS,
    SYN_REPORT,
    EventDecoder,
    InputEvent,
    MisalignedStreamError,
)
from inkboard.services.geometry import RM2_RANGES, Orientation, PenRanges, to_page
from inkboard.services.strokes import PenPoint

MIN_FRAMES = 8

# The corners probe asks for, in tapping order, with where each sits on the portrait page.
CORNERS: tuple[tuple[str, tuple[float, float]], ...] = (
    ("TOP-LEFT", (0.0, 0.0)),
    ("TOP-RIGHT", (1.0, 0.0)),
    ("BOTTOM-RIGHT", (1.0, 1.0)),
    ("BOTTOM-LEFT", (0.0, 1.0)),
)
# Sum of squared distances between the taps and their corners, in page units. Taps a tenth of
# the page in from each corner score 0.08; any wrong orientation puts at least two taps on
# another corner and scores 2 or more. Above this, the taps match no orientation.
MAX_FIT_ERROR = 0.5


@dataclass(frozen=True, slots=True)
class OrientationFit:
    orientation: Orientation
    error: float
    runner_up: float

    @property
    def clear(self) -> bool:
        return self.error <= MAX_FIT_ERROR


def find_pen_device(devices_text: str) -> str | None:
    """Return ``/dev/input/eventN`` of the Wacom digitizer from ``/proc/bus/input/devices``."""
    for block in re.split(r"\n\s*\n", devices_text):
        name = re.search(r'^N: Name="([^"]*)"', block, re.MULTILINE)
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.MULTILINE)
        if name and handlers and "wacom" in name.group(1).lower():
            event = re.search(r"\bevent(\d+)\b", handlers.group(1))
            if event:
                return f"/dev/input/event{event.group(1)}"
    return None


def detect_event_size(raw: bytes) -> int | None:
    """Return the one frame size under which ``raw`` decodes plausibly, or None if not exactly one.

    A capture stopped mid-stream can end inside a frame, so each size drops its partial tail.
    """
    plausible: list[int] = []
    for size in sorted(FRAME_FORMATS):
        whole = raw[: len(raw) - len(raw) % size]
        if len(whole) < size * MIN_FRAMES:
            continue
        try:
            events = EventDecoder(size).feed(whole)
        except MisalignedStreamError:
            continue
        has_report = any(e.type == EV_SYN and e.code == SYN_REPORT for e in events)
        monotonic = all(a.timestamp <= b.timestamp for a, b in itertools.pairwise(events))
        if has_report and monotonic:
            plausible.append(size)
    return plausible[0] if len(plausible) == 1 else None


def fit_orientation(taps: Sequence[PenPoint], ranges: PenRanges = RM2_RANGES) -> OrientationFit:
    """Pick the orientation that puts the taps, made in ``CORNERS`` order, on their corners."""
    if len(taps) != len(CORNERS):
        raise ValueError(f"need {len(CORNERS)} taps, got {len(taps)}")

    def error(orientation: Orientation) -> float:
        total = 0.0
        for tap, (_, (cu, cv)) in zip(taps, CORNERS, strict=True):
            u, v, _ = to_page(tap.x, tap.y, 0, orientation, ranges)
            total += (u - cu) ** 2 + (v - cv) ** 2
        return total

    scored = sorted(
        ((error(orientation), orientation) for orientation in _all_orientations()), key=lambda item: item[0]
    )
    (best_error, best), (runner_up, _) = scored[0], scored[1]
    return OrientationFit(best, best_error, runner_up)


def _all_orientations() -> list[Orientation]:
    return [Orientation(*flags) for flags in itertools.product([False, True], repeat=3)]


def observed_maxima(events: list[InputEvent]) -> dict[int, int]:
    """Largest value seen per ABS code - a sanity check against the known ranges, never new ranges."""
    maxima: dict[int, int] = {}
    for event in events:
        if event.type == EV_ABS:
            maxima[event.code] = max(maxima.get(event.code, event.value), event.value)
    return maxima
