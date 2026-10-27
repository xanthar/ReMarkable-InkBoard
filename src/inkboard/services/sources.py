"""Pen sources: the tablet over SSH, or a recorded file.

A source runs in its own threads and reports through two callbacks, ``on_events`` with
decoded events and ``on_status`` with connection state. Callers that live in an asyncio
loop hand the callbacks over with ``loop.call_soon_threadsafe``; threads keep the sources
independent of the loop type, which matters on Windows where a Selector loop cannot start
subprocesses.

SshSource runs the system OpenSSH client. The remote command first writes a marker to
stderr, so "connected" means the remote shell is running, not merely that ssh started; it
then ``exec``s ``dd`` on the pen device. stdout carries only frame bytes and is read
unbuffered, so a frame is delivered as soon as it arrives.

With a link (``serve``, ADR 0003) SshSource asks ``TabletLink`` for a route before
every attempt and builds the argv for it: an address brings the host-key options of
``ssh_base_argv``, so a device that is not the tablet ends ssh before any key is offered. The
link's watcher calls ``switch`` to move to a better link at once.
"""

import re
import subprocess  # nosec B404 - runs the user's own ssh client with a fixed argv
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import IO, Literal, Protocol

import structlog

from inkboard.core.config import Settings
from inkboard.services.events import EV_SYN, SYN_REPORT, EventDecoder, InputEvent, MisalignedStreamError

log = structlog.get_logger(__name__)

READY_MARKER = "inkboard-ready"
READ_SIZE = 65536
DEFAULT_BACKOFF = (1.0, 2.0, 3.0)
MISALIGNED_DETAIL = "frame misalignment - check INKBOARD_EVENT_SIZE / INKBOARD_PEN_DEVICE (run inkboard probe)"
# A new process group (Windows) or session (elsewhere) keeps Ctrl+C in the console from also
# reaching ssh, so shutdown stays ordered: inkboard stops its children itself.
SPAWN_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
NEW_SESSION = sys.platform != "win32"

State = Literal["connecting", "connected", "down"]
Link = Literal["usb", "wifi"]


@dataclass(frozen=True, slots=True)
class SourceStatus:
    state: State
    detail: str = ""
    retrying: bool = False
    link: Link | None = None  # set while connected over a chosen link
    address: str = ""


EventsCallback = Callable[[list[InputEvent]], None]
StatusCallback = Callable[[SourceStatus], None]


class PenSource(Protocol):
    def start(self, on_events: EventsCallback, on_status: StatusCallback) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Route:
    """Where the tablet is reached: the link and a validated address on it."""

    link: Link
    address: IPv4Address


def ssh_base_argv(settings: Settings, address: IPv4Address | None = None) -> list[str]:
    """The ssh options and host. Without an address the alias's own HostName is used (probe,
    record, the runbook's D7). With one, ssh goes to that address and checks the host key against the
    alias's known-hosts entry whatever the address, independent of ``~/.ssh/config``: a wrong or
    changed key ends ssh before user authentication, and inkboard never writes to known_hosts."""
    argv = [
        settings.ssh_bin,
        "-T",
        "-n",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
    ]
    if address is not None:
        argv += [
            "-o",
            f"HostName={address}",  # str(IPv4Address): digits and dots only
            "-o",
            f"HostKeyAlias={settings.ssh_host}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "CheckHostIP=no",
            "-o",
            "UpdateHostKeys=no",
        ]
    return [*argv, settings.ssh_host]


# The tablet's own Wi-Fi address, reported once at the start of the pen stream. The redirect
# order sends the line to stderr and drops ip's own errors (no wlan0, no address).
WIFI_REPORT = "ip -4 -o addr show wlan0 >&2 2>/dev/null"
WIFI_REPORT_LINE = re.compile(r"\bwlan0\s+inet (\d{1,3}(?:\.\d{1,3}){3})/\d{1,2}\b")


def pen_stream_argv(
    settings: Settings,
    reader: Literal["dd", "cat"] = "dd",
    *,
    address: IPv4Address | None = None,
    report_wifi: bool = False,
) -> list[str]:
    """The argv that streams the pen device. ``cat`` reads whole events of any size (for probe).
    ``report_wifi`` (serve only) adds one line on stderr with the tablet's Wi-Fi address."""
    read = f"dd bs={settings.event_size} if={settings.pen_device}" if reader == "dd" else f"cat {settings.pen_device}"
    report = f"{WIFI_REPORT}; " if report_wifi else ""
    return [*ssh_base_argv(settings, address), f"echo {READY_MARKER} >&2; {report}exec {read} 2>/dev/null"]


