"""The web app: health routes, the WebSocket and the static page.

Security for a single-user localhost tool:
- the server binds 127.0.0.1 only (see ``cli.cmd_serve``);
- TrustedHostMiddleware refuses any other Host header, which stops DNS rebinding;
- the WebSocket accepts only the page's own origins, fixed at start-up and never derived
  from the request, so another website open in the browser cannot read the strokes;
- strict CSP and framing headers on every HTTP response;
- the Screen view's frame route answers only the page's own fetches (Fetch Metadata), is never
  cached, and has no side effect: a grab on the tablet starts only from the Origin-checked
  WebSocket.

On the tablet path one ``TabletLink`` chooses USB or Wi-Fi (ADR 0003); the pen stream and the
screen grab both use the route it is connected on, and ``/api/v1/health/ready`` reports it.
"""

import asyncio
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

import anyio
import structlog
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from inkboard.core.config import Settings
from inkboard.services.hub import ClearRequest, ClientMessage, Hub, ScreenRequest
from inkboard.services.link import TabletLink
from inkboard.services.screen import FRAME_H, FRAME_W, Grabber, ScreenService, SshGrabber, screen_argv
from inkboard.services.sources import PenSource, SshSource, pen_stream_argv

log = structlog.get_logger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
MAX_CLIENT_MESSAGE = 1024
OVERFLOW_CLOSE = 1013  # "try again later": the page reconnects and gets a fresh snapshot
POLICY_CLOSE = 1008

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


# Python's mimetypes reads the Windows registry, which can map .js to text/plain; with
# `nosniff` the browser then refuses to run the script. Pin the types the page needs.
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html"}
for _ext, _type in STATIC_TYPES.items():
    mimetypes.add_type(_type, _ext)


def allowed_origins(port: int) -> frozenset[str]:
    return frozenset(f"http://{host}:{port}" for host in LOOPBACK_HOSTS)


FRAME_HEADERS = {"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"}


def same_origin_fetch(request: Request) -> bool:
    """True unless the browser says the request comes from elsewhere or is not a fetch.

    Chrome and Edge send Fetch Metadata to loopback; a missing header (tests, curl) passes,
    which TrustedHost and the loopback bind already confine to this machine.
    """
    site = request.headers.get("sec-fetch-site")
    dest = request.headers.get("sec-fetch-dest")
    return site in (None, "same-origin") and dest in (None, "empty")


def create_app(
    settings: Settings | None = None,
    source: PenSource | None = None,
    grabber: Grabber | None = None,
    extra_hosts: Sequence[str] = (),
) -> FastAPI:
    settings = settings or Settings()
    link: TabletLink | None = None
    if source is None:
        # The tablet: one link chooses USB or Wi-Fi for the pen stream, and the screen grab
        # follows whatever the pen stream is connected on.
        link = TabletLink(settings)
        pen_source: PenSource = SshSource(
            pen_stream_argv(settings),
            event_size=settings.event_size,
            link=link,
            argv_for=lambda route: pen_stream_argv(settings, address=route.address, report_wifi=True),
        )
        tablet = link

        def grab_argv() -> list[str] | None:
            route = tablet.current
            return screen_argv(settings, route.address) if route is not None else None

        grabber = grabber or SshGrabber(screen_argv(settings), argv_for=grab_argv)
    else:
        pen_source = source
    hub = Hub(
        settings.orientation_value,
        settings.batch_ms,
        on_pen=lambda down: screen.pen(down),
        on_connected=lambda: screen.source_connected(),
    )
    screen = ScreenService(grabber or SshGrabber(screen_argv(settings)), hub.publish)
    origins = allowed_origins(settings.port)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        on_events, on_status = hub.callbacks(loop)
        flusher = asyncio.create_task(hub.run_flush())
        screen.start(loop)
        pen_source.start(on_events, on_status)
        log.info("app.started", port=settings.port, orientation=settings.orientation)
        try:
            yield
        finally:
            await screen.close()  # kills a grab in flight, so shutdown never waits on the tablet
            await asyncio.to_thread(pen_source.stop)
            flusher.cancel()
            log.info("app.stopped")

    app = FastAPI(title="inkboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    app.state.screen = screen
    app.state.link = link
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*LOOPBACK_HOSTS, *extra_hosts])

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/api/v1/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/health/ready")
    async def ready() -> JSONResponse:
        status = hub.status
        if status.state == "connected":
            return JSONResponse({"status": "ready", "source": status.state, "link": status.link})
        return JSONResponse(
            {"error": "not_ready", "message": f"pen source is {status.state}", "detail": status.detail},
            status_code=503,
        )

    @app.get("/api/v1/screen/frame")
    async def screen_frame(request: Request) -> Response:
        # Declared before the static mount at "/", which would otherwise shadow it.
        if not same_origin_fetch(request):
            return JSONResponse(
                {"error": "forbidden", "message": "only the inkboard page may fetch the screen"},
                status_code=403,
                headers=FRAME_HEADERS,
            )
        frame = screen.latest
        if frame is None:
            return JSONResponse(
                {"error": "no_frame", "message": "no screen frame yet"}, status_code=404, headers=FRAME_HEADERS
            )
        menu = ";".join(",".join(str(v) for v in rect) for rect in frame.menu)
        return Response(
            frame.rgba,
            media_type="application/octet-stream",
            headers={
                **FRAME_HEADERS,
                "X-Inkboard-Frame-N": str(frame.n),
                "X-Inkboard-Frame-At": frame.at.isoformat(),
                "X-Inkboard-Frame-Size": f"{FRAME_W}x{FRAME_H}",
                "X-Inkboard-Frame-Layout": frame.layout,
                "X-Inkboard-Frame-Menu": menu,
            },
        )

    @app.websocket("/api/v1/ws")
    async def ws(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin not in origins:
            log.warning("ws.rejected_origin", origin=origin or "(none)")
            await websocket.close(code=POLICY_CLOSE)
            return
        await websocket.accept()
        client = hub.connect()

        async def send() -> None:
            while True:
                message = await client.queue.get()
                if client.overflowed:
                    await websocket.close(code=OVERFLOW_CLOSE)
                    return
                await websocket.send_json(message)

        async def receive() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                text = message.get("text")
                if text is None or len(text) > MAX_CLIENT_MESSAGE:
                    log.warning("ws.dropped_message", reason="binary or oversize")
                    continue
                try:
                    request = ClientMessage.validate_json(text)
                except ValidationError:
                    log.warning("ws.dropped_message", reason="invalid")
                    continue
                if isinstance(request, ClearRequest):
                    hub.clear()
                elif isinstance(request, ScreenRequest):
                    if request.action == "on":
                        screen.subscribe(client)
                    elif request.action == "off":
                        screen.unsubscribe(client)
                    else:
                        screen.refresh(client)

        # Both loops run in one anyio task group (Starlette's own concurrency model); whichever
        # ends first cancels the other, so a closed tab never leaves a loop behind and server
        # shutdown never waits on a handler that is only sending.
        try:
            async with anyio.create_task_group() as group:

                async def run(loop: Callable[[], Awaitable[None]]) -> None:
                    try:
                        await loop()
                    except (WebSocketDisconnect, OSError, RuntimeError) as exc:
                        # The browser went away mid-send or mid-receive.
                        log.info("ws.closed", reason=type(exc).__name__)
                    group.cancel_scope.cancel()

                group.start_soon(run, send)
                group.start_soon(run, receive)
        finally:
            screen.unsubscribe(client)
            hub.disconnect(client)

    app.mount("/", StaticFiles(packages=[("inkboard", "static")], html=True), name="static")
    return app
