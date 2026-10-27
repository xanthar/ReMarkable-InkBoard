"""Stand-in for ``ssh remarkable "echo inkboard-ready >&2; exec dd ..."`` in tests.

Usage: python tests/fake_device.py <mode>
Modes:
  stream-exit   marker, one stroke, exit 0
  hold-open     marker, one pen-down packet, then stay alive (proves frames are not buffered)
  taps          marker, four short strokes (one per corner), then stay alive
  stderr-noise  marker, stroke interleaved with stderr chatter, exit 0
  fail-auth     ssh-style auth failure on stderr, exit 255
  misaligned    marker, 24-byte frames (the source expects 16), then stay alive
  silent        no marker, exit 1
  ip-line       marker, the tablet's `ip -4 -o addr show wlan0` line, one stroke, then stay alive
  hostkey       ssh's strict host-key refusal on stderr, exit 255
Screen grab modes (stand-in for the screen read; stdout is a gzip stream, as on the tablet):
  screen-ok         firmware line, one synthetic frame, exit 0
  screen-warning    an ssh warning line, then as screen-ok
  screen-short      firmware line, a frame 4 bytes short
  screen-badalpha   firmware line, a frame with one transparent pixel
  screen-empty      firmware line, an empty gzip stream (dd read nothing)
  screen-garbage    firmware line, bytes that are not gzip
  screen-firmware   firmware 3.25.0.1, exit 5
  screen-noxochitl  exit 3
  screen-nomap      exit 4
  screen-exit1      exit 1
  screen-hang       firmware line, then stay alive
"""

import gzip
import sys
import time
from pathlib import Path

# Run as a script, so make the repo root importable for tests.helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inkboard.services.events import InputEvent, encode_event
from tests.helpers import down, move, up

FIRMWARE = "REMARKABLE_RELEASE_VERSION=3.29.0.153\n"
WIFI_LINE = "3: wlan0    inet 192.168.1.163/24 brd 192.168.1.255 scope global dynamic wlan0\\       valid_lft 1\n"

TAP_POINTS = ((100, 200), (100, 15500), (20800, 15500), (20800, 200))


def stamped(events: list[InputEvent]) -> list[InputEvent]:
    return [InputEvent(5000 + i, 1000 * i + 17, e.type, e.code, e.value) for i, e in enumerate(events)]


def write(events: list[InputEvent], size: int = 16) -> None:
    sys.stdout.buffer.write(b"".join(encode_event(e, size) for e in stamped(events)))
    sys.stdout.buffer.flush()


def marker() -> None:
    sys.stderr.write("inkboard-ready\n")
    sys.stderr.flush()


def main(mode: str) -> int:
    stroke = down(100, 200, 1000) + move(110, 210, 1100) + up()
    if mode == "stream-exit":
        marker()
        write(stroke)
        return 0
    if mode == "hold-open":
        marker()
        write(down(100, 200, 1000))
        time.sleep(60)
        return 0
    if mode == "taps":
        marker()
        for x, y in TAP_POINTS:
            write(down(x, y, 900) + up())
        time.sleep(60)
        return 0
    if mode == "stderr-noise":
        marker()
        for i in range(0, len(stroke), 2):
            sys.stderr.write(f"Warning: noise line {i}\n")
            sys.stderr.flush()
            write(stroke[i : i + 2])
        return 0
    if mode == "ip-line":
        marker()
        sys.stderr.write(WIFI_LINE)
        sys.stderr.flush()
        write(stroke)
        time.sleep(60)
        return 0
    if mode == "hostkey":
        sys.stderr.write("No ED25519 host key is known for remarkable and you have requested strict checking.\n")
        sys.stderr.write("Host key verification failed.\n")
        return 255
    if mode == "fail-auth":
        sys.stderr.write("root@10.11.99.1: Permission denied (publickey).\n")
        return 255
    if mode == "misaligned":
        marker()
        write(stroke, size=24)
        time.sleep(60)
        return 0
    if mode == "silent":
        return 1
    if mode.startswith("screen-"):
        return screen(mode)
    raise SystemExit(f"unknown mode {mode}")


def screen(mode: str) -> int:
    # Imported here, not at the top: the pen modes must start as fast as before (read_raw tests
    # give the child one second).
    from inkboard.services.screen import make_test_card

    exits = {"screen-firmware": 5, "screen-noxochitl": 3, "screen-nomap": 4, "screen-exit1": 1}
    if mode == "screen-firmware":
        sys.stderr.write("REMARKABLE_RELEASE_VERSION=3.25.0.1\n")
    if mode in exits:
        return exits[mode]
    if mode == "screen-warning":
        sys.stderr.write("Warning: Permanently added 'remarkable' (ED25519) to the list of known hosts.\n")
    sys.stderr.write(FIRMWARE)
    sys.stderr.flush()
    if mode == "screen-hang":
        time.sleep(60)
        return 0
    frame = make_test_card(1)
    if mode == "screen-short":
        frame = frame[:-4]
    elif mode == "screen-badalpha":
        frame = frame[:3] + b"\x00" + frame[4:]
    elif mode == "screen-empty":
        frame = b""
    out = b"not gzip at all" if mode == "screen-garbage" else gzip.compress(frame, compresslevel=1)
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
