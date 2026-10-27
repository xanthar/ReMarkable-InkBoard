import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from inkboard.core.config import Settings
from inkboard.services.events import EV_SYN, SYN_REPORT, InputEvent
from inkboard.services.sources import (
    LOST_DETAIL,
    MISALIGNED_DETAIL,
    READY_MARKER,
    ReplaySource,
    Route,
    Runner,
    SourceStatus,
    SshSource,
    parse_wifi_report,
    pen_stream_argv,
    read_raw,
    split_packets,
    ssh_base_argv,
)
from tests.helpers import down, encode, move, up

FAKE = str(Path(__file__).with_name("fake_device.py"))
TIMEOUT = 10


def fake(mode: str) -> list[str]:
    return [sys.executable, FAKE, mode]


class Recorder:
    """Collect callbacks from source threads; tests wait on queues, never on sleeps."""

    def __init__(self) -> None:
        self.events: queue.Queue[InputEvent] = queue.Queue()
        self.statuses: queue.Queue[SourceStatus] = queue.Queue()

    def on_events(self, events: list[InputEvent]) -> None:
        for event in events:
            self.events.put(event)

    def on_status(self, status: SourceStatus) -> None:
        self.statuses.put(status)

    def wait_status(self, predicate, timeout: float = TIMEOUT) -> SourceStatus:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while True:
            status = self.statuses.get(timeout=max(0.01, deadline - time.monotonic()))
            if predicate(status):
                return status

    def take_events(self, count: int, timeout: float = TIMEOUT) -> list[InputEvent]:
        return [self.events.get(timeout=timeout) for _ in range(count)]


def test_stream_delivers_events_and_reports_connected() -> None:
    rec = Recorder()
    source = SshSource(fake("stream-exit"), backoff=(30,))
    source.start(rec.on_events, rec.on_status)
    try:
        rec.wait_status(lambda s: s.state == "connected")
        events = rec.take_events(len(down(0, 0, 0) + move(0, 0, 0) + up()))
        assert events[-1].type == EV_SYN and events[-1].code == SYN_REPORT
        down_status = rec.wait_status(lambda s: s.state == "down")
        assert down_status.retrying is True
        # A stream that ran shows "connection lost", never ssh's own last line.
        assert down_status.detail == LOST_DETAIL
    finally:
        source.stop()


def test_frame_arrives_while_child_is_still_running() -> None:
    """Proves the pipe is read unbuffered: the child never exits until we stop it."""
    rec = Recorder()
    source = SshSource(fake("hold-open"), backoff=(30,))
    source.start(rec.on_events, rec.on_status)
    try:
        events = rec.take_events(len(down(0, 0, 0)), timeout=TIMEOUT)
        assert len(events) == len(down(0, 0, 0))
        assert source.process is not None and source.process.poll() is None
    finally:
        source.stop()


def test_stop_leaves_no_child_running() -> None:
    rec = Recorder()
    source = SshSource(fake("hold-open"), backoff=(30,))
    source.start(rec.on_events, rec.on_status)
    rec.take_events(1)
    child = source.process
    assert child is not None
    source.stop()
    assert child.poll() is not None


def test_stderr_noise_never_corrupts_frames() -> None:
    rec = Recorder()
    source = SshSource(fake("stderr-noise"), backoff=(30,))
    source.start(rec.on_events, rec.on_status)
    try:
        expected = down(100, 200, 1000) + move(110, 210, 1100) + up()
        events = rec.take_events(len(expected))
        assert [(e.type, e.code, e.value) for e in events] == [(e.type, e.code, e.value) for e in expected]
    finally:
        source.stop()


def test_auth_failure_reaches_status_detail_and_retries() -> None:
    rec = Recorder()
    source = SshSource(fake("fail-auth"), backoff=(0.01,))
    source.start(rec.on_events, rec.on_status)
    try:
        status = rec.wait_status(lambda s: s.state == "down")
        assert "Permission denied (publickey)" in status.detail
        assert status.retrying is True
        rec.wait_status(lambda s: s.state == "connecting")
        rec.wait_status(lambda s: s.state == "down")
        assert source.spawn_count >= 2
    finally:
        source.stop()


