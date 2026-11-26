# ADR 0004 — A command pulls the Screen view as a PNG, as a client of `serve`

- **Status:** accepted
- **Date:** 2026-11-26
- **Decided by:** the maintainer

## Context

inkboard is for handing a hand-drawn page to an AI agent. Until 0.1.0 that meant Copy image or
Save PNG in the page, and pasting the image into the agent's chat. An agent that can run a command
should be able to fetch the current page itself, as often as the user redraws, without the page
open and without copy and paste. The image must be the one the page exports from the Screen view
(ADR 0002): the tablet's own screen, with the menu painted white and a landscape page turned
upright.

Until now the code kept these rules: a grab starts only from the Origin-checked WebSocket, a frame
is served only to the page's own same-origin fetch, and a frame is never written to disk by the
server.

Options considered:

1. **A command that is a local client of a running `serve`**: it connects to the page's own
   WebSocket with the page's origin, asks for the Screen view, fetches the frame once from
   `GET /api/v1/screen/frame`, and encodes the PNG itself.
2. A new server route that grabs and returns a PNG in one request.
3. A command that asks an open page to export its image.
4. A command that reads the tablet over SSH by itself, without `serve`.

## Decision

Option 1: `inkboard snapshot --out FILE`.

- It connects to `ws://127.0.0.1:<port>/api/v1/ws` with `Origin: http://127.0.0.1:<port>`, sends
  `screen on`, and takes a frame only after an `updating` that reached it, so the frame was grabbed
  after it connected; a `ready` from a grab already running is answered with one `refresh`.
- It fetches the frame once, checks its size, its number and its headers, sends `screen off` and
  closes the connection before it encodes anything.
- It paints the menu areas white, turns a `landscape-*` frame a quarter turn clockwise (as the
  page's Horizontal view), and writes an 8-bit RGB PNG with only IHDR, IDAT and IEND, through a
  temporary file beside the target and one rename. The encoder is the standard library's `zlib`.
- It prints one line on success and one `snapshot:` line with an exit code on failure (README,
  "`inkboard snapshot` for scripts and agents"). The screen error message gains a `kind` field so
  the command can tell a tablet that is not connected from a failed grab.
- It refuses an `--out` inside a git work tree.

## Reasons

- Option 1 adds no new route and no new way into the server: the WebSocket and the frame route
  already exist, and the command uses them exactly as the page does. `serve` stays the one owner
  of the tablet link, the host keys and `link.json` (ADR 0003).
- Option 2 adds a request with a side effect (a grab on the tablet) and an image encoder to the
  server; ADR 0002 rejected encoding on the server for the same reason. The frame route keeps its
  "no side effect" rule.
- Option 3 needs an open page and a channel from the server to that page's export code.
- Option 4 duplicates the link choice, the host-key handling and `link.json`, which would then
  have two writers.

## Consequences

- **No new reachability.** The `Origin` check is a defence against other websites in the browser,
  not an authentication: since 0.1.0 any program on the PC could send that header, start a grab and
  fetch the frame (SECURITY.md, Known limits). What is new is that a shipped command uses that
  path.
- **A frame now reaches the disk**, at the path the user names and nowhere else. The server still
  writes nothing. The temporary file is created owner-only and removed on any failure.
- **Every open page sees the command's frame.** Screen messages go to every client, so a page in
  the Screen view shows the frame the command asked for, and any page fetches it once.
- **The command's grab is at once**, even while the pen is down; the page's own grabs wait for the
  pen to rest. A pull in the middle of a stroke shows the stroke so far.
- **An error line can carry ssh's own last line**, which may name the tablet's address, into
  whatever runs the command (an agent's transcript, for example).
- **The source label** (`usb`, `wifi`, `test-card`) is the last link any status named during the
  run. During a link switch it can name the link the grab did not use; `test-card` appears only
  when no link was seen, which is a replay.
- **An older `serve`** sends no `kind`; the command then reports every grab error as exit 5.
  Restart `serve` after updating inkboard.
- The rules in `docs/architecture.md` are reworded, not dropped: a grab still starts only from the
  Origin-checked WebSocket; a frame is served to inkboard's own page or command; it reaches the
  disk only through the command, at a path the user names.
- ADR 0002 stays in force for how the screen is read.
