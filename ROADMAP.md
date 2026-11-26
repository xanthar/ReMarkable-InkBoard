# Roadmap

inkboard is for sketching ideas and rough diagrams by hand and handing them to an AI agent, which
reviews them or turns them into Mermaid, text or code. Drawing an idea is often quicker than
describing it. This file lists what is done, what comes next, and what was deliberately left out,
with the reason.

## Done (0.2.0)

- **An agent pulls the picture itself:** `inkboard snapshot --out FILE` saves the Screen view as a
  PNG from a running `serve`, with the same pixels as the page's Save PNG (pinned by a test), the
  menu left out and a landscape page turned upright; one line and an exit code tell a script or an
  agent what happened. It is a client of `serve`'s own WebSocket, and encodes the PNG itself with
  the standard library, so the server gained no image library and no new route (ADR 0004).

## Done (0.1.0)

- **Live strokes (Sketch):** pen strokes in the browser as you draw, the line width following
  pressure, upright in portrait and landscape, reconnecting by itself after the tablet sleeps.
- **Image export:** Copy image puts the drawing on the clipboard as a PNG for an agent chat, and
  Save PNG downloads it: black ink on white at the tablet's screen size, following the view.
- **Screen view:** the tablet's own screen a moment after the pen lifts, erasing, selections and
  menus included; read-only over SSH, nothing installed on the tablet; Copy menu leaves the
  toolbar out of the image (ADR 0002).
- **USB or Wi-Fi, cable optional:** the cable when it is in, the tablet's Wi-Fi when it is not,
  switching by itself; every Wi-Fi connection checks the tablet's host key first (ADR 0003).

## Next

### What changed between two pulls

An agent that pulls the page again compares the new picture with the last one by eye. A pixel
difference (what was added, what was erased) would make that exact. Deferred until a real change
is missed by eye.

## Later

- **Pen marker.** The digitizer reports the pen's position while it hovers just above the glass.
  Show it as a dot on the page, with a toggle; most useful in the Screen view, which lags the
  tablet by a second or two. The drawing and the exported image do not change.
- **Tablet on another network (VLAN).** The Wi-Fi search reads the PC's neighbour (ARP) cache,
  which only holds hosts on the PC's own subnets. A tablet on a separate network behind the router,
  such as an IoT VLAN, is never found: the remembered address is stale until the tablet is plugged
  in once (it then reports its new address) or the address is listed in `INKBOARD_WIFI_ADDRESSES`
  by hand. Option: an opt-in, configured subnet that is searched with a bounded port-22 check, the
  host key being the proof as today. It must keep the search opt-in and bounded (ADR 0003).
- **A changed host key, guided.** A firmware update or a reset gives the tablet a new SSH host key,
  and inkboard refuses it, which is right. Today the way back is the runbook's `ssh-keygen` steps
  by hand. The page could show the new fingerprint, compare it with the one read over the USB
  cable, and lead through trusting it, without weakening the check.

## Parked

| Item | Why |
|---|---|
| Eraser drawing in Sketch | Needs the **Marker Plus**, whose eraser end the pen stream reports as `rubber`; inkboard already receives those strokes and could paint them white. With the standard Marker the toolbar Eraser is used with the tip and arrives as an ordinary pen stroke, so the page cannot tell erasing from drawing. The Screen view shows erasing as the tablet does. |
| Shape cleanup | The agent reading the image turns rough shapes into a clean diagram. Revisit only if that falls short. |
| Text cleanup | The agent reads handwriting from the image. Revisit only if that falls short. |

## Deferred, with the reason

| Item | Why not now |
|---|---|
| SVG export | PNG covers the agent use; SVG waits until something needs vectors. |
| Crop the image to the drawing | The image is the full sheet, like the tablet. Revisit if an agent misreads small sketches. |
| Live video of the screen | The Screen view takes a picture when the pen rests (about 0.85 s per picture). Smooth live video needs a helper installed on the tablet (for example goMarkableStream), a change to the tablet that inkboard avoids. |
| Left-handed toolbar in Copy menu | Only the right-handed toolbar was measured; other layouts keep the menu in the image and say so. |
| Firmware versions beyond 3.24 and 3.29 for Screen | Each needs a measurement on a real tablet (runbook D7); pull requests with one are welcome. |
| `serve --open` | Opening the browser is one click. |
| Tray icon / autostart | inkboard is started when it is used. |
| Latency instrumentation | No lag was visible at the default batching of 16 ms. |
