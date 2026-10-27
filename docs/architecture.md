# Architecture

inkboard is a small Python service (FastAPI, run by uvicorn) and one static page. The service reads
the reMarkable 2's pen over SSH, turns the raw input records into strokes, and sends them to the
page over a WebSocket; on demand it also reads one frame of the tablet's screen and serves it to
the page. This document describes how the pieces fit, the exact export formats, and the rules the
code keeps. The decisions behind them are in [docs/adr/](adr/).

## Data flow

```
TabletLink.resolve() before every connection -> Route(usb 10.11.99.1 | wifi <ip>) or Unreachable
  order: USB (route check + port 22) -> link.json (reported, last ok) -> INKBOARD_WIFI_ADDRESSES
         -> discovery.find_tablet (only with INKBOARD_TABLET_MAC set: neighbour cache, UDP touch
            per address, MAC match, port 22)
  watcher thread, every 1 s: does 10.11.99.1 route over the USB adapter? (local, nothing sent)
         cable out on USB -> SshSource.switch(); cable in on Wi-Fi -> 2 checks + ssh echo -> switch
rM2 /dev/input/event1 --ssh [-o HostName=<ip> -o HostKeyAlias=remarkable
                             -o StrictHostKeyChecking=yes ...] remarkable
     "echo inkboard-ready >&2; ip -4 -o addr show wlan0 >&2 2>/dev/null; exec dd bs=N if=DEV 2>/dev/null"-->
  SshSource (threads): stdout unbuffered -> EventDecoder -> list[InputEvent]
                       stderr -> marker = "connected" (link.connected); wlan0 line = the
                       tablet's Wi-Fi address (link.json); other lines = status detail
  -> Hub (asyncio): StrokeBuilder, page state, orientation -> page units, batching,
                    bounded queue per browser tab (1000; on overflow the tab is closed with 1013
                    and reconnects to a fresh snapshot); any status but connected ends the stroke
  -> WebSocket /api/v1/ws -> static/app.js canvas (incremental drawing, Vertical/Horizontal view,
                             image export: Copy image / Save PNG, drawn again off-screen)
ReplaySource (recorded file) is the drop-in for offline use and tests.

Screen view (only while a page shows it):
  Hub pen down / up + reconnect -> ScreenService (asyncio): pen-up + 0.5 s, Refresh, subscribe,
       view turn -> one grab at a time in a worker thread
  -> SshGrabber: ssh [address options of link.current] remarkable
                 "<firmware gate>; dd frame from /proc/<xochitl>/mem | gzip -1"
  -> decode: size + alpha check, BGRA -> RGBA, menu layout from the tablet's own icons
  -> WebSocket "screen ready n" -> page fetches GET /api/v1/screen/frame (raw RGBA, no-store)
TestCardGrabber (synthetic frame) stands in for the tablet in replay mode and tests.
```

## Modules

| Module | Role |
|---|---|
| `core/config.py` | `INKBOARD_*` settings from the environment and a local `.env`: host and device patterns, `wifi_addresses` (private LAN only, at most 16), `tablet_mac` (no default) |
| `core/logging.py` | structlog: handwriting and frame keys dropped, plain tracebacks without locals |
| `services/events.py` | `input_event` records (16 or 24 bytes), partial-read buffering, misalignment check |
| `services/strokes.py` | touch-bounded strokes, hover ignored, `SYN_DROPPED` recovery, `lost()` when the reader is replaced |
| `services/geometry.py` | raw digitizer units to the normalised portrait page (`Orientation` swap/flip) |
| `services/sources.py` | `SshSource` (route per attempt, `switch`), `ReplaySource`, `read_raw`; `Route`, `ssh_base_argv` (host-key options with an address), `pen_stream_argv`, `Runner` (tracked children); status and backoff |
| `services/link.py` | `TabletLink`: the connection order, the cable watcher, host-key rejections, the search schedule, `link.json` |
| `services/discovery.py` | the search: physical subnets (PowerShell), the neighbour cache (`arp -a`), UDP touch, VPN check, `SearchResult` texts |
| `services/hub.py` | page state, the wire protocol (pydantic models; client messages `clear` and `screen`), fan-out; pen and reconnect hooks for the Screen view |
| `services/screen.py` | the Screen view: remote command and firmware gate, `SshGrabber`, frame check and decode, menu `LAYOUTS` with the icon masks, `TestCardGrabber`, `ScreenService` (when to grab) |
| `services/probe.py`, `cli.py` | `probe` measurements; the `serve`, `demo`, `record` and `probe` commands |
| `app.py` | `create_app()`, security middleware, the WebSocket, the frame route, static files |
| `static/app.js` | the page: Sketch and Screen boards, the stroke model, one drawing path for the screen and the image export, frame fetch and drawing, Vertical/Horizontal view, Copy image / Save PNG per board, Copy menu, status chip, Clear |
| `scripts/tablet_backup.py` | the full verified tablet backup and its one-file restore test (runbook D8); not part of the package |

