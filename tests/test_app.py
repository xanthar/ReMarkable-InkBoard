import importlib.util
import time
from collections.abc import Iterator
from ipaddress import IPv4Address
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from inkboard.app import OVERFLOW_CLOSE, SECURITY_HEADERS, create_app
from inkboard.core.config import Settings
from inkboard.services.geometry import Orientation
from inkboard.services.link import TabletLink
from inkboard.services.screen import FRAME_BYTES, ScreenError, SshGrabber
from inkboard.services.sources import Route, SourceStatus
from tests.fakes import FakeGrabber, FakeSource
from tests.helpers import down, move, up

ORIGIN = {"origin": "http://127.0.0.1:8000"}
FRAME_URL = "/api/v1/screen/frame"


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def grabber() -> FakeGrabber:
    return FakeGrabber()


@pytest.fixture
def client(source: FakeSource, grabber: FakeGrabber) -> Iterator[TestClient]:
    settings = Settings(orientation="none", batch_ms=4)
    with TestClient(create_app(settings, source, grabber, extra_hosts=["testserver"])) as test_client:
        yield test_client


def test_websockets_library_is_installed() -> None:
    # uvicorn cannot serve the WebSocket without it; TestClient alone would never notice.
    assert importlib.util.find_spec("websockets") is not None


def test_lifespan_starts_and_stops_the_source(source: FakeSource, grabber: FakeGrabber) -> None:
    with TestClient(create_app(Settings(), source, grabber, extra_hosts=["testserver"])):
        assert source.started.is_set()
    assert source.stopped.is_set()
    assert grabber.closed.is_set()  # the screen grabber is stopped too


def test_the_tablet_grabber_is_the_default(source: FakeSource) -> None:
    app = create_app(Settings(ssh_host="tablet-2"), source)
    assert isinstance(app.state.screen._grabber, SshGrabber)
    assert app.state.link is None and app.state.screen._grabber._argv_for is None  # a given source: no link


def test_without_a_source_one_link_drives_the_pen_and_the_grab() -> None:
    # Built, not started: nothing reaches the network.
    app = create_app(Settings())
    link = app.state.link
    assert isinstance(link, TabletLink)
    assert app.state.screen._grabber._argv_for() is None
    link._current = Route("usb", IPv4Address("10.11.99.1"))
    assert "HostName=10.11.99.1" in app.state.screen._grabber._argv_for()


def test_ready_reports_the_link(client: TestClient, source: FakeSource) -> None:
    source.status(SourceStatus("connected", link="wifi", address="192.168.1.42"))
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        ws.receive_json()
        assert ws.receive_json()["link"] == "wifi"
    assert client.get("/api/v1/health/ready").json() == {"status": "ready", "source": "connected", "link": "wifi"}


def test_health(client: TestClient, source: FakeSource) -> None:
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}
    assert client.get("/api/v1/health/ready").status_code == 200
    source.status(SourceStatus("down", "Connection timed out", retrying=True))
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        ws.receive_json()
        assert ws.receive_json()["source"] == "down"  # the status has reached the hub
    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 503
    assert ready.json() == {"error": "not_ready", "message": "pen source is down", "detail": "Connection timed out"}


def test_security_headers(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html"),
        ("/app.js", "text/javascript"),
        ("/styles.css", "text/css"),
        ("/favicon.svg", "image/svg+xml"),
    ],
)
def test_static_files_have_executable_content_types(client: TestClient, path: str, content_type: str) -> None:
    # With nosniff, a wrong type (Windows can map .js to text/plain) stops the page script.
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == content_type


def test_page_has_export_buttons(client: TestClient) -> None:
    # The Sketch image export lives only in the page; this guards against losing its controls.
    page = client.get("/").text
    for element_id in ('id="copy"', 'id="save"', 'id="notice"'):
        assert element_id in page