def parse_wifi_report(line: str) -> IPv4Address | None:
    """The address in an ``ip -4 -o addr show wlan0`` line, or None for any other line."""
    match = WIFI_REPORT_LINE.search(line)
    if match is None:
        return None
    try:
        return IPv4Address(match.group(1))
    except ValueError:
        return None


class Runner:
    """Short child processes (validation ssh, powershell, arp) that ``stop()`` can kill, so
    shutdown never waits on them. Each run returns ``(returncode, stdout, stderr)``; a run that
    could not start, timed out or was stopped returns code -1."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: set[subprocess.Popen[bytes]] = set()
        self._stopped = False

    def run(self, argv: Sequence[str], timeout: float) -> tuple[int, bytes, bytes]:
        with self._lock:
            if self._stopped:
                return -1, b"", b"inkboard is stopping"
            try:
                process = subprocess.Popen(  # nosec B603 - argv list, no shell
                    list(argv),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=SPAWN_FLAGS,
                    start_new_session=NEW_SESSION,
                )
            except OSError as exc:
                return -1, b"", f"could not start {argv[0]}: {exc}".encode()
            self._children.add(process)
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate(process)
            return -1, b"", f"no answer within {timeout:g} s".encode()
        finally:
            with self._lock:
                self._children.discard(process)
        return process.returncode, out, err

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            children = list(self._children)
        for process in children:
            _terminate(process)


class NoRoute(Protocol):
    @property
    def detail(self) -> str: ...


class LinkControl(Protocol):
    """What SshSource needs from ``link.TabletLink`` (a protocol, so this module stays below it)."""

    def resolve(self) -> "Route | NoRoute": ...

    def connected(self, route: Route) -> None: ...

    def lost(self) -> None: ...

    def rejected(self, route: Route) -> None: ...

    def reported_wifi(self, address: IPv4Address) -> None: ...

    def start(self, on_switch: Callable[[str], None]) -> None: ...

    def stop(self) -> None: ...


HOST_KEY_FAILED = "Host key verification failed"
# A stream that ran and then ended (cable out, sleep, Wi-Fi gone): ssh's own last line, such as
# "client_loop: send disconnect: Connection reset", goes to the log, not the status chip.
LOST_DETAIL = "connection lost"


class SshSource:
    """Supervise one ssh child at a time, restarting it with backoff until stopped.

    With a ``link``, every attempt first asks it for a route and builds the argv for that route
    with ``argv_for``; the link's watcher calls :meth:`switch` to move to a better link at once.
    Without one, the fixed ``argv`` is run (probe, record, tests).
    """

    def __init__(
        self,
        argv: Sequence[str],
        event_size: int = 16,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
        misaligned_retries: int = 1,
        *,
        link: LinkControl | None = None,
        argv_for: Callable[[Route], Sequence[str]] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._event_size = event_size
        self._backoff = tuple(backoff)
        self._misaligned_retries = misaligned_retries
        self._link = link
        self._argv_for = argv_for
        self._stopping = threading.Event()
        self._wake = threading.Event()  # ends a backoff wait early (stop, switch)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._switch_detail: str | None = None
        self._thread: threading.Thread | None = None
        self.spawn_count = 0

    @property
    def process(self) -> "subprocess.Popen[bytes] | None":
        return self._process

    def start(self, on_events: EventsCallback, on_status: StatusCallback) -> None:
        log.info("source.start", ssh_bin=self._argv[0], link=self._link is not None)
        self._thread = threading.Thread(
            target=self._supervise, args=(on_events, on_status), name="inkboard-ssh", daemon=True
        )
        self._thread.start()
        if self._link is not None:
            self._link.start(self.switch)

    def stop(self) -> None:
        if self._link is not None:
            self._link.stop()  # first: ends a search or a validation run the thread may be in
        with self._lock:
            self._stopping.set()
            process = self._process
        self._wake.set()
        if process is not None:
            _terminate(process)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def switch(self, detail: str) -> None:
        """End the current connection so the next attempt takes the better link: no backoff, not
        counted as a failure, and ``detail`` shows while it connects."""
        with self._lock:
            self._switch_detail = detail
            process = self._process
        self._wake.set()
        if process is not None:
            _terminate(process)

    def _take_switch(self) -> str | None:
        with self._lock:
            detail, self._switch_detail = self._switch_detail, None
        return detail

    def _pause(self, delay: float) -> None:
        self._wake.wait(delay)
        self._wake.clear()

    def _supervise(self, on_events: EventsCallback, on_status: StatusCallback) -> None:
        attempt = 0
        misaligned = 0
        logged_down = False
        detail = ""
        while not self._stopping.is_set():
            detail = self._take_switch() or detail
            on_status(SourceStatus("connecting", detail))
            detail = ""
            route: Route | None = None
            argv = self._argv
            if self._link is not None:
                resolved = self._link.resolve()
                if self._stopping.is_set():
                    break
                if not isinstance(resolved, Route):
                    on_status(SourceStatus("down", resolved.detail, retrying=True))
                    self._pause(self._backoff[min(attempt, len(self._backoff) - 1)])
                    attempt += 1
                    continue
                route = resolved
                argv = list(self._argv_for(route)) if self._argv_for is not None else self._argv
            outcome = self._run_once(argv, route, on_events, on_status)
            if self._link is not None:
                self._link.lost()
            if self._stopping.is_set():
                break
            switched = self._take_switch()
            if switched is not None:
                detail, attempt = switched, 0
                log.info("source.switch", detail=switched)
                continue
            if outcome.host_key and route is not None and self._link is not None:
                self._link.rejected(route)
            if outcome.connected:
                attempt = 0
                logged_down = False
            if not logged_down:
                # Log the transition once; retries while the tablet is away stay quiet.
                log.info("source.down", detail=outcome.detail)
                logged_down = True
            if outcome.fatal:
                on_status(SourceStatus("down", outcome.detail, retrying=False))
                log.warning("source.stopped", detail=outcome.detail)
                return
            if outcome.misaligned:
                misaligned += 1
                if misaligned > self._misaligned_retries:
                    on_status(SourceStatus("down", MISALIGNED_DETAIL, retrying=False))
                    log.warning("source.misaligned", retries=self._misaligned_retries)
                    return
            shown = LOST_DETAIL if outcome.connected else outcome.detail
            on_status(SourceStatus("down", shown, retrying=True))
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            self._pause(delay)

    def _run_once(
        self, argv: list[str], route: Route | None, on_events: EventsCallback, on_status: StatusCallback
    ) -> "_Outcome":
        try:
            process = subprocess.Popen(  # nosec B603 - argv list, no shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=SPAWN_FLAGS,
                start_new_session=NEW_SESSION,
            )
        except FileNotFoundError:
            return _Outcome(fatal=True, detail=f"ssh client not found: {argv[0]}")
        except OSError as exc:
            return _Outcome(fatal=True, detail=f"could not start ssh: {exc}")
        self.spawn_count += 1
        with self._lock:
            self._process = process
            stop_now = self._stopping.is_set()
        if stop_now:
            # stop() ran between the loop check and the spawn and could not see this child.
            _terminate(process)
        connected = threading.Event()
        last_line: list[str] = []
        link = self._link
        ready = SourceStatus("connected")
        on_ready: Callable[[], None] | None = None
        if route is not None and link is not None:
            ready = SourceStatus("connected", link=route.link, address=str(route.address))

            def on_ready() -> None:
                link.connected(route)  # before the status: the status asks the Screen view for a grab

        stderr_thread = threading.Thread(
            target=_drain_stderr,
            args=(process, connected, last_line, on_status),
            kwargs={"ready": ready, "on_ready": on_ready, "on_report": link.reported_wifi if link else None},
            name="inkboard-ssh-stderr",
            daemon=True,
        )
        stderr_thread.start()

        decoder = EventDecoder(self._event_size)
        misaligned = False
        assert process.stdout is not None  # nosec B101 - set by stdout=PIPE
        while True:
            chunk = process.stdout.read(READ_SIZE)
            if not chunk:
                break
            try:
                events = decoder.feed(chunk)
            except MisalignedStreamError:
                misaligned = True
                _terminate(process)
                break
            if events:
                on_events(events)
        returncode = _wait(process)
        stderr_thread.join(timeout=2)
        with self._lock:
            self._process = None
        detail = last_line[-1] if last_line else f"ssh exited with code {returncode}"
        return _Outcome(
            connected=connected.is_set(), misaligned=misaligned, detail=detail, host_key=HOST_KEY_FAILED in detail
        )


@dataclass(frozen=True, slots=True)
class _Outcome:
    connected: bool = False
    misaligned: bool = False
    fatal: bool = False
    detail: str = ""
    host_key: bool = False


def _drain_stderr(
    process: "subprocess.Popen[bytes]",
    connected: threading.Event,
    last_line: list[str],
    on_status: StatusCallback,
    *,
    ready: SourceStatus = SourceStatus("connected"),  # noqa: B008 - frozen, shared safely
    on_ready: Callable[[], None] | None = None,
    on_report: Callable[[IPv4Address], None] | None = None,
) -> None:
    assert process.stderr is not None  # nosec B101 - set by stderr=PIPE
    for raw in iter(process.stderr.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line == READY_MARKER and not connected.is_set():
            connected.set()
            if on_ready is not None:
                on_ready()
            log.info("source.connected", link=ready.link)
            on_status(ready)
            continue
        reported = parse_wifi_report(line)
        if reported is not None:
            # The tablet's own Wi-Fi address: remembered, never shown as status detail.
            if on_report is not None:
                on_report(reported)
            continue
        last_line[:] = [line]
        log.info("source.stderr", line=line)


def _terminate(process: "subprocess.Popen[bytes]") -> None:
    if process.poll() is not None:
        return
    process.terminate()
    _wait(process)


def _wait(process: "subprocess.Popen[bytes]") -> int:
    try:
        return process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=3)


class ReplaySource:
    """Play a recorded pen stream, paced by its own timestamps."""

    def __init__(self, path: Path, event_size: int = 16, speed: float = 1.0, loop: bool = False) -> None:
        self._path = path
        self._event_size = event_size
        self._speed = speed
        self._loop = loop
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_events: EventsCallback, on_status: StatusCallback) -> None:
        self._thread = threading.Thread(
            target=self._play, args=(on_events, on_status), name="inkboard-replay", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _play(self, on_events: EventsCallback, on_status: StatusCallback) -> None:
        try:
            packets = split_packets(EventDecoder(self._event_size).feed(self._path.read_bytes()))
        except (OSError, MisalignedStreamError) as exc:
            on_status(SourceStatus("down", f"cannot replay {self._path.name}: {exc}", retrying=False))
            return
        on_status(SourceStatus("connected", f"replay {self._path.name}"))
        while not self._stopping.is_set():
            previous: float | None = None
            for packet in packets:
                stamp = packet[-1].timestamp
                if previous is not None and self._speed > 0:
                    # Cap long gaps so a recording with a pause does not look frozen.
                    gap = min(max(stamp - previous, 0.0), 1.0) / self._speed
                    if self._stopping.wait(gap):
                        return
                previous = stamp
                on_events(packet)
            if not self._loop:
                break
            if self._stopping.wait(0.5):
                return
        on_status(SourceStatus("down", "replay finished", retrying=False))


def split_packets(events: list[InputEvent]) -> list[list[InputEvent]]:
    """Group events into packets closed by SYN_REPORT; a trailing partial packet is kept."""
    packets: list[list[InputEvent]] = []
    current: list[InputEvent] = []
    for event in events:
        current.append(event)
        if event.type == EV_SYN and event.code == SYN_REPORT:
            packets.append(current)
            current = []
    if current:
        packets.append(current)
    return packets


def read_raw(argv: Sequence[str], seconds: float, enough: int | None = None) -> tuple[bytes, str]:
    """Run ``argv`` for ``seconds``, or until ``enough`` bytes have arrived; return
    ``(stdout bytes, stderr text)`` without decoding.

    Used by probe and record, where the frame size is not trusted yet.
    """
    process = subprocess.Popen(  # nosec B603 - argv list, no shell
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=SPAWN_FLAGS,
        start_new_session=NEW_SESSION,
    )
    chunks: list[bytes] = []
    errors: list[bytes] = []

    def pump(stream: IO[bytes] | None, into: list[bytes]) -> None:
        if stream is None:
            return
        for chunk in iter(lambda: stream.read(READ_SIZE), b""):
            into.append(chunk)

    readers = [
        threading.Thread(target=pump, args=(process.stdout, chunks), daemon=True),
        threading.Thread(target=pump, args=(process.stderr, errors), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + seconds
    while process.poll() is None and time.monotonic() < deadline:
        if enough is not None and sum(map(len, chunks)) >= enough:
            break
        time.sleep(0.05)
    _terminate(process)
    for reader in readers:
        reader.join(timeout=2)
    return b"".join(chunks), b"".join(errors).decode("utf-8", errors="replace")
