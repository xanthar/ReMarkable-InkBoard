from inkboard.services.events import ABS_PRESSURE, BTN_TOUCH, EV_ABS, EV_KEY, InputEvent
from inkboard.services.strokes import (
    PenPoint,
    StrokeBuilder,
    StrokeEnded,
    StrokeEvent,
    StrokePoint,
    StrokeStarted,
)
from tests.helpers import down as pen_down
from tests.helpers import dropped, ev, hover, move, pen_in, syn, up


def run(events: list[InputEvent]) -> list[StrokeEvent]:
    builder = StrokeBuilder()
    out: list[StrokeEvent] = []
    for event in events:
        out += builder.feed(event)
    return out


def test_one_stroke_start_points_end() -> None:
    out = run(pen_in() + pen_down(100, 200, 1000) + move(110, 210, 1200) + move(120, 220, 1400) + up())
    assert out == [
        StrokeStarted(1, "pen", PenPoint(100, 200, 1000)),
        StrokePoint(1, PenPoint(110, 210, 1200)),
        StrokePoint(1, PenPoint(120, 220, 1400)),
        StrokeEnded(1),
    ]


def test_first_point_comes_from_the_packet_that_sets_touch() -> None:
    out = run(hover(50, 60) + pen_down(100, 200, 900))
    assert out == [StrokeStarted(1, "pen", PenPoint(100, 200, 900))]


def test_no_trailing_zero_pressure_point_on_lift() -> None:
    out = run(pen_down(100, 200, 1000) + up())
    assert out == [StrokeStarted(1, "pen", PenPoint(100, 200, 1000)), StrokeEnded(1)]


def test_hover_is_ignored() -> None:
    assert run(pen_in() + hover(10, 10) + hover(20, 20)) == []


def test_packet_without_change_adds_no_point() -> None:
    out = run(pen_down(100, 200, 1000) + [syn()] + up())
    assert out == [StrokeStarted(1, "pen", PenPoint(100, 200, 1000)), StrokeEnded(1)]


def test_values_persist_across_packets() -> None:
    # evdev reports only changed axes; a packet with only pressure keeps the old x/y
    out = run(pen_down(100, 200, 1000) + [ev(EV_ABS, ABS_PRESSURE, 2000), syn()])
    assert out[-1] == StrokePoint(1, PenPoint(100, 200, 2000))


def test_stroke_ids_increase() -> None:
    out = run(pen_down(1, 1, 1) + up() + pen_down(2, 2, 2) + up())
    assert [e.stroke_id for e in out] == [1, 1, 2, 2]


def test_rubber_tool_is_tagged() -> None:
    out = run(pen_in("rubber") + pen_down(5, 5, 5) + up())
    assert out[0] == StrokeStarted(1, "rubber", PenPoint(5, 5, 5))


def test_no_stroke_before_position_is_known() -> None:
    assert run([ev(EV_KEY, BTN_TOUCH, 1), syn()]) == []


def test_syn_dropped_ends_stroke_and_discards_partial_packet() -> None:
    events = pen_down(100, 200, 1000) + [dropped()] + move(999, 999, 999)[:-1] + [syn()]
    out = run(events)
    assert out == [StrokeStarted(1, "pen", PenPoint(100, 200, 1000)), StrokeEnded(1)]


def test_after_drop_pressure_resumes_a_new_stroke() -> None:
    events = pen_down(100, 200, 1000) + [dropped(), syn()] + move(150, 250, 1100) + move(160, 260, 1200)
    out = run(events)
    assert out == [
        StrokeStarted(1, "pen", PenPoint(100, 200, 1000)),
        StrokeEnded(1),
        StrokeStarted(2, "pen", PenPoint(150, 250, 1100)),
        StrokePoint(2, PenPoint(160, 260, 1200)),
    ]


def test_after_drop_zero_pressure_stays_up() -> None:
    events = pen_down(100, 200, 1000) + [dropped(), syn()] + move(150, 250, 0)
    assert run(events) == [StrokeStarted(1, "pen", PenPoint(100, 200, 1000)), StrokeEnded(1)]


def test_close_ends_an_open_stroke_once() -> None:
    builder = StrokeBuilder()
    for event in pen_down(1, 2, 3):
        builder.feed(event)
    assert builder.open_stroke == 1
    assert builder.close() == [StrokeEnded(1)]
    assert builder.close() == []


def feed_all(builder: StrokeBuilder, events: list[InputEvent]) -> list[StrokeEvent]:
    out: list[StrokeEvent] = []
    for event in events:
        out += builder.feed(event)
    return out


def test_after_a_lost_reader_hovering_draws_nothing() -> None:
    # The pen lifted while the reader was being replaced; the new reader sees only hover (pressure 0).
    hovering = move(150, 250, 0) + move(160, 260, 0)
    control = StrokeBuilder()  # close() alone keeps "touching": the hover path would become ink
    feed_all(control, pen_down(1, 2, 3))
    control.close()
    assert feed_all(control, hovering) == [
        StrokeStarted(2, "pen", PenPoint(150, 250, 0)),
        StrokePoint(2, PenPoint(160, 260, 0)),
    ]

    builder = StrokeBuilder()
    feed_all(builder, pen_down(1, 2, 3))
    assert builder.lost() == [StrokeEnded(1)]
    assert feed_all(builder, hovering) == []
    assert builder.lost() == []  # nothing open: nothing ended


def test_after_a_lost_reader_pressure_resumes_the_pen() -> None:
    # The pen stayed down through the switch: the next pressed packet starts a new stroke.
    builder = StrokeBuilder()
    feed_all(builder, pen_down(1, 2, 3))
    builder.lost()
    assert feed_all(builder, move(50, 60, 700)) == [StrokeStarted(2, "pen", PenPoint(50, 60, 700))]
