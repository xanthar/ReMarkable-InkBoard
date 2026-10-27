import asyncio
import sys
import threading
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from inkboard.core.config import Settings
from inkboard.services.screen import (
    FRAME_BYTES,
    FRAME_H,
    FRAME_OFFSET,
    FRAME_W,
    ICON,
    LAYOUTS,
    MASKS,
    Frame,
    ScreenError,
    ScreenService,
    SshGrabber,
    TestCardGrabber,
    decode,
    detect_layout,
    icon_mask,
    make_test_card,
    remote_command,
    screen_argv,
    unpack,
)
from inkboard.services.sources import ssh_base_argv
from tests.fakes import FakeGrabber

FAKE = str(Path(__file__).with_name("fake_device.py"))
DELAY = 0.05
WHITE = b"\xff" * FRAME_BYTES


def fake(mode: str) -> list[str]:
    return [sys.executable, FAKE, mode]


def stamp(frame: bytearray, x0: int, y0: int, mask: bytes) -> None:
    bits = int.from_bytes(mask, "big")
    for i in range(ICON * ICON):
        if bits >> (ICON * ICON - 1 - i) & 1:
            start = ((y0 + i // ICON) * FRAME_W + x0 + i % ICON) * 4
            frame[start : start + 4] = b"\x00\x00\x00\xff"


def frame_with(*icons: tuple[int, int, str]) -> bytes:
    frame = bytearray(WHITE)
    for x, y, name in icons:
        stamp(frame, x, y, MASKS[name])
    return bytes(frame)


# ---- the remote command --------------------------------------------------------------------


def test_remote_command_survives_windows_argv_quoting() -> None:
    command = remote_command()
    assert not any(char in command for char in '"\\%\n')
    assert f"0x$a + {FRAME_OFFSET}" in command and f"bs={FRAME_BYTES} count=1" in command


def test_remote_command_gates_on_measured_firmware_before_reading() -> None:
    command = remote_command()
    gate, read = command.index("|| exit 5"), command.index("/proc/$pid/mem")
    assert gate < read
    assert "(3[.]24|3[.]29)[.]" in command


def test_screen_argv_takes_only_the_host_from_settings() -> None:
    settings = Settings(ssh_host="tablet-2")
    argv = screen_argv(settings)
    assert argv[:-1] == ssh_base_argv(settings)
    assert argv[-2] == "tablet-2" and argv[-1] == remote_command()


# ---- unpacking an ssh run --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "stderr", "kind", "text"),
    [
        (255, "REMARKABLE_RELEASE_VERSION=3.29.0.153\nPermission denied (publickey).", "ssh", "Permission denied"),
        (255, "", "ssh", "ssh failed"),
        (5, "REMARKABLE_RELEASE_VERSION=3.25.0.1", "firmware", "the tablet has 3.25.0.1"),
        (5, "", "firmware", "the tablet has ?"),
        (3, "", "no_xochitl", "not running"),
        (4, "", "no_map", "screen memory not found"),
        (1, "", "read", "could not be read"),
    ],
)
def test_unpack_names_the_cause(code: int, stderr: str, kind: str, text: str) -> None:
    with pytest.raises(ScreenError) as raised:
        unpack(code, b"", stderr)
    assert raised.value.kind == kind and text in str(raised.value)


def test_firmware_message_names_the_supported_versions() -> None:
    with pytest.raises(ScreenError, match="supports firmware 3.24 and 3.29"):
        unpack(5, b"", "REMARKABLE_RELEASE_VERSION=3.25.0.1")


# ---- the real grabber against a fake ssh -----------------------------------------------------


@pytest.mark.parametrize("mode", ["screen-ok", "screen-warning"])
def test_grabber_returns_one_frame(mode: str) -> None:
    assert SshGrabber(fake(mode)).grab() == make_test_card(1)


@pytest.mark.parametrize(
    ("mode", "kind"),
    [
        ("screen-short", "size"),
        ("screen-empty", "read"),
        ("screen-garbage", "read"),
        ("screen-firmware", "firmware"),
        ("screen-noxochitl", "no_xochitl"),
        ("screen-nomap", "no_map"),
        ("screen-exit1", "read"),
        ("fail-auth", "ssh"),
    ],
)
def test_grabber_reports_failures(mode: str, kind: str) -> None:
    with pytest.raises(ScreenError) as raised:
        SshGrabber(fake(mode)).grab()
    assert raised.value.kind == kind


def test_grabber_follows_the_link_and_refuses_without_one() -> None:
    # Refusal: the pen stream is not connected, so there is no link to grab over; nothing starts.
    route: list[list[str] | None] = [None]
    grabber = SshGrabber([r"C:\nowhere\ssh.exe"], argv_for=lambda: route[0])
    with pytest.raises(ScreenError) as raised:
        grabber.grab()
    assert raised.value.kind == "no_link" and str(raised.value) == "the tablet is not connected"
    assert grabber._process is None
    # Control: once connected, the grab runs the argv for that link, not the fixed one.
    route[0] = fake("screen-ok")
    assert grabber.grab() == make_test_card(1)


def test_screen_argv_with_an_address_pins_the_host_key_check() -> None:
    argv = screen_argv(Settings(ssh_bin="ssh"), IPv4Address("192.168.1.42"))
    assert "HostName=192.168.1.42" in argv and "StrictHostKeyChecking=yes" in argv
    assert argv[-2] == "remarkable" and argv[-1] == screen_argv(Settings(ssh_bin="ssh"))[-1]


def test_grabber_kills_a_tablet_that_does_not_answer() -> None:
    with pytest.raises(ScreenError, match="did not answer within 0.5 s"):
        SshGrabber(fake("screen-hang"), timeout_s=0.5).grab()


def test_grabber_reports_a_missing_ssh_client() -> None:
    with pytest.raises(ScreenError) as raised:
        SshGrabber([r"C:\nowhere\ssh.exe"]).grab()
    assert raised.value.kind == "ssh_missing"


def test_close_kills_a_grab_in_flight_and_refuses_new_ones() -> None:
    grabber = SshGrabber(fake("screen-hang"), timeout_s=30)
    failure: list[ScreenError] = []

    def run() -> None:
        try:
            grabber.grab()
        except ScreenError as error:
            failure.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    while grabber._process is None:
        thread.join(0.02)
    grabber.close()
    thread.join(10)
    assert not thread.is_alive() and failure
    with pytest.raises(ScreenError) as raised:
        grabber.grab()
    assert raised.value.kind == "closed"


# ---- checking and decoding ---------------------------------------------------------------------


def test_decode_swaps_blue_and_red() -> None:
    frame = bytearray(WHITE)
    frame[0:4] = bytes((1, 2, 3, 255))  # B G R A
    rgba, _, _ = decode(bytes(frame))
    assert rgba[0:4] == bytes((3, 2, 1, 255)) and len(rgba) == FRAME_BYTES


def test_decode_refuses_a_frame_that_is_not_a_screen_image() -> None:
    with pytest.raises(ScreenError) as raised:
        decode(WHITE[:3] + b"\x00" + WHITE[4:])
    assert raised.value.kind == "not_image" and "docs/operations/device-setup.md, D7" in str(raised.value)


def test_decode_refuses_a_wrong_size() -> None:
    with pytest.raises(ScreenError) as raised:
        decode(WHITE[:-4])
    assert raised.value.kind == "size"


@pytest.mark.parametrize(
    ("icons", "name"),
    [
        (((1328, 36, "close"), (36, 36, "toggle-portrait-open")), "portrait-open"),
        (((36, 36, "close"), (36, 1796, "toggle-landscape-open")), "landscape-open"),
        (((36, 36, "toggle-portrait-closed"),), "portrait-closed"),
        (((36, 1796, "toggle-landscape-closed"),), "landscape-closed"),
    ],
)
def test_each_measured_layout_is_recognised(icons: tuple[tuple[int, int, str], ...], name: str) -> None:
    layout = detect_layout(frame_with(*icons))
    assert layout is not None and layout.name == name


@pytest.mark.parametrize(
    "icons",
    [
        (),  # nothing: the library, a pop-up over the toolbar, left-handed mode
        ((1328, 36, "close"), (36, 36, "toggle-portrait-closed")),  # an X where a closed toolbar has none
        ((36, 36, "toggle-portrait-open"),),  # open toggle, close button missing
    ],
)
def test_anything_else_is_not_recognised(icons: tuple[tuple[int, int, str], ...]) -> None:
    assert detect_layout(frame_with(*icons)) is None
    _, name, menu = decode(frame_with(*icons))
    assert name == "unknown" and menu == ()


def test_icon_mask_reads_back_what_was_stamped() -> None:
    assert icon_mask(frame_with((100, 200, "close")), 100, 200) == MASKS["close"]


def test_menu_areas_stay_inside_the_frame() -> None:
    for layout in LAYOUTS:
        for x, y, w, h in layout.menu:
            assert x >= 0 and y >= 0 and x + w <= FRAME_W and y + h <= FRAME_H


def test_test_card_is_a_valid_portrait_open_frame_that_changes_per_grab() -> None:
    card = TestCardGrabber()
    first, second = card.grab(), card.grab()
    assert first != second and card.count == 2
    _, name, menu = decode(first)
    assert name == "portrait-open" and menu == LAYOUTS[0].menu
    card.close()


def test_a_frame_never_shows_its_pixels_in_a_repr() -> None:
    frame = Frame(1, datetime(2026, 10, 26, tzinfo=UTC), b"\x01" * 16, "portrait-open", ())
    assert "rgba" not in repr(frame) and "\\x01" not in repr(frame)


# ---- ScreenService -------------------------------------------------------------------------------


class Pages:
    """Collects what the service publishes."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def states(self) -> list[str]:
        return [m["state"] for m in self.messages]


async def settle(service: ScreenService) -> None:
    for _ in range(200):
        task = service._task
        if (task is None or task.done()) and service._timer is None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("screen service did not settle")


def run(scenario: Any) -> Any:
    return asyncio.run(scenario())


def make(grabber: FakeGrabber | None = None) -> tuple[ScreenService, FakeGrabber, Pages]:
    grabber = grabber or FakeGrabber()
    pages = Pages()
    return ScreenService(grabber, pages, delay_s=DELAY), grabber, pages


def test_nothing_happens_without_a_page_in_screen_view() -> None:
    async def scenario() -> None:
        service, grabber, pages = make()
        service.start(asyncio.get_running_loop())
        service.pen(True)
        service.pen(False)
        service.refresh("page")
        service.source_connected()
        await asyncio.sleep(DELAY * 3)
        await settle(service)
        assert grabber.calls == 0 and pages.messages == [] and service.latest is None

    run(scenario)


def test_subscribing_grabs_at_once_and_publishes_the_frame() -> None:
    async def scenario() -> None:
        service, grabber, pages = make()
        service.start(asyncio.get_running_loop())
        service.subscribe("page")
        await settle(service)
        assert pages.states() == ["updating", "ready"]
        assert pages.messages[1]["n"] == 1 and service.latest is not None
        assert service.latest.layout == "portrait-open" and service.subscriber_count == 1

    run(scenario)


def test_pen_up_grabs_after_the_delay_and_pen_down_cancels_it() -> None:
    async def scenario() -> None:
        service, grabber, _ = make()
        service.start(asyncio.get_running_loop())
        service.subscribe("page")
        await settle(service)
        service.pen(False)
        service.pen(True)  # writing again before the delay ran out
        await asyncio.sleep(DELAY * 3)
        assert grabber.calls == 1
        service.pen(False)
        await asyncio.sleep(DELAY / 2)
        assert grabber.calls == 1  # not yet
        await settle(service)
        assert grabber.calls == 2

    run(scenario)


def test_one_grab_at_a_time_with_one_follow_up() -> None:
    async def scenario() -> None:
        gate = threading.Event()
        service, grabber, _ = make(FakeGrabber(gate=gate))
        service.start(asyncio.get_running_loop())
        service.subscribe("page")
        await asyncio.to_thread(grabber.started.wait, 5)
        service.refresh("page")
        service.refresh("page")
        service.pen(False)
        await asyncio.sleep(DELAY * 2)  # the pen-up timer fires while the grab still runs
        gate.set()
        await settle(service)
        assert grabber.calls == 2

    run(scenario)


def test_a_pen_follow_up_waits_while_the_pen_is_down() -> None:
    async def scenario() -> None:
        gate = threading.Event()
        service, grabber, _ = make(FakeGrabber(gate=gate))
        service.start(asyncio.get_running_loop())
        service.subscribe("page")
        await asyncio.to_thread(grabber.started.wait, 5)
        service.pen(False)
        await asyncio.sleep(DELAY * 2)  # timer fired during the grab: an idle follow-up is queued
        service.pen(True)  # but the pen is down again
        gate.set()
        await settle(service)
        assert grabber.calls == 1

    run(scenario)


def test_a_failed_grab_publishes_the_error_and_keeps_no_frame() -> None:
    async def scenario() -> None:
        service, _, pages = make(FakeGrabber(error=ScreenError("no_map", "screen memory not found")))
        service.start(asyncio.get_running_loop())
        service.subscribe("page")
        await settle(service)
        assert pages.messages[-1] == {"type": "screen", "state": "error", "detail": "screen memory not found"}
        assert service.latest is None

    run(scenario)


def test_the_last_page_leaving_drops_the_frame_and_the_timer() -> None:
    async def scenario() -> None:
        service, grabber, pages = make()
        service.start(asyncio.get_running_loop())
        service.subscribe("a")
        service.subscribe("b")
        await settle(service)
        service.unsubscribe("a")
        assert service.latest is not None
        service.pen(False)
        service.unsubscribe("b")
        assert service.latest is None and service._timer is None
        before = len(pages.messages)
        await asyncio.sleep(DELAY * 3)
        assert len(pages.messages) == before and grabber.calls == 1

    run(scenario)


def test_reconnect_grabs_for_a_page_in_screen_view() -> None:
    async def scenario() -> None:
        service, grabber, _ = make()
        service.start(asyncio.get_running_loop())
        service.subscribe("page")
        await settle(service)
        service.source_connected()
        await settle(service)
        assert grabber.calls == 2

    run(scenario)


def test_close_stops_a_grab_in_flight_without_an_error_message() -> None:
    async def scenario() -> None:
        gate = threading.Event()
        service, grabber, pages = make(FakeGrabber(gate=gate))
        service.start(asyncio.get_running_loop())
        service.subscribe("page")
        await asyncio.to_thread(grabber.started.wait, 5)
        await service.close()
        assert grabber.closed.is_set() and pages.states() == ["updating"]
        service.subscribe("again")
        service.pen(False)
        await asyncio.sleep(DELAY * 2)
        assert grabber.calls == 1

    run(scenario)


def test_frames_arriving_after_the_last_page_left_are_dropped() -> None:
    async def scenario() -> None:
        gate = threading.Event()
        service, grabber, _ = make(FakeGrabber(gate=gate))
        service.start(asyncio.get_running_loop())
        service.subscribe("page")
        await asyncio.to_thread(grabber.started.wait, 5)
        service.unsubscribe("page")
        gate.set()
        await settle(service)
        assert service.latest is None

    run(scenario)


def test_the_grab_log_line_holds_timing_only() -> None:
    async def scenario() -> None:
        service, _, _ = make()
        service.start(asyncio.get_running_loop())
        service.subscribe("page")
        await settle(service)

    with capture_logs() as logs:
        run(scenario)
    (entry,) = [e for e in logs if e["event"] == "screen.grab"]
    assert set(entry) == {"event", "log_level", "ms", "result", "layout"}
    assert entry["result"] == "ok"
