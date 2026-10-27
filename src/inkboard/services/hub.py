"""The hub: page state, stroke building and fan-out to browser clients.

Everything here runs on the asyncio loop. Sources live in their own threads and reach the
hub through :meth:`Hub.callbacks`, which hands each call over with ``call_soon_threadsafe``.

Points are batched and flushed every ``batch_ms`` or at stroke end, so a short tap is never
held back. Each client has a bounded queue; a client that falls behind (a frozen tab) is
flagged and closed by its handler, then reconnects and gets a fresh snapshot.
"""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, Field, TypeAdapter

from inkboard.services.events import InputEvent
from inkboard.services.geometry import Orientation, to_page
from inkboard.services.sources import EventsCallback, SourceStatus, StatusCallback
from inkboard.services.strokes import PenPoint, StrokeBuilder, StrokeEnded, StrokePoint, StrokeStarted, Tool

log = structlog.get_logger(__name__)

QUEUE_SIZE = 1000
DECIMALS = 4

Point = tuple[float, float, float]


# ---- wire protocol (server -> browser) --------------------------------------------------


class StrokeModel(BaseModel):
    id: int
    tool: Tool
    pts: list[Point]


class SnapshotMessage(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    strokes: list[StrokeModel]


class StrokeStartMessage(BaseModel):
    type: Literal["stroke_start"] = "stroke_start"
    id: int
    tool: Tool
    pts: list[Point]


class PointsMessage(BaseModel):
    type: Literal["points"] = "points"
    id: int
    pts: list[Point] = Field(min_length=1)


class StrokeEndMessage(BaseModel):
    type: Literal["stroke_end"] = "stroke_end"
    id: int


class StatusMessage(BaseModel):
    type: Literal["status"] = "status"
    source: Literal["connecting", "connected", "down"]
    detail: str = ""
    retrying: bool = False
    link: Literal["usb", "wifi"] | None = None
    address: str = ""


class ClearedMessage(BaseModel):
    type: Literal["cleared"] = "cleared"


# ---- wire protocol (browser -> server) --------------------------------------------------


class ClearRequest(BaseModel):
    type: Literal["clear"]


class ScreenRequest(BaseModel):
    """The page shows the Screen view (``on``), leaves it (``off``) or asks for a frame now."""

    type: Literal["screen"]
    action: Literal["on", "off", "refresh"]


ClientMessage = TypeAdapter(Annotated[ClearRequest | ScreenRequest, Field(discriminator="type")])

Message = dict[str, Any]


def _dump(model: BaseModel) -> Message:
    return model.model_dump(mode="json")


@dataclass
class _Stroke:
    id: int
    tool: Tool
    pts: list[Point] = field(default_factory=list)


@dataclass(eq=False)
class Client:
    queue: asyncio.Queue[Message] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    overflowed: bool = False


class Hub:
    def __init__(
        self,
        orientation: Orientation,
        batch_ms: int = 16,
        *,
        on_pen: Callable[[bool], None] | None = None,
        on_connected: Callable[[], None] | None = None,
    ) -> None:
        self._orientation = orientation
        self._batch_s = batch_ms / 1000
        self._builder = StrokeBuilder()
        self._strokes: dict[int, _Stroke] = {}
        self._pending: dict[int, list[Point]] = {}
        self._clients: set[Client] = set()
        # The Screen view listens to pen down / up and to the source reconnecting.
        self._on_pen = on_pen
        self._on_connected = on_connected
        self.status = SourceStatus("connecting")

    # -- thread hand-off ------------------------------------------------------------------

    def callbacks(self, loop: asyncio.AbstractEventLoop) -> tuple[EventsCallback, StatusCallback]:
        def safe(fn: Callable[[Any], None]) -> Callable[[Any], None]:
            def call(arg: Any) -> None:
                # RuntimeError: the loop closed during shutdown; nothing is listening any more.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(fn, arg)

            return call

        return safe(self.handle_events), safe(self.handle_status)

    # -- clients ----------------------------------------------------------------------------

    def connect(self) -> Client:
        client = Client()
        client.queue.put_nowait(self.snapshot())
        client.queue.put_nowait(self._status_message())
        self._clients.add(client)
        log.info("client.connected", clients=len(self._clients))
        return client

    def disconnect(self, client: Client) -> None:
        self._clients.discard(client)
        log.info("client.disconnected", clients=len(self._clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def snapshot(self) -> Message:
        strokes = [StrokeModel(id=s.id, tool=s.tool, pts=s.pts) for s in self._strokes.values()]
        return _dump(SnapshotMessage(strokes=strokes))

    # -- inputs -----------------------------------------------------------------------------

    def handle_events(self, events: list[InputEvent]) -> None:
        for event in events:
            for stroke_event in self._builder.feed(event):
                if isinstance(stroke_event, StrokeStarted):
                    point = self._point(stroke_event.point)
                    self.flush()
                    self._strokes[stroke_event.stroke_id] = _Stroke(stroke_event.stroke_id, stroke_event.tool, [point])
                    self._broadcast(
                        _dump(StrokeStartMessage(id=stroke_event.stroke_id, tool=stroke_event.tool, pts=[point]))
                    )
                    if self._on_pen is not None:
                        self._on_pen(True)
                elif isinstance(stroke_event, StrokePoint):
                    self._add_point(stroke_event.stroke_id, self._point(stroke_event.point))
                elif isinstance(stroke_event, StrokeEnded):
                    self._end(stroke_event.stroke_id)
                    if self._on_pen is not None:
                        self._on_pen(False)

    def handle_status(self, status: SourceStatus) -> None:
        if status.state != "connected":
            # A new reader on the tablet gets no replay of the pen's touch state: end the open
            # stroke (or it would join the next one with a line) and let pressure decide again.
            ended = self._builder.lost()
            for stroke in ended:
                self._end(stroke.stroke_id)
            if ended and self._on_pen is not None:
                self._on_pen(False)  # the pen is no longer down: the Screen view may grab again
        if status != self.status:
            reconnected = status.state == "connected" and self.status.state != "connected"
            self.status = status
            self._broadcast(self._status_message())
            if reconnected and self._on_connected is not None:
                self._on_connected()

    def publish(self, message: Message) -> None:
        """Send a message from another service (the Screen view) to every page."""
        self._broadcast(message)

    def clear(self) -> None:
        # Points still arriving for a stroke drawn across the clear are dropped (see _add_point).
        self._strokes.clear()
        self._pending.clear()
        self._broadcast(_dump(ClearedMessage()))
        log.info("page.cleared")

    # -- batching ---------------------------------------------------------------------------

    def flush(self) -> None:
        pending, self._pending = self._pending, {}
        for stroke_id, pts in pending.items():
            self._broadcast(_dump(PointsMessage(id=stroke_id, pts=pts)))

    async def run_flush(self) -> None:
        while True:
            await asyncio.sleep(self._batch_s)
            self.flush()

    # -- internals --------------------------------------------------------------------------

    def _point(self, point: PenPoint) -> Point:
        u, v, p = to_page(point.x, point.y, point.pressure, self._orientation)
        return round(u, DECIMALS), round(v, DECIMALS), round(p, DECIMALS)

    def _add_point(self, stroke_id: int, point: Point) -> None:
        stroke = self._strokes.get(stroke_id)
        if stroke is None:
            return
        stroke.pts.append(point)
        self._pending.setdefault(stroke_id, []).append(point)

    def _end(self, stroke_id: int) -> None:
        pts = self._pending.pop(stroke_id, None)
        if pts:
            self._broadcast(_dump(PointsMessage(id=stroke_id, pts=pts)))
        if stroke_id in self._strokes:
            self._broadcast(_dump(StrokeEndMessage(id=stroke_id)))
            log.info("stroke.end", stroke_id=stroke_id, point_count=len(self._strokes[stroke_id].pts))

    def _status_message(self) -> Message:
        s = self.status
        return _dump(
            StatusMessage(source=s.state, detail=s.detail, retrying=s.retrying, link=s.link, address=s.address)
        )

    def _broadcast(self, message: Message) -> None:
        for client in self._clients:
            if client.overflowed:
                continue
            try:
                client.queue.put_nowait(message)
            except asyncio.QueueFull:
                client.overflowed = True
                log.warning("client.overflow")
