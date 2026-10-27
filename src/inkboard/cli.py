"""Command line: ``inkboard serve | demo | record | probe``."""

import argparse
import queue
import subprocess  # nosec B404 - runs the user's own ssh client with a fixed argv
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from inkboard.core.config import Settings
from inkboard.core.logging import configure_logging
from inkboard.services.demo import generate_bytes
from inkboard.services.events import ABS_PRESSURE, ABS_X, ABS_Y, EventDecoder, InputEvent
from inkboard.services.geometry import RM2_RANGES
from inkboard.services.probe import (
    CORNERS,
    MAX_FIT_ERROR,
    detect_event_size,
    find_pen_device,
    fit_orientation,
    observed_maxima,
)
from inkboard.services.screen import Grabber, TestCardGrabber
from inkboard.services.sources import (
    PenSource,
    ReplaySource,
    SourceStatus,
    SshSource,
    pen_stream_argv,
    read_raw,
    ssh_base_argv,
)
from inkboard.services.strokes import PenPoint, StrokeBuilder, StrokeStarted

Handler = Callable[[argparse.Namespace], int]


def say(text: str) -> None:
    """CLI output for the person at the terminal (logging goes to stderr via structlog)."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


BIND_HOST = "127.0.0.1"  # fixed: the page carries handwriting and has no login
SAMPLE_BYTES = 4800  # 300 frames of 16 bytes or 200 of 24: enough to tell the sizes apart


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from inkboard.app import create_app

    overrides: dict[str, object] = {}
    if args.port is not None:
        overrides["port"] = args.port
    if args.event_size is not None:
        overrides["event_size"] = args.event_size
    settings = Settings(**overrides)  # type: ignore[arg-type]
    source: PenSource | None = None  # None: create_app reads the tablet over the chosen link
    grabber: Grabber | None = None  # the tablet's screen over SSH
    if args.source == "replay":
        if not args.replay:
            say("--source replay needs --replay FILE")
            return 2
        source = ReplaySource(Path(args.replay), event_size=settings.event_size, speed=args.speed, loop=args.loop)
        grabber = TestCardGrabber()  # no tablet: the Screen view shows a synthetic test card
        where = f"replay of {args.replay}; Screen view shows a test card"
    else:
        where = f"{settings.pen_device} on {settings.ssh_host}, over USB or Wi-Fi"
    say(f"inkboard: http://{BIND_HOST}:{settings.port}  (pen: {where}; Ctrl+C to stop)")
    uvicorn.run(
        create_app(settings, source, grabber),
        host=BIND_HOST,
        port=settings.port,
        timeout_graceful_shutdown=3,
        access_log=False,
        log_level="warning",
    )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = generate_bytes(args.event_size)
    out.write_bytes(data)
    say(f"wrote {out} ({len(data)} bytes, {len(data) // args.event_size} events)")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    settings = Settings()
    out = Path(args.out) if args.out else Path("recordings") / f"{datetime.now():%Y-%m-%dT%H%M%S}.bin"
    say(f"recording {args.seconds} s from {settings.ssh_host}:{settings.pen_device} - draw now")
    raw, errors = read_raw(pen_stream_argv(settings), args.seconds)
    if not raw:
        say(f"nothing recorded. ssh said: {_last_line(errors) or '(nothing)'}")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    say(f"wrote {out} ({len(raw)} bytes, {len(raw) // settings.event_size} events)")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    settings = Settings()
    say(f"1/4 input devices on {settings.ssh_host}")
    try:
        listing = subprocess.run(  # nosec B603 - argv list, no shell
            [*ssh_base_argv(settings), "cat /proc/bus/input/devices"],
            capture_output=True,
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        say(f"could not reach the tablet: {exc}")
        return 1
    if listing.returncode != 0:
        say(f"ssh failed: {_last_line(listing.stderr.decode('utf-8', errors='replace'))}")
        return 1
    devices = listing.stdout.decode("utf-8", errors="replace")
    say(devices)
    found = find_pen_device(devices)
    say(f"pen digitizer: {found or 'NOT FOUND'} (configured: {settings.pen_device})")
    if found is None:
        return 1
    settings = settings.model_copy(update={"pen_device": found})

    say(f"2/4 frame size - move the pen over the screen (hovering is enough); waiting up to {args.wait:g} s")
    raw, errors = read_raw(pen_stream_argv(settings, reader="cat"), args.wait, enough=SAMPLE_BYTES)
    size = detect_event_size(raw)
    say(f"captured {len(raw)} bytes; frame size: {size or 'UNDECIDED'} (configured: {settings.event_size})")
    if size is None:
        say(f"ssh said: {_last_line(errors) or '(nothing)'} - move the pen and run probe again")
        return 1
    settings = settings.model_copy(update={"event_size": size})

    say("3/4 orientation - tablet in portrait, page text upright; tap just inside each corner")
    taps = wait_for_taps(settings, [name for name, _ in CORNERS], timeout=args.tap_timeout)
    if taps is None:
        say("    no tap seen - run probe again")
        return 1
    fit = fit_orientation(taps)
    say(f"    fit error {fit.error:.3f}, next best {fit.runner_up:.3f} (clear below {MAX_FIT_ERROR})")
    if not fit.clear:
        say("    the taps match no orientation - check portrait mode and the tap order, then run probe again")
        return 1
    orientation = fit.orientation

    say("4/4 sanity check against the known ranges")
    maxima = observed_maxima(EventDecoder(size).feed(raw))
    limits = {ABS_X: RM2_RANGES.max_x, ABS_Y: RM2_RANGES.max_y, ABS_PRESSURE: RM2_RANGES.max_pressure}
    over = {code: value for code, value in maxima.items() if code in limits and value > limits[code]}
    if over:
        say(f"    WARNING: values above the known ranges {over} - stop and check before using these settings")
    else:
        say("    observed values are within the known ranges")
    say("")
    say("settings for this tablet:")
    say(f"INKBOARD_PEN_DEVICE={found}")
    say(f"INKBOARD_EVENT_SIZE={size}")
    say(f"INKBOARD_ORIENTATION={orientation}")
    return 0 if not over else 1


def wait_for_taps(settings: Settings, corners: Sequence[str], timeout: float) -> list[PenPoint] | None:
    """Ask for one tap per corner over a single pen stream; return the first point of each.

    The first prompt waits until the stream is connected, so an early tap is not lost.
    Returns None when the tablet cannot be reached, the stream drops or a tap does not come.
    """
    found: queue.Queue[PenPoint | SourceStatus] = queue.Queue()
    builder = StrokeBuilder()

    def on_events(events: list[InputEvent]) -> None:
        for event in events:
            for stroke_event in builder.feed(event):
                if isinstance(stroke_event, StrokeStarted):
                    found.put(stroke_event.point)

    def on_status(status: SourceStatus) -> None:
        if status.state == "connected" or (status.state == "down" and not status.retrying):
            found.put(status)

    source = SshSource(pen_stream_argv(settings), event_size=settings.event_size)
    source.start(on_events, on_status)
    # stdout and stderr have their own reader threads, so a tap can be reported just before
    # the ready marker that preceded it on the tablet; keep such taps in order.
    early: list[PenPoint] = []
    taps: list[PenPoint] = []
    try:
        while True:
            item = found.get(timeout=timeout)
            if isinstance(item, PenPoint):
                early.append(item)
            elif item.state == "connected":
                break
            else:
                return None
        for corner in corners:
            say(f"    tap the {corner} corner")
            item = early.pop(0) if early else found.get(timeout=timeout)
            if not isinstance(item, PenPoint):
                return None
            taps.append(item)
    except queue.Empty:
        return None
    finally:
        source.stop()
    return taps


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip() and "inkboard-ready" not in line]
    return lines[-1] if lines else ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inkboard", description="Live drawing board for a reMarkable 2.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="serve the live drawing board on http://127.0.0.1")
    serve.add_argument("--source", choices=("ssh", "replay"), default="ssh")
    serve.add_argument("--replay", help="recording to play with --source replay")
    serve.add_argument("--speed", type=float, default=1.0, help="replay speed; 0 = as fast as possible")
    serve.add_argument("--loop", action="store_true", help="repeat the replay")
    serve.add_argument("--port", type=int, help="default 8000 (INKBOARD_PORT)")
    serve.add_argument("--event-size", type=int, choices=(16, 24), help="frame size (INKBOARD_EVENT_SIZE)")
    serve.set_defaults(handler=cmd_serve)

    demo = sub.add_parser("demo", help="write a synthetic pen recording")
    demo.add_argument("--out", required=True, help="file to write")
    demo.add_argument("--event-size", type=int, choices=(16, 24), default=16)
    demo.set_defaults(handler=cmd_demo)

    record = sub.add_parser("record", help="record the raw pen stream from the tablet")
    record.add_argument("--out", help="file to write (default: recordings/<timestamp>.bin, gitignored)")
    record.add_argument("--seconds", type=float, default=15.0)
    record.set_defaults(handler=cmd_record)

    probe = sub.add_parser("probe", help="find the pen device, frame size and orientation")
    probe.add_argument("--wait", type=float, default=20.0, help="how long to wait for pen data in the frame-size step")
    probe.add_argument("--tap-timeout", type=float, default=30.0, help="how long to wait for each corner tap")
    probe.set_defaults(handler=cmd_probe)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    handler: Handler = args.handler
    return handler(args)
