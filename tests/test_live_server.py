"""The real path: uvicorn + a real WebSocket client, with the replay source.

TestClient bypasses uvicorn, so only this test proves the server can actually upgrade a
WebSocket (uvicorn needs the ``websockets`` package for that).
"""

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from inkboard.app import create_app
from inkboard.core.config import Settings
from inkboard.services.sources import ReplaySource

FIXTURE = Path(__file__).parent / "fixtures" / "demo.bin"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def server() -> Iterator[int]:
    port = free_port()
    settings = Settings(port=port, batch_ms=4)
    app = create_app(settings, ReplaySource(FIXTURE, speed=0, loop=True))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=3)
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert instance.started, "uvicorn did not start"
    yield port
    instance.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "uvicorn did not shut down"


def test_real_websocket_streams_strokes(server: int) -> None:
    url = f"ws://127.0.0.1:{server}/api/v1/ws"
    with connect(url, origin=f"http://127.0.0.1:{server}", open_timeout=5) as ws:  # type: ignore[arg-type]
        types = set()
        deadline = time.monotonic() + 10
        while not {"snapshot", "stroke_start", "points", "stroke_end"} <= types and time.monotonic() < deadline:
            types.add(json.loads(ws.recv(timeout=5))["type"])
    assert {"snapshot", "status", "stroke_start", "points", "stroke_end"} <= types


def test_real_server_rejects_a_foreign_origin(server: int) -> None:
    with pytest.raises(InvalidStatus) as rejected:  # noqa: SIM117
        with connect(f"ws://127.0.0.1:{server}/api/v1/ws", origin="https://evil.example", open_timeout=5):  # type: ignore[arg-type]
            pass
    assert rejected.value.response.status_code == 403


def test_real_server_serves_the_page_with_headers(server: int) -> None:
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{server}/", timeout=5) as response:  # nosec B310 - loopback
        assert response.status == 200
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