def test_page_has_the_screen_view_controls(client: TestClient) -> None:
    page = client.get("/").text
    for element_id in ("board-sketch", "board-screen", "refresh", "copy-menu", "frame-board", "frame-state"):
        assert f'id="{element_id}"' in page


def test_foreign_host_is_rejected(client: TestClient) -> None:
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_snapshot_status_and_live_strokes(client: TestClient, source: FakeSource) -> None:
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        assert ws.receive_json() == {"type": "snapshot", "strokes": []}
        assert ws.receive_json()["source"] == "connected"
        source.push(down(0, 0, 4095) + move(20967, 15725, 0) + up())
        assert ws.receive_json() == {"type": "stroke_start", "id": 1, "tool": "pen", "pts": [[0.0, 0.0, 1.0]]}
        assert ws.receive_json() == {"type": "points", "id": 1, "pts": [[1.0, 1.0, 0.0]]}
        assert ws.receive_json() == {"type": "stroke_end", "id": 1}


def test_reconnect_gets_the_page_in_the_snapshot(client: TestClient, source: FakeSource) -> None:
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        ws.receive_json()
        ws.receive_json()
        source.push(down(0, 0, 0) + up())
        ws.receive_json()
        ws.receive_json()
    with client.websocket_connect("/api/v1/ws", headers={"origin": "http://localhost:8000"}) as ws:
        snapshot = ws.receive_json()
        assert [s["id"] for s in snapshot["strokes"]] == [1]


def test_clear_from_the_page(client: TestClient, source: FakeSource) -> None:
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        ws.receive_json()
        ws.receive_json()
        source.push(down(0, 0, 0) + up())
        ws.receive_json()
        ws.receive_json()
        ws.send_text('{"type": "nonsense"}')
        ws.send_bytes(b"\x00\x01")
        ws.send_text("x" * 5000)
        ws.send_json({"type": "clear"})
        assert ws.receive_json() == {"type": "cleared"}  # the junk before it was dropped


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "http://127.0.0.1:9999"},
        {"origin": "http://rebind.evil.example:8000"},
        {},
    ],
    ids=["foreign", "wrong-port", "rebinding", "missing"],
)
def test_websocket_rejects_other_origins(client: TestClient, headers: dict[str, str]) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/v1/ws", headers=headers) as ws:
        ws.receive_json()


def test_overflowed_client_is_closed_with_1013(client: TestClient, source: FakeSource) -> None:
    hub = client.app.state.hub  # type: ignore[attr-defined]
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        ws.receive_json()
        ws.receive_json()
        (server_side,) = hub._clients
        server_side.overflowed = True
        server_side.queue.put_nowait({"type": "cleared"})  # wake the sender
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == OVERFLOW_CLOSE


def test_orientation_setting_reaches_the_hub(source: FakeSource, grabber: FakeGrabber) -> None:
    settings = Settings(orientation="swap_xy", batch_ms=4)
    with (
        TestClient(create_app(settings, source, grabber, extra_hosts=["testserver"])) as client,
        client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws,
    ):
        ws.receive_json()
        ws.receive_json()
        source.push(down(20967, 0, 0))
        start = ws.receive_json()
        assert start["pts"] == [[0.0, 1.0, 0.0]]
        assert Orientation.parse(settings.orientation).swap_xy


# ---- Screen view ---------------------------------------------------------------------------------


def screen_message(ws: Any, state: str) -> dict[str, Any]:
    """Read until the next ``screen`` message in ``state`` (stroke and status messages pass by)."""
    for _ in range(50):
        message: dict[str, Any] = ws.receive_json()
        if message["type"] == "screen" and message["state"] == state:
            return message
    raise AssertionError(f"no screen {state} message")


def open_screen(ws: Any) -> dict[str, Any]:
    ws.receive_json()
    ws.receive_json()
    ws.send_json({"type": "screen", "action": "on"})
    return screen_message(ws, "ready")


