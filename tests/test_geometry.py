import itertools

import pytest

from inkboard.services.geometry import Orientation, PenRanges, to_page

R = PenRanges()
CORNERS = {"origin": (0, 0), "x_max": (R.max_x, 0), "y_max": (0, R.max_y), "both": (R.max_x, R.max_y)}


@pytest.mark.parametrize(("swap", "fx", "fy"), list(itertools.product([False, True], repeat=3)))
def test_all_eight_orientations_map_corners_to_corners(swap: bool, fx: bool, fy: bool) -> None:
    orientation = Orientation(swap, fx, fy)
    seen = set()
    for x, y in CORNERS.values():
        u, v, _ = to_page(x, y, 0, orientation)
        assert (u, v) in {(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)}
        seen.add((u, v))
    assert len(seen) == 4  # a bijection: no two corners collapse


def test_swap_then_flip_order() -> None:
    # raw x_max corner, swapped -> v = 1, then flip_y -> v = 0
    assert to_page(R.max_x, 0, 0, Orientation(swap_xy=True, flip_y=True))[:2] == (0.0, 0.0)
    assert to_page(R.max_x, 0, 0, Orientation(swap_xy=True))[:2] == (0.0, 1.0)


def test_pressure_is_normalised_and_clamped() -> None:
    assert to_page(0, 0, 4095, Orientation())[2] == 1.0
    assert to_page(0, 0, 0, Orientation())[2] == 0.0
    assert to_page(0, 0, 9999, Orientation())[2] == 1.0


def test_out_of_range_coordinates_clamp() -> None:
    assert to_page(R.max_x * 2, -5, 0, Orientation())[:2] == (1.0, 0.0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", Orientation()),
        ("none", Orientation()),
        ("swap_xy", Orientation(swap_xy=True)),
        (" Swap_XY , flip_y ", Orientation(swap_xy=True, flip_y=True)),
        ("flip_x,flip_y", Orientation(flip_x=True, flip_y=True)),
    ],
)
def test_parse(text: str, expected: Orientation) -> None:
    assert Orientation.parse(text) == expected


def test_parse_rejects_unknown_flags() -> None:
    with pytest.raises(ValueError, match="unknown orientation"):
        Orientation.parse("rotate_90")


def test_str_round_trips() -> None:
    for o in (Orientation(), Orientation(True, False, True)):
        assert Orientation.parse(str(o)) == o
