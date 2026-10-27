# ADR 0002 — Read the tablet's screen from xochitl's memory over SSH, read-only

- **Status:** accepted
- **Date:** 2026-10-26
- **Decided by:** the maintainer

## Context

inkboard's live view rebuilds the drawing from pen events (ADR 0001). Much of what a user does
on the tablet never reaches the pen stream: the toolbar Eraser used with the tip (it arrives as
an ordinary pen stroke), selections, moved or deleted content, text, page turns and menus.
With the standard Marker there is no eraser end the pen stream could report. To hand an agent
what is really on the tablet, the page needs the tablet's own screen image, without giving up
the instant stroke view.

The rM2 has no public screen API, and `/dev/fb0` reads empty. reMarkable's app, xochitl, keeps
the screen image in its own memory. Since firmware 3.23 / 3.24 it is a 1404 × 1872 ABGR32 frame
(in memory the bytes B, G, R, A) in the anonymous map that follows the `/dev/fb0` map, at a fixed
offset. The community found where it lives
([goMarkableStream issue #140](https://github.com/owulveryck/goMarkableStream/issues/140),
[reSnap PR #27](https://github.com/cloudsftp/reSnap/pull/27)); inkboard measured the offset on
its own (two pixels later than reSnap's) and shares no code with either project.

Options considered:

1. **Read the frame over the existing SSH login:** `dd` from `/proc/<xochitl pid>/mem` at the
   measured offset, compressed with the tablet's busybox `gzip`, one SSH run per picture.
2. Install a streaming helper on the tablet (goMarkableStream, or reStream with its binaries).
3. The rm2fb framebuffer shim (needs an older firmware and changes the tablet).
4. Encode PNG on the server with an image library and serve it to the page.

## Decision

Option 1, as a second view (**Screen**) beside the stroke view (**Sketch**), with the page
receiving raw RGBA over loopback (not option 4: a new dependency for no gain on loopback).

- The remote command is fixed text plus two integers; it first checks the firmware version on
  the tablet (`/usr/share/remarkable/update.conf`) and exits before reading memory on a version
  that was never measured.
- Every frame must have the exact size and alpha 255 on every pixel before it is used; a moved
  offset breaks that, so a wrong read never reaches the page.
- Grabs run only while a page shows Screen: after the pen rests, on Refresh, on a view switch and
  after a reconnect, one at a time.

## Reasons

- Nothing is installed or changed on the tablet; the same key login, `known_hosts` and process
  supervision as the pen stream (ADR 0001).
- The picture is exactly what the tablet shows, so the agent sees erasing, selections and text
  as the user does.
- Fast enough for snapshots: about 0.85 s per grab over USB with `gzip -1` (0.5 s of it the SSH
  connect, which Windows OpenSSH cannot share between runs), and 0.87–1.04 s over Wi-Fi, where
  the grab follows the pen stream's link (ADR 0003).

## Consequences

- The offset and the firmware list are measurements, not an interface: a firmware update can move
  them (3.23 / 3.24 did once). Screen then refuses or reports a bad frame, Sketch keeps working,
  and the runbook's section D7 re-measures it (3.24.0.149 and 3.29.0.153 share the offset
  2,629,640).
- Not live video: a picture arrives a second or two after the pen lifts, and finger actions need
  Refresh. Live video would need option 2, a tablet change.
- A frame is handwriting-grade and shows whatever is open on the tablet, read as root: it is
  never logged or written to disk by the server, held only while a page shows Screen, and served
  only to the page's own same-origin fetch, with no caching and no side effect.
- The tablet's menu is recognised from its own icons (40 × 40 masks, right-handed toolbar), so
  Copy menu can paint it white; an unrecognised layout keeps the menu and says so.