def test_frame_route_has_nothing_before_a_page_asks(client: TestClient) -> None:
    response = client.get(FRAME_URL)
    assert response.status_code == 404
    assert response.json() == {"error": "no_frame", "message": "no screen frame yet"}
    assert response.headers["cache-control"] == "no-store"


def test_screen_on_grabs_and_the_page_fetches_the_frame(client: TestClient, grabber: FakeGrabber) -> None:
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        ready = open_screen(ws)
        assert ready["n"] == 1 and "at" in ready
        response = client.get(FRAME_URL, headers={"sec-fetch-site": "same-origin", "sec-fetch-dest": "empty"})
    assert response.status_code == 200 and len(response.content) == FRAME_BYTES
    headers = response.headers
    assert headers["content-type"] == "application/octet-stream"
    assert headers["cache-control"] == "no-store"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-inkboard-frame-n"] == "1"
    assert headers["x-inkboard-frame-size"] == "1404x1872"
    assert headers["x-inkboard-frame-layout"] == "portrait-open"
    assert headers["x-inkboard-frame-menu"] == "0,0,116,1872;1320,28,56,56"
    assert grabber.calls == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
        {"sec-fetch-site": "none"},
        {"sec-fetch-site": "same-origin", "sec-fetch-dest": "image"},
    ],
    ids=["cross-site", "same-site", "typed-url", "as-image"],
)
def test_frame_route_answers_only_the_page(client: TestClient, headers: dict[str, str]) -> None:
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        open_screen(ws)
        response = client.get(FRAME_URL, headers=headers)
    assert response.status_code == 403 and response.headers["cache-control"] == "no-store"


def test_frame_route_refuses_a_foreign_host(client: TestClient) -> None:
    assert client.get(FRAME_URL, headers={"host": "evil.example"}).status_code == 400


def test_fetching_the_frame_never_starts_a_grab(client: TestClient, grabber: FakeGrabber) -> None:
    for _ in range(3):
        client.get(FRAME_URL)
    assert grabber.calls == 0


def test_a_pen_stroke_brings_a_new_frame(client: TestClient, source: FakeSource) -> None:
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        open_screen(ws)
        source.push(down(0, 0, 0) + up())
        assert screen_message(ws, "ready")["n"] == 2


def test_refresh_and_off_from_the_page(client: TestClient, grabber: FakeGrabber) -> None:
    screen = client.app.state.screen  # type: ignore[attr-defined]
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        open_screen(ws)
        ws.send_json({"type": "screen", "action": "refresh"})
        assert screen_message(ws, "ready")["n"] == 2
        ws.send_json({"type": "screen", "action": "off"})
        ws.send_json({"type": "screen", "action": "explode"})  # dropped
        ws.send_json({"type": "clear"})
        assert ws.receive_json() == {"type": "cleared"}  # the messages before it were handled in order
        assert screen.subscriber_count == 0 and screen.latest is None
    assert grabber.calls == 2


def test_a_closed_page_leaves_the_screen_view(client: TestClient) -> None:
    screen = client.app.state.screen  # type: ignore[attr-defined]
    with client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws:
        open_screen(ws)
        assert screen.subscriber_count == 1
    for _ in range(100):
        if screen.subscriber_count == 0:
            break
        time.sleep(0.01)
    assert screen.subscriber_count == 0 and screen.latest is None


def test_a_failed_grab_reaches_the_page_and_sketch_keeps_working(source: FakeSource) -> None:
    grabber = FakeGrabber(error=ScreenError("firmware", "Screen view supports firmware 3.24 and 3.29"))
    settings = Settings(orientation="none", batch_ms=4)
    with (
        TestClient(create_app(settings, source, grabber, extra_hosts=["testserver"])) as client,
        client.websocket_connect("/api/v1/ws", headers=ORIGIN) as ws,
    ):
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "screen", "action": "on"})
        assert screen_message(ws, "error")["detail"].startswith("Screen view supports firmware")
        source.push(down(0, 0, 0) + up())
        assert ws.receive_json()["type"] == "stroke_start"
