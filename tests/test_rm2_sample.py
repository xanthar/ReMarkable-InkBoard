"""Regression check against a real rM2 recording.

tests/fixtures/rm2-sample.bin was recorded with `inkboard record`, tablet in portrait: a circle, a
square and the word "inkboard", drawn across the middle of the page. Its timestamps start at
1000 s (shifted from the recording's clock; every gap between events is unchanged).
"""

from pathlib import Path

from inkboard.core.config import Settings
from inkboard.services.events import EventDecoder
from inkboard.services.geometry import RM2_RANGES, Orientation, to_page
from inkboard.services.probe import detect_event_size
from inkboard.services.strokes import StrokeBuilder, StrokePoint, StrokeStarted

SAMPLE = Path(__file__).parent / "fixtures" / "rm2-sample.bin"
PAGE_WIDTH, PAGE_HEIGHT = 1404, 1872  # portrait page, in screen pixels


def _strokes() -> list[list[tuple[int, int]]]:
    builder = StrokeBuilder()
    strokes: list[list[tuple[int, int]]] = []
    for event in EventDecoder(16).feed(SAMPLE.read_bytes()):
        for item in builder.feed(event):
            if isinstance(item, StrokeStarted):
                strokes.append([(item.point.x, item.point.y)])
            elif isinstance(item, StrokePoint):
                strokes[-1].append((item.point.x, item.point.y))
    return strokes


def _drawing_size(orientation: Orientation) -> tuple[float, float]:
    """Width and height of everything drawn, in page pixels."""
    pages = [to_page(x, y, 0, orientation) for stroke in _strokes() for x, y in stroke]
    us = [u for u, _, _ in pages]
    vs = [v for _, v, _ in pages]
    return (max(us) - min(us)) * PAGE_WIDTH, (max(vs) - min(vs)) * PAGE_HEIGHT


def test_sample_is_16_byte_frames() -> None:
    assert detect_event_size(SAMPLE.read_bytes()) == 16


def test_sample_decodes_into_the_strokes_drawn() -> None:
    strokes = _strokes()
    assert len(strokes) == 11
    assert all(len(stroke) > 1 for stroke in strokes)
    points = [point for stroke in strokes for point in stroke]
    assert all(0 <= x <= RM2_RANGES.max_x and 0 <= y <= RM2_RANGES.max_y for x, y in points)


def test_default_orientation_shows_the_drawing_upright() -> None:
    # The word runs left to right, so the drawing is wider than tall on the portrait page.
    width, height = _drawing_size(Settings().orientation_value)
    assert width > 1.2 * height


def test_unswapped_axes_would_stand_the_drawing_on_its_side() -> None:
    width, height = _drawing_size(Orientation())
    assert height > 1.2 * width


I_STEM, I_DOT = 2, 3  # strokes in drawing order: circle, square, then the "i" of "inkboard"


def _boxes(orientation: Orientation) -> list[tuple[float, float, float, float]]:
    """(u_min, u_max, v_min, v_max) per stroke on the page."""
    boxes = []
    for stroke in _strokes():
        pages = [to_page(x, y, 0, orientation) for x, y in stroke]
        us = [u for u, _, _ in pages]
        vs = [v for _, v, _ in pages]
        boxes.append((min(us), max(us), min(vs), max(vs)))
    return boxes


def test_default_orientation_puts_the_i_dot_above_its_stem_and_leftmost() -> None:
    boxes = _boxes(Settings().orientation_value)
    dot, stem = boxes[I_DOT], boxes[I_STEM]
    assert dot[3] < stem[2]  # the dot ends above where the stem starts
    assert all(dot[1] < box[0] for i, box in enumerate(boxes) if i != I_DOT)


def test_without_flip_y_the_i_dot_would_hang_below_its_stem() -> None:
    boxes = _boxes(Orientation(swap_xy=True))
    assert boxes[I_DOT][2] > boxes[I_STEM][3]