def test_misalignment_stops_after_one_retry() -> None:
    rec = Recorder()
    source = SshSource(fake("misaligned"), backoff=(0.01,))
    source.start(rec.on_events, rec.on_status)
    try:
        final = rec.wait_status(lambda s: s.state == "down" and not s.retrying)
        assert final.detail == MISALIGNED_DETAIL
        assert source.spawn_count == 2
    finally:
        source.stop()


def test_missing_ssh_client_is_fatal() -> None:
    rec = Recorder()
    source = SshSource([r"C:\nowhere\ssh.exe" if sys.platform == "win32" else "/nowhere/ssh"])
    source.start(rec.on_events, rec.on_status)
    try:
        status = rec.wait_status(lambda s: s.state == "down")
        assert status.retrying is False
        assert "ssh client not found" in status.detail
    finally:
        source.stop()


def test_exit_without_marker_uses_exit_code_as_detail() -> None:
    rec = Recorder()
    source = SshSource(fake("silent"), backoff=(30,))
    source.start(rec.on_events, rec.on_status)
    try:
        status = rec.wait_status(lambda s: s.state == "down")
        assert status.detail == "ssh exited with code 1"
    finally:
        source.stop()


def test_pen_stream_argv_is_built_from_settings() -> None:
    settings = Settings(ssh_bin="ssh", ssh_host="tablet-alias", pen_device="/dev/input/event3", event_size=24)
    argv = pen_stream_argv(settings)
    assert argv[0] == "ssh"
    assert "-T" in argv and "-n" in argv and "BatchMode=yes" in argv
    assert "-t" not in argv and "-tt" not in argv
    assert argv[-2] == "tablet-alias"
    assert argv[-1] == f"echo {READY_MARKER} >&2; exec dd bs=24 if=/dev/input/event3 2>/dev/null"
    assert pen_stream_argv(settings, reader="cat")[-1].endswith("exec cat /dev/input/event3 2>/dev/null")