## Naming

The tablet is in **portrait** or **landscape** mode (reMarkable's own words); the page has a
**Vertical** and a **Horizontal** view to match. The page shows one of two **boards**, **Sketch** or
**Screen** (the documentation says "the Sketch view" and "the Screen view"; the code says `board`,
stored as `inkboard.board`; the view and the Copy menu choice are `inkboard.view` and
`inkboard.copyMenu` in the browser's local storage). The tablet's screen image is a **frame** in
code (`frame*` names); in `app.js`, `screen` is the Sketch drawing target and keeps that meaning.
The WebSocket message `snapshot` is the full page state sent on connect; what a user hands to an
agent is the **image export** (the buttons **Copy image** and **Save PNG**).

## Image export

Copy image and Save PNG export the board that is showing.

### Sketch

The page draws the strokes again on an off-screen canvas. The numbers live in `static/app.js`
(`EXPORT_SHORT`, `EXPORT_LONG`, `PEN_WIDTH`, `MIN_FACTOR`) and `static/styles.css`
(`--export-ink`, `--export-paper`).

| Item | Value |
|---|---|
| Size | 1404 × 1872 px in Vertical, 1872 × 1404 px in Horizontal (the rM2 screen) |
| Colours | ink `#111111` on opaque `#ffffff`, the paper painted behind the ink last, so an eraser wipe stays white |
| Line width | `max(0.6, short side × 0.0035 × (0.25 + 0.75 × pressure))` px, round caps and joins; a stroke's first point is a dot |
| Horizontal | the portrait page turned a quarter turn clockwise (`HORIZONTAL_CLOCKWISE`) |
| Area | the full sheet, no margin, no crop |
| Eraser strokes | not drawn |
| File name | `inkboard-YYYY-MM-DD-HHMMSS.png`, local time |

### Screen

The frame as the tablet shows it. The numbers live in `services/screen.py` (`FRAME_*`,
`FRAME_OFFSET`, `SUPPORTED_FIRMWARE`, `LAYOUTS`, `MASKS`) and `static/app.js` (`FRAME_W`,
`FRAME_H`).

| Item | Value |
|---|---|
| Source | 1404 × 1872 BGRA from xochitl's memory: the map after `/dev/fb0` in `/proc/<pid>/maps`, + 2,629,640 bytes (measured on firmware 3.24.0.149 and 3.29.0.153) |
| Firmware gate | on the tablet, before any read: `update.conf` must say 3.24.x or 3.29.x, else exit 5 |
| Check | exactly 10,513,152 bytes and alpha 255 on every pixel, else nothing reaches the page |
| Size | 1404 × 1872 px in Vertical, 1872 × 1404 px in Horizontal (turned clockwise, as Sketch) |
| Colours | the tablet's own pixels (colour on 3.29), opaque |
| Menu | Copy menu off (default): the recognised layout's menu areas are painted white before turning; an unrecognised layout keeps the menu and the notice says so |
| Layouts | `portrait-open`, `portrait-closed`, `landscape-open`, `landscape-closed`, told apart by 40 × 40 icon masks (round toolbar button, close X) at fixed boxes; right-handed toolbar |
| File name | `inkboard-screen-YYYY-MM-DD-HHMMSS.png`, local time |

## Rules the code keeps

These hold for every change; a change that needs to break one is a design decision with its own
ADR.

- **Bind is fixed at 127.0.0.1.** The page carries handwriting and has no login; there is no option
  to bind elsewhere.
- **The WebSocket accepts only `http://127.0.0.1:<port>` and `http://localhost:<port>` origins**,
  fixed at start-up and never derived from the request; a missing Origin is refused.
  TrustedHostMiddleware refuses any other Host (DNS rebinding).
- **No handwriting in logs.** `core/logging.drop_handwriting` strips coordinate and pressure keys;
  logs carry ids, counts, network addresses and ssh's own messages, never points or frames. Raw
  captures go to the git-ignored `recordings/`; the only committed device data is a deliberately
  non-personal fixture.
- **The SSH argv is built in code from validated settings** (`ssh_host`, `pen_device` patterns):
  never a free-form command setting, never `shell=True`, never `-t`/`-tt` (a pty corrupts the
  binary stream).
- **Every connection to an address carries the host-key options** (`sources.ssh_base_argv`):
  `-o HostName=<ip> -o HostKeyAlias=<ssh_host> -o StrictHostKeyChecking=yes -o CheckHostIP=no
  -o UpdateHostKeys=no`, the address an `IPv4Address` (Wi-Fi candidates private LAN only). A wrong
  key stops ssh before the login key is offered; inkboard never writes `known_hosts`. Without an
  address the argv is the alias as written (probe, record, the runbook).
- **The Wi-Fi search is opt-in, quiet and bounded** (`services/discovery.py`): only with a tablet
  MAC set; private subnets of physical adapters only, up to /22, none routed into a VPN, never
  while the USB route is up; one UDP datagram per address, port 22 only on the tablet's MAC; once
  per lost episode, then after 5 and 15 minutes, then hourly until the tablet is back. A limit
  that stops it is said on the chip, never cut short silently.
- **Sources run in threads, never as asyncio subprocesses**: a Selector loop on Windows cannot
  start subprocesses. The thread-to-loop hand-off is `call_soon_threadsafe` via `Hub.callbacks`.
- **The WebSocket handler runs its send and receive loops in one anyio task group**: plain asyncio
  tasks do not take part in Starlette's cancellation and broke shutdown.
- **Static content types are pinned** in `app.py`: Windows can map `.js` to `text/plain`, and with
  `nosniff` the browser then refuses the script.
- **The Sketch export is made in the page**, and the page never logs strokes, blobs or image data;
  a failure shows the error name only.
- **The screen is only read.** One grab is one SSH run whose remote text is fixed in code plus two
  integers (no `"`, `\`, `%` or newline, so Windows argv quoting cannot change it); the firmware
  gate runs on the tablet before any memory is read; nothing is installed on the tablet for it.
- **A frame is handwriting-grade**: never logged (`frame`, `rgba`, `bgra`, `raw` are dropped keys;
  tracebacks are plain, without locals), never written to disk by the server, dropped when the last
  page leaves the Screen view, and served only by `GET /api/v1/screen/frame` with
  `Cache-Control: no-store`, `Cross-Origin-Resource-Policy: same-origin`, 403 when Fetch Metadata
  names another site or a non-fetch destination (a request without those headers, such as `curl`,
  is allowed, since only this machine can reach the port), and no side effect: a grab starts only
  from the Origin-checked WebSocket.
- **Tracebacks stay plain, by requirement.** When `rich` is installed (it is, through the dev
  tools), structlog's default console renderer prints every local variable in a traceback, so a
  frame or a list of points held in a local would reach the console. `core/logging.py` therefore
  pins `exception_formatter=structlog.dev.plain_traceback`, and `tests/test_logging.py` checks that
  the traceback prints while a local value does not.
- **`demo.bin` is generated, never edited**: `uv run inkboard demo --out tests/fixtures/demo.bin`;
  a test fails when it drifts from the generator.

## Windows-specific paths

The System32 `ssh.exe`, `CREATE_NEW_PROCESS_GROUP` for the ssh children (a new session elsewhere),
the screen command surviving Windows argv quoting, the registry MIME mapping, the search's
PowerShell `Get-NetAdapter -Physical` read and `arp.exe -a`, and the 2 s delay before Windows
reports a refused connection are proven on Windows by the test suite (which CI also runs on
`windows-latest`) and on a real tablet. The Linux CI exercises them through fakes:
`tests/netguard.py` refuses any real network use or child process in the link and search tests.

## Behaviour worth knowing

- **An asleep tablet does not answer** over USB or Wi-Fi. A running server reconnects by itself
  when it wakes. While it sleeps on the cable the PC keeps its cable address, so the cable watcher
  cannot see sleep; the pen stream's own connection does.
- **With the cable out, Windows routes `10.11.99.1` over the LAN**, so a TCP check of the USB
  address would leave the PC. The cable watcher therefore asks the routing table only (UDP
  `connect` plus `getsockname`, nothing sent).
- **Windows reports a refused TCP connection only after about 2 s** (two SYN retries), so the port
  check waits 3 s; a shorter wait would make "SSH off" look like "no answer". After a replug,
  Windows can take up to about 30 s to bring the cable network back.
- **A `dd` can outlive its connection on the tablet.** `dd` notices a closed pipe only when it
  writes, so after a stop it exits on the next pen movement; a connection that broke while the
  tablet slept stays open on the tablet until its TCP stack gives up. It is harmless: several
  readers can read the pen device at once.
- **The Windows OpenSSH client has no connection sharing**, so each screen grab opens a new SSH
  connection: about 0.5 s of the roughly 0.85 s a grab takes.
- **The screen offset is per firmware.** It was found by measurement and moved once already
  (3.23 to 3.24). `/etc/os-release` `VERSION_ID` is the OS image, not the firmware; the firmware is
  `REMARKABLE_RELEASE_VERSION` in `/usr/share/remarkable/update.conf`.
- **busybox on the tablet**: `pidof` has no `-s`; shell arithmetic is 64-bit.
