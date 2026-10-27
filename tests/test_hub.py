import asyncio
from typing import Any

import pytest

from inkboard.services import hub as hub_module
from inkboard.services.geometry import Orientation, PenRanges
from inkboard.services.hub import Client, Hub
from inkboard.services.sources import SourceStatus
from tests.helpers import down, move, up

R = PenRanges()


def drain(client: Client) -> list[dict[str, Any]]:
    out = []
    while not client.queue.empty():
        out.append(client.queue.get_nowait())
    return out


def make() -> tuple[Hub, Client]:
    hub = Hub(Orientation(), batch_ms=16)
    client = hub.connect()
    return hub, client


def test_connect_sends_snapshot_then_status() -> None:
    _, client = make()
    first, second = drain(client)
    assert first == {"type": "snapshot", "strokes": []}
    assert second["type"] == "status" and second["source"] == "connecting"


def test_stroke_flow_with_batching_and_flush_at_end() -> None:
    hub, client = make()
    drain(client)
    hub.handle_events(down(R.max_x, 0, R.max_pressure) + move(0, R.max_y, 0) + move(R.max_x // 2, 0, 2048))
    msgs = drain(client)
    assert msgs == [{"type": "stroke_start", "id": 1, "tool": "pen", "pts": [[1.0, 0.0, 1.0]]}]
    hub.flush()
    (points,) = drain(client)
    assert points["type"] == "points" and points["id"] == 1 and len(points["pts"]) == 2
    assert points["pts"][1] == [0.5, 0.0, 0.5001]  # rounded to 4 decimals
    hub.handle_events(move(10, 10, 10) + up())
    msgs = drain(client)
    assert [m["type"] for m in msgs] == ["points", "stroke_end"]  # a pending point is flushed at stroke end


def test_snapshot_holds_the_page() -> None:
    hub, _ = make()
    hub.handle_events(down(0, 0, 0) + move(R.max_x, R.max_y, 0) + up())
    (stroke,) = hub.snapshot()["strokes"]
    assert stroke["id"] == 1 and len(stroke["pts"]) == 2


def test_status_broadcast_only_on_change() -> None:
    hub, client = make()
    drain(client)
    hub.handle_status(SourceStatus("connected"))
    hub.handle_status(SourceStatus("connected"))
    assert [m["source"] for m in drain(client)] == ["connected"]


def test_source_down_closes_open_stroke() -> None:
    hub, client = make()
    hub.handle_events(down(5, 5, 5))
    drain(client)
    hub.handle_status(SourceStatus("down", "Connection timed out", retrying=True))
    msgs = drain(client)
    assert msgs[0] == {"type": "stroke_end", "id": 1}
    assert msgs[1] == {
        "type": "status",
        "source": "down",
        "detail": "Connection timed out",
        "retrying": True,
        "link": None,
        "address": "",
    }


def test_a_link_switch_ends_the_open_stroke_and_lifts_the_pen() -> None:
    pens: list[bool] = []
    hub = Hub(Orientation(), batch_ms=16, on_pen=pens.append)
    client = hub.connect()
    hub.handle_status(SourceStatus("connected", link="usb", address="10.11.99.1"))
    hub.handle_events(down(5, 5, 5))
    drain(client)
    hub.handle_status(SourceStatus("connecting", "USB cable out - trying Wi-Fi"))
    msgs = drain(client)
    assert msgs[0] == {"type": "stroke_end", "id": 1}
    assert msgs[1]["source"] == "connecting" and msgs[1]["detail"] == "USB cable out - trying Wi-Fi"
    assert pens == [True, False]  # Screen's idle grab re-arms
    # The pen is still on the glass: its next pressed point starts a new stroke. Stroke 1 gets
    # nothing more, so no line joins the two.
    hub.handle_status(SourceStatus("connected", link="wifi", address="192.168.1.42"))
    hub.handle_events(move(9, 9, 900))
    msgs = drain(client)
    assert [m["type"] for m in msgs[1:]] == ["stroke_start"] and msgs[1]["id"] == 2
    assert all(m.get("id") != 1 for m in msgs)
    assert hub.status.link == "wifi"


def test_a_pen_lifted_during_a_switch_draws_no_hover_path() -> None:
    hub = Hub(Orientation(), batch_ms=16)
    client = hub.connect()
    hub.handle_status(SourceStatus("connected", link="usb", address="10.11.99.1"))
    hub.handle_events(down(5, 5, 5))
    hub.handle_status(SourceStatus("connecting", "USB cable out - trying Wi-Fi"))
    hub.handle_status(SourceStatus("connected", link="wifi", address="192.168.1.42"))
    drain(client)
    # The lift happened in the gap; the new reader sees only hovering (pressure 0), then a new touch.
    hub.handle_events(move(20, 20, 0) + move(30, 30, 0))
    assert [m for m in drain(client) if m["type"] in ("stroke_start", "points")] == []
    hub.handle_events(down(40, 40, 800))
    assert [m["id"] for m in drain(client) if m["type"] == "stroke_start"] == [2]


def test_a_plain_reconnect_with_no_open_stroke_does_not_touch_the_pen() -> None:
    # Control: nothing open, so nothing ends and the Screen view is not told the pen lifted.
    pens: list[bool] = []
    hub = Hub(Orientation(), batch_ms=16, on_pen=pens.append)
    client = hub.connect()
    drain(client)
    hub.handle_status(SourceStatus("connecting"))
    hub.handle_status(SourceStatus("connected", link="usb", address="10.11.99.1"))
    msgs = drain(client)
    assert [m["source"] for m in msgs] == ["connected"]  # "connecting" was already the state
    assert msgs[0]["link"] == "usb" and msgs[0]["address"] == "10.11.99.1"
    assert pens == []


def test_clear_empties_page_and_drops_points_of_a_cleared_stroke() -> None:
    hub, client = make()
    hub.handle_events(down(5, 5, 5))
    hub.clear()
    hub.handle_events(move(6, 6, 6))
    hub.flush()
    msgs = drain(client)
    assert msgs[-1] == {"type": "cleared"}
    assert hub.snapshot()["strokes"] == []


def test_overflow_flags_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_module, "QUEUE_SIZE", 3)
    hub = Hub(Orientation())
    slow = hub.connect()  # snapshot + status already queued
    for i in range(5):
        hub.handle_events(down(i, i, i) + up())
    assert slow.overflowed is True


def test_callbacks_hand_over_to_the_loop_from_a_thread() -> None:
    async def scenario() -> list[dict[str, Any]]:
        hub, client = make()
        drain(client)
        on_events, on_status = hub.callbacks(asyncio.get_running_loop())
        await asyncio.to_thread(on_status, SourceStatus("connected"))
        await asyncio.to_thread(on_events, down(1, 2, 3))
        await asyncio.sleep(0.05)
        return drain(client)

    msgs = asyncio.run(scenario())
    assert [m["type"] for m in msgs] == ["status", "stroke_start"]


def test_callbacks_after_loop_close_are_ignored() -> None:
    loop = asyncio.new_event_loop()
    hub = Hub(Orientation())
    on_events, on_status = hub.callbacks(loop)
    loop.close()
    on_status(SourceStatus("down"))  # must not raise
    on_events(down(1, 2, 3))


def test_pen_hook_sees_every_stroke_start_and_end() -> None:
    seen: list[bool] = []
    hub = Hub(Orientation(), on_pen=seen.append)
    client = hub.connect()
    hub.handle_events(down(1, 1, 1) + move(2, 2, 2) + up() + down(3, 3, 3) + up())
    assert seen == [True, False, True, False]
    types = [m["type"] for m in drain(client)]
    assert types.count("stroke_start") == 2 and types.count("stroke_end") == 2  # Sketch messages unchanged


def test_connected_hook_fires_on_each_reconnect_only() -> None:
    calls: list[str] = []
    hub = Hub(Orientation(), on_connected=lambda: calls.append("connected"))
    hub.handle_status(SourceStatus("connected"))
    hub.handle_status(SourceStatus("connected", "same state, new detail"))
    hub.handle_status(SourceStatus("down", "asleep", retrying=True))
    hub.handle_status(SourceStatus("connected"))
    assert calls == ["connected", "connected"]


def test_publish_reaches_every_page() -> None:
    hub, first = make()
    second = hub.connect()
    drain(first)
    drain(second)
    hub.publish({"type": "screen", "state": "updating"})
    assert drain(first) == drain(second) == [{"type": "screen", "state": "updating"}]


def test_run_flush_sends_batches() -> None:
    async def scenario() -> list[dict[str, Any]]:
        hub = Hub(Orientation(), batch_ms=5)
        client = hub.connect()
        drain(client)
        hub.handle_events(down(1, 1, 1) + move(2, 2, 2))
        task = asyncio.create_task(hub.run_flush())
        await asyncio.sleep(0.05)
        task.cancel()
        return drain(client)

    msgs = asyncio.run(scenario())
    assert [m["type"] for m in msgs] == ["stroke_start", "points"]