BASE = [
    "ssh",
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


def test_ssh_base_argv_without_an_address_is_unchanged() -> None:
    # probe, record and the runbook's D7 depend on this exact form.
    assert ssh_base_argv(Settings(ssh_bin="ssh")) == [*BASE, "remarkable"]


def test_ssh_base_argv_with_an_address_pins_the_host_key_check() -> None:
    argv = ssh_base_argv(Settings(ssh_bin="ssh", ssh_host="tablet-alias"), IPv4Address("192.168.1.163"))
    assert argv == [
        *BASE,
        "-o",
        "HostName=192.168.1.163",
        "-o",
        "HostKeyAlias=tablet-alias",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "CheckHostIP=no",
        "-o",
        "UpdateHostKeys=no",
        "tablet-alias",
    ]


def test_wifi_report_is_only_added_when_asked() -> None:
    settings = Settings(ssh_bin="ssh")
    reported = pen_stream_argv(settings, report_wifi=True, address=IPv4Address("10.11.99.1"))
    assert reported[-1] == (
        f"echo {READY_MARKER} >&2; ip -4 -o addr show wlan0 >&2 2>/dev/null; "
        "exec dd bs=16 if=/dev/input/event1 2>/dev/null"
    )
    assert not any(c in reported[-1] for c in '"\\%\n')  # Windows argv quoting cannot change it
    assert "HostName=10.11.99.1" in reported
    assert "ip -4" not in pen_stream_argv(settings)[-1]


@pytest.mark.parametrize(
    ("line", "address"),
    [
        (
            "3: wlan0    inet 192.168.1.163/24 brd 192.168.1.255 scope global dynamic noprefixroute wlan0\\  ...",
            IPv4Address("192.168.1.163"),
        ),
        ("3: wlan0    inet 999.1.1.1/24 brd x", None),
        (READY_MARKER, None),
        ("Warning: Permanently added 'remarkable' (ED25519) to the list of known hosts.", None),
        ("2: usb0    inet 10.11.99.1/29 brd 10.11.99.7 scope global usb0", None),
    ],
)
def test_parse_wifi_report(line: str, address: IPv4Address | None) -> None:
    assert parse_wifi_report(line) == address


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_runner_runs_a_short_child() -> None:
    code, out, err = Runner().run(py("import sys; print('hi'); sys.stderr.write('e'); sys.exit(3)"), timeout=TIMEOUT)
    assert (code, out.strip(), err) == (3, b"hi", b"e")


def test_runner_times_out() -> None:
    started = time.monotonic()
    code, _, err = Runner().run(py("import time; time.sleep(30)"), timeout=0.5)
    assert code == -1 and b"no answer within 0.5 s" in err
    assert time.monotonic() - started < 8


def test_runner_reports_a_missing_program(tmp_path: Path) -> None:
    code, _, err = Runner().run([str(tmp_path / "missing.exe")], timeout=TIMEOUT)
    assert code == -1 and b"could not start" in err


def test_runner_stop_kills_a_running_child_and_refuses_new_ones() -> None:
    runner = Runner()
    result: list[tuple[int, bytes, bytes]] = []
    thread = threading.Thread(target=lambda: result.append(runner.run(py("import time; time.sleep(30)"), 60)))
    thread.start()
    deadline = time.monotonic() + TIMEOUT
    while not runner._children and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    runner.stop()
    thread.join(timeout=TIMEOUT)
    assert time.monotonic() - started < 5
    assert result and result[0][0] != 0
    assert runner.run(py("print(1)"), TIMEOUT) == (-1, b"", b"inkboard is stopping")


def test_replay_plays_packets_then_finishes(tmp_path: Path) -> None:
    events = down(100, 200, 1000) + move(110, 210, 1100) + up()
    path = tmp_path / "rec.bin"
    path.write_bytes(encode(events))
    rec = Recorder()
    source = ReplaySource(path, speed=0)
    source.start(rec.on_events, rec.on_status)
    try:
        assert rec.wait_status(lambda s: True).state == "connected"
        assert rec.take_events(len(events)) == events
        finished = rec.wait_status(lambda s: s.state == "down")
        assert finished.detail == "replay finished" and finished.retrying is False
    finally:
        source.stop()


def test_replay_loops(tmp_path: Path) -> None:
    events = down(1, 2, 3) + up()
    path = tmp_path / "rec.bin"
    path.write_bytes(encode(events))
    rec = Recorder()
    source = ReplaySource(path, speed=0, loop=True)
    source.start(rec.on_events, rec.on_status)
    try:
        assert rec.take_events(len(events) * 2) == events * 2
    finally:
        source.stop()


def test_replay_of_wrong_size_reports_down(tmp_path: Path) -> None:
    path = tmp_path / "rec.bin"
    stamped = [InputEvent(9000 + i, 777 * i, e.type, e.code, e.value) for i, e in enumerate(down(1, 2, 3) + up())]
    path.write_bytes(encode(stamped, 24))
    rec = Recorder()
    source = ReplaySource(path, event_size=16, speed=0)
    source.start(rec.on_events, rec.on_status)
    try:
        status = rec.wait_status(lambda s: s.state == "down")
        assert "cannot replay" in status.detail
    finally:
        source.stop()


def test_split_packets_keeps_trailing_partial() -> None:
    events = down(1, 2, 3) + move(4, 5, 6)[:-1]
    packets = split_packets(events)
    assert len(packets) == 2
    assert packets[0] == down(1, 2, 3)


def test_read_raw_returns_undecoded_bytes_and_stderr() -> None:
    raw, errors = read_raw(fake("stream-exit"), seconds=TIMEOUT)
    assert len(raw) == 16 * len(down(0, 0, 0) + move(0, 0, 0) + up())
    assert READY_MARKER in errors


def test_read_raw_stops_a_child_that_keeps_running() -> None:
    started = time.monotonic()
    raw, _ = read_raw(fake("hold-open"), seconds=3.0)  # room for a cold CI runner to start Python
    assert len(raw) == 16 * len(down(0, 0, 0))
    assert time.monotonic() - started < 8


def test_read_raw_stops_once_it_has_enough() -> None:
    started = time.monotonic()
    raw, _ = read_raw(fake("hold-open"), seconds=30.0, enough=16)
    assert len(raw) == 16 * len(down(0, 0, 0))
    assert time.monotonic() - started < 15  # well before the 30 s window


@pytest.mark.parametrize("mode", ["stream-exit"])
def test_backoff_resets_after_a_connected_run(mode: str) -> None:
    rec = Recorder()
    source = SshSource(fake(mode), backoff=(0.01, 30.0))
    source.start(rec.on_events, rec.on_status)
    try:
        # Every run connects, so the delay never escalates to 30 s: several spawns happen quickly.
        deadline = time.monotonic() + TIMEOUT
        while source.spawn_count < 3 and time.monotonic() < deadline:
            rec.wait_status(lambda s: s.state == "connecting")
        assert source.spawn_count >= 3
    finally:
        source.stop()


# ---- the source with a link ----------------------------------------------------------------

WIFI = Route("wifi", IPv4Address("192.168.1.163"))
USB = Route("usb", IPv4Address("10.11.99.1"))


@dataclass(frozen=True)
class NoRouteYet:
    detail: str


class FakeLink:
    """Hands out scripted routes and records what the source tells it."""

    def __init__(self, routes: list[object], block: bool = False) -> None:
        self.routes = list(routes)
        self.block = block
        self.calls: list[tuple[str, object]] = []
        self.on_switch: Callable[[str], None] | None = None
        self.stopped = threading.Event()

    def resolve(self) -> object:
        if self.block:
            self.stopped.wait(TIMEOUT)
            return NoRouteYet("inkboard is stopping")
        result = self.routes.pop(0) if len(self.routes) > 1 else self.routes[0]
        self.calls.append(("resolve", result))
        return result

    def connected(self, route: Route) -> None:
        self.calls.append(("connected", route))

    def lost(self) -> None:
        self.calls.append(("lost", None))

    def rejected(self, route: Route) -> None:
        self.calls.append(("rejected", route))

    def reported_wifi(self, address: IPv4Address) -> None:
        self.calls.append(("reported", address))

    def start(self, on_switch: Callable[[str], None]) -> None:
        self.on_switch = on_switch

    def stop(self) -> None:
        self.calls.append(("stop", None))
        self.stopped.set()


def linked(link: FakeLink, mode: str | dict[str, str], backoff: tuple[float, ...] = (30,)) -> SshSource:
    modes = mode if isinstance(mode, dict) else {"usb": mode, "wifi": mode}
    return SshSource(
        ["unused"],
        link=link,
        argv_for=lambda route: fake(modes[route.link]),
        backoff=backoff,  # type: ignore[arg-type]
    )


def test_linked_source_waits_for_a_route_then_connects_on_it() -> None:
    link = FakeLink([NoRouteYet("USB: cable not connected; next Wi-Fi search in 5 min"), WIFI])
    rec = Recorder()
    known_at_status: list[bool] = []

    def on_status(status: SourceStatus) -> None:
        if status.state == "connected":
            # The link must know the route before the status goes out: the status starts a Screen grab.
            known_at_status.append(("connected", WIFI) in link.calls)
        rec.on_status(status)

    source = linked(link, "ip-line", backoff=(0.01,))
    source.start(rec.on_events, on_status)
    try:
        down_status = rec.wait_status(lambda s: s.state == "down")
        assert down_status.detail == "USB: cable not connected; next Wi-Fi search in 5 min" and down_status.retrying
        status = rec.wait_status(lambda s: s.state == "connected")
        assert (status.link, status.address) == ("wifi", "192.168.1.163")
        assert known_at_status == [True]
        assert link.on_switch == source.switch  # the watcher was started with the source's switch
        rec.take_events(len(down(0, 0, 0) + move(0, 0, 0) + up()))
        deadline = time.monotonic() + TIMEOUT
        while ("reported", IPv4Address("192.168.1.163")) not in link.calls and time.monotonic() < deadline:
            time.sleep(0.01)  # the stderr reader handles the line on its own thread
        assert ("reported", IPv4Address("192.168.1.163")) in link.calls
    finally:
        source.stop()
    assert ("stop", None) in link.calls


def test_the_wifi_report_line_never_becomes_status_detail() -> None:
    link = FakeLink([WIFI])
    rec = Recorder()
    source = linked(link, "ip-line")
    source.start(rec.on_events, rec.on_status)
    try:
        rec.wait_status(lambda s: s.state == "connected")
        rec.take_events(len(down(0, 0, 0) + move(0, 0, 0) + up()))
        source.switch("switching to USB")
        status = rec.wait_status(lambda s: s.state == "connecting")
        assert "wlan0" not in status.detail
    finally:
        source.stop()


def test_switch_reconnects_at_once_with_its_detail() -> None:
    link = FakeLink([WIFI, USB])
    rec = Recorder()
    source = linked(link, "hold-open")  # backoff 30 s: a failure would stall the test
    source.start(rec.on_events, rec.on_status)
    try:
        rec.wait_status(lambda s: s.state == "connected" and s.link == "wifi")
        started = time.monotonic()
        source.switch("switching to USB")
        switching = rec.wait_status(lambda s: s.state != "connected")
        assert switching == SourceStatus("connecting", "switching to USB")  # no "down" in between
        back = rec.wait_status(lambda s: s.state == "connected")
        assert back.link == "usb" and time.monotonic() - started < 5
        assert ("lost", None) in link.calls
    finally:
        source.stop()


def test_switch_during_a_backoff_wait_retries_at_once() -> None:
    link = FakeLink([NoRouteYet("USB: no answer (tablet asleep?)"), USB])
    rec = Recorder()
    source = linked(link, "hold-open")
    source.start(rec.on_events, rec.on_status)
    try:
        rec.wait_status(lambda s: s.state == "down")  # now waiting 30 s
        source.switch("switching to USB")
        assert rec.wait_status(lambda s: s.state == "connecting").detail == "switching to USB"
        assert rec.wait_status(lambda s: s.state == "connected").link == "usb"
    finally:
        source.stop()


def test_host_key_refusal_rejects_the_route() -> None:
    # Refusal: ssh refuses the host key and the route is reported. Control: a run that only
    # fails authentication rejects nothing.
    link = FakeLink([WIFI])
    rec = Recorder()
    source = linked(link, "hostkey")
    source.start(rec.on_events, rec.on_status)
    try:
        assert rec.wait_status(lambda s: s.state == "down").detail == "Host key verification failed."
    finally:
        source.stop()
    assert ("rejected", WIFI) in link.calls

    link = FakeLink([WIFI])
    source = linked(link, "fail-auth")
    rec = Recorder()
    source.start(rec.on_events, rec.on_status)
    try:
        rec.wait_status(lambda s: s.state == "down")
    finally:
        source.stop()
    assert not any(name == "rejected" for name, _ in link.calls)


def test_stop_returns_while_the_link_is_still_resolving() -> None:
    link = FakeLink([], block=True)
    rec = Recorder()
    source = linked(link, "hold-open")
    source.start(rec.on_events, rec.on_status)
    rec.wait_status(lambda s: s.state == "connecting")
    started = time.monotonic()
    source.stop()
    assert time.monotonic() - started < 1
    assert link.calls[0] == ("stop", None)  # the link is stopped first, which ends a search
