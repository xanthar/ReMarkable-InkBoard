# Device setup (reMarkable 2)

This runbook sets up a reMarkable 2 and a Windows PC for inkboard, and repeats the setup after a
factory reset, on a new PC or on a new tablet. Every command is PowerShell 7.3 or later. The
sections are numbered D0–D8, and inkboard's own messages refer to them (for example "see
docs/operations/device-setup.md, D3b").

Two rules run through it:

- **Every change on the tablet is a deliberate step you take yourself**, never something a script
  does on its own: the commands that write to the tablet are marked as such.
- **Take a full verified backup (D8) before any change on the tablet.**

Use the Windows OpenSSH client for every step (`C:\Windows\System32\OpenSSH\…`): Git's ssh is often
also on PATH, and it behaves differently.

## Reference values

Measured on a reMarkable 2. Your tablet should show the same values; `inkboard probe` (D4) checks
the pen values on yours.

| Item | Value |
|---|---|
| Firmware | measured on 3.24.0.149 and 3.29.0.153. Read it from `REMARKABLE_RELEASE_VERSION` in `/usr/share/remarkable/update.conf`; `/etc/os-release` `VERSION_ID` is the OS image, not the firmware |
| USB link | the tablet is `10.11.99.1`, the PC gets `10.11.99.2` (the `10.11.99.0/29` link is the same on every tablet) |
| Pen device | `/dev/input/event1` ("Wacom I2C Digitizer"; tip and eraser end) |
| Input record size | 16 bytes |
| Ranges | X 0–20967, Y 0–15725, pressure 0–4095 |
| Orientation | `swap_xy,flip_y` (four-corner fit error 0.064–0.095; the next best orientation scores above 3) |
| Horizontal view | reMarkable's landscape mode is the portrait page turned a quarter turn clockwise (`HORIZONTAL_CLOCKWISE = true` in `static/app.js`) |
| Lag | none visible at `INKBOARD_BATCH_MS=16` |
| Screen memory | xochitl keeps the screen in the anonymous map right after `/dev/fb0` in `/proc/<pid>/maps`; the frame starts **2,629,640** bytes in (3.24.0.149 and 3.29.0.153) |
| Screen frame | 1404 × 1872 BGRA, portrait, alpha 255 on every pixel; colour on 3.29; in landscape mode the UI is drawn turned inside the same portrait frame |
| Screen grab | SSH connect 0.44–0.60 s; one frame with `gzip -1` about 0.8–1.0 s over USB and 0.87–1.04 s over Wi-Fi; a blank page is 11–16 KB on the wire |
| Screen layouts | right-handed toolbar: round button at (36, 36) portrait / (36, 1796) landscape, close X at (1328, 36) portrait / (36, 36) landscape, strip x 0–112 portrait / y 1760–1872 landscape (frame pixels) |
| Link switch | cable out to Wi-Fi in about 2 s; cable in back to USB within a second of Windows bringing the cable network up (which can take up to about 30 s) |
| eMMC | `mmcblk2` 7.65 GB (p1 vfat, p2 the running root, p3 the inactive root, p4 `/home`) plus `mmcblk2boot0` and `mmcblk2boot1`, 2 MB each; a full backup (D8) takes about 9 minutes and 440 MB |
| busybox | `pidof` has no `-s`; shell arithmetic is 64-bit; `gzip` present |

These values are the defaults in `src/inkboard/core/config.py`, `services/screen.py`
(`FRAME_OFFSET`, `SUPPORTED_FIRMWARE`, `LAYOUTS`) and `static/app.js`.

## D0 — prepare

- Charge and boot the tablet, and note the firmware version (Settings → General → About).
- Open a **new blank notebook**: the tablet's own app records every stroke into the open page.

## D1 — reachability over USB

```powershell
Test-NetConnection 10.11.99.1 -Port 22
```

If it fails, wake the tablet first: an asleep tablet does not answer over USB. Then check the USB
network adapter and replug the cable:

```powershell
Get-NetAdapter | Where-Object InterfaceDescription -like '*RNDIS*'
```

## D2 — first login and host key

The root password is on the tablet under Settings → Help → Copyrights and licenses. Accept the host
key when asked; it is stored under the name `remarkable`, which every later connection is checked
against:

```powershell
C:\Windows\System32\OpenSSH\ssh.exe -o HostKeyAlias=remarkable root@10.11.99.1 "uname -a; cat /proc/bus/input/devices"
```

Firmware 3.24 and later offer ed25519, so no `ssh-rsa` options are needed. If an older firmware
fails with `no matching host key type found. Their offer: ssh-rsa`, add
`-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa` here and the same two lines to
the host block in D3.

## D2b — a raw capture as insurance

Before anything else touches a new tablet, keep some real pen bytes on the PC. Draw while this runs
(3000 events is a few seconds of drawing), then copy the file back byte for byte:

```powershell
C:\Windows\System32\OpenSSH\ssh.exe -o HostKeyAlias=remarkable root@10.11.99.1 "dd bs=16 if=/dev/input/event1 of=/tmp/raw.bin count=3000"
New-Item -ItemType Directory -Force recordings | Out-Null
C:\Windows\System32\OpenSSH\scp.exe -o HostKeyAlias=remarkable root@10.11.99.1:/tmp/raw.bin recordings\raw-d2b.bin
```

Run it from the repository folder; `recordings/` is git-ignored, because a capture is handwriting.
If `dd` fails with "Invalid argument", the kernel uses 24-byte records: repeat with `bs=24` and set
`INKBOARD_EVENT_SIZE=24`.

## D3 — key login

Create a key without a passphrase (inkboard runs SSH in batch mode; `''` is an empty argument in
PowerShell 7.3 and later), then prove it has none — the second command prints the public key only
when the passphrase is empty:

```powershell
C:\Windows\System32\OpenSSH\ssh-keygen.exe -t ed25519 -N '' -f $env:USERPROFILE\.ssh\id_ed25519_remarkable -C inkboard
C:\Windows\System32\OpenSSH\ssh-keygen.exe -y -P '' -f $env:USERPROFILE\.ssh\id_ed25519_remarkable
```

Install it on the tablet byte for byte (scp, then strip any Windows line ending). This writes to
the tablet:

```powershell
C:\Windows\System32\OpenSSH\scp.exe -o HostKeyAlias=remarkable $env:USERPROFILE\.ssh\id_ed25519_remarkable.pub root@10.11.99.1:/tmp/inkboard.pub
C:\Windows\System32\OpenSSH\ssh.exe -o HostKeyAlias=remarkable root@10.11.99.1 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && tr -d '\r' < /tmp/inkboard.pub >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && rm /tmp/inkboard.pub"
```

Add the host block to `~/.ssh/config`. If the file already exists, copy it somewhere safe first:

```powershell
Copy-Item $env:USERPROFILE\.ssh\config "$env:USERPROFILE\.ssh\config.backup-$(Get-Date -Format yyyy-MM-dd)"
```

```
Host remarkable
    HostName 10.11.99.1
    User root
    HostKeyAlias remarkable
    IdentityFile ~/.ssh/id_ed25519_remarkable
    IdentitiesOnly yes
```

Check it:

```powershell
C:\Windows\System32\OpenSSH\ssh.exe -G remarkable | Select-String "^(hostname|user|identityfile|hostkeyalias) "
C:\Windows\System32\OpenSSH\ssh.exe -o BatchMode=yes remarkable "echo ok"
```

The key belongs to the `remarkable` entry only: `ssh root@10.11.99.1` still asks for the password.
Use `ssh remarkable`.

## D3b — SSH over Wi-Fi (optional; changes the tablet)

Do this only if you want inkboard to work without the cable, and only after a D8 backup. inkboard
needs nothing more in `~/.ssh/config` for Wi-Fi: it connects to the `remarkable` entry with the
tablet's address and the host-key options in its own command line (ADR 0003).

**How the firmware does it.** `/usr/bin/rm-ssh-over-wlan on|off` creates or deletes the marker file
`/home/root/.config/remarkable/rm_enable_ssh_wifi_marker` and restarts `dropbear-wlan.socket`,
which listens on port 22 on `wlan0` only while the marker exists. Without an argument the script
prints its help.

**Turn password login off for Wi-Fi first.** By default the Wi-Fi listener accepts the root
password. `dropbear-wlan@`, `dropbear-usb0@` and `dropbear-usb1@` are all aliases of one template,
`/usr/lib/systemd/system/dropbear@.service`, so a drop-in under the Wi-Fi alias name would reach USB
too and take away the password login you need as a way back in. Instead, a real unit file named
`dropbear-wlan@.service` in `/etc/systemd/system` replaces the alias for Wi-Fi alone. It ships in
this repository as [`contrib/dropbear-wlan@.service`](../../contrib/dropbear-wlan@.service) (written
for firmware 3.29; on another firmware compare it with `systemctl cat dropbear@.service` first and
copy that `ExecStart` with `-s` added).

1. **Install the unit file** (writes to the tablet):

   ```powershell
   C:\Windows\System32\OpenSSH\scp.exe contrib\dropbear-wlan@.service remarkable:/etc/systemd/system/dropbear-wlan@.service
   C:\Windows\System32\OpenSSH\ssh.exe -o BatchMode=yes remarkable 'systemctl daemon-reload'
   ```

2. **Prove it before anything else** (reads only). The Wi-Fi instance must name the new file and
   show `-s`; both USB instances must name `/usr/lib/systemd/system/dropbear@.service` and show no
   `-s`:

   ```powershell
   C:\Windows\System32\OpenSSH\ssh.exe -o BatchMode=yes remarkable 'for u in dropbear-wlan@x dropbear-usb0@x dropbear-usb1@x; do echo == $u; systemctl show $u.service -p FragmentPath -p DropInPaths -p ExecStart; done'
   ```

   If a USB instance shows `-s`, stop: remove the file, run `systemctl daemon-reload`, and find out
   why before going on.

3. **Turn SSH over Wi-Fi on** (writes to the tablet), then check the port at the tablet's Wi-Fi
   address (shown by your router, or by `ip -4 addr show wlan0` on the tablet):

   ```powershell
   C:\Windows\System32\OpenSSH\ssh.exe -o BatchMode=yes remarkable 'rm-ssh-over-wlan on'
   Test-NetConnection <tablet address> -Port 22
   ```

4. **Prove the result** (reads only). Key login over Wi-Fi prints `inkboard-ok`; a password is
   refused over Wi-Fi (`Permission denied (publickey).`) and still offered over USB
   (`Permission denied (publickey,password).`); `known_hosts` is unchanged:

   ```powershell
   $ssh = 'C:\Windows\System32\OpenSSH\ssh.exe'
   $opts = @('-o','BatchMode=yes','-o','HostKeyAlias=remarkable','-o','StrictHostKeyChecking=yes','-o','CheckHostIP=no','-o','UpdateHostKeys=no')
   & $ssh @opts -o HostName=<tablet address> remarkable 'echo inkboard-ok'
   & $ssh -o BatchMode=yes -o PreferredAuthentications=password -o PubkeyAuthentication=no -o HostName=<tablet address> remarkable 'echo x'
   & $ssh -o BatchMode=yes -o PreferredAuthentications=password -o PubkeyAuthentication=no -o HostName=10.11.99.1 remarkable 'echo x'
   (Get-FileHash $env:USERPROFILE\.ssh\known_hosts -Algorithm SHA256).Hash
   ```

   With an empty `known_hosts` the same connection prints `Host key verification failed.`, and
   with `-v` there is no `Offering public key` line: a wrong host key stops ssh before the login key
   leaves the PC.

5. **Tell inkboard your tablet's Wi-Fi MAC**, which its network search needs, in `.env`:

   ```powershell
   C:\Windows\System32\OpenSSH\ssh.exe -o BatchMode=yes remarkable 'cat /sys/class/net/wlan0/address'
   ```

   Then set `INKBOARD_TABLET_MAC=<the address it printed>` in `.env` (see `.env.example`). A DHCP
   reservation in your router keeps the tablet's address stable; inkboard does not need one.

**Off again:** `rm-ssh-over-wlan off`, then remove the unit file and run `systemctl daemon-reload`
(see Rollback).

## D4 — probe and calibrate

```powershell
uv run inkboard probe
```

Probe works in four steps:

1. It lists the input devices and finds the Wacom digitizer.
2. It measures the record size while you move the pen. Hovering is enough; it waits up to 20 s
   (`--wait`) and stops as soon as it has enough data.
3. With the tablet in portrait and the page text upright, it asks for a tap just inside each corner
   in turn: top-left, top-right, bottom-right, bottom-left. It prints a fit error; below 0.5 is a
   clear answer (the measured runs scored 0.064 and 0.095). A higher score means a tap landed off
   its corner, or the tablet was not in portrait: run probe again.
4. It checks the observed values against the known ranges.

It ends with three lines:

```
INKBOARD_PEN_DEVICE=/dev/input/event1
INKBOARD_EVENT_SIZE=16
INKBOARD_ORIENTATION=swap_xy,flip_y
```

If they differ from the defaults, put them in `.env`. If probe warns about values above the known
ranges, stop: that is a different digitizer, not a calibration.

Cross-check the decoder against the D2b capture:

```powershell
uv run inkboard serve --source replay --replay recordings\raw-d2b.bin
```

## D5 — the regression fixture

`tests/fixtures/rm2-sample.bin` is a recording with the tablet in portrait: a circle, a square and
the word "inkboard" (11 strokes). `tests/test_rm2_sample.py` checks that it decodes and draws
upright with the default orientation. A replacement must hold only those three things (it is
committed) and its timestamps should be shifted to start at 1000 s, as the current one is
(`tests/fixtures/README.md`):

```powershell
uv run inkboard record --out tests/fixtures/rm2-sample.bin --seconds 30
```

## D6 — live check

```powershell
uv run inkboard serve
```

Open `http://127.0.0.1:8000` and check:

- (a) Strokes appear live, upright, with the width following pressure.
- (b) Let the tablet sleep, then wake it: the chip goes Down → Connecting → Connected and strokes
  return within seconds.
- (c) Stop the server, move the pen once, then check that no reader is left on the tablet (expect
  no output): `C:\Windows\System32\OpenSSH\ssh.exe remarkable "pidof dd"`. A Screen grab also runs
  `dd`, for under a second, and ends by itself.
- (d) If writing visibly trails the pen, lower `INKBOARD_BATCH_MS`.
- (e) **Horizontal view:** put the tablet in landscape mode, switch the page to Horizontal and
  draw. If strokes come out upside down, flip `HORIZONTAL_CLOCKWISE` in `static/app.js`.
- (f) **USB and Wi-Fi** (after D3b):
  1. Cable in: the chip shows **Connected · USB**; draw; Screen works.
  2. Unplug: Connecting → **Connected · Wi-Fi** with the tablet's address, within about 2 s; draw;
     Screen works; no visible lag.
  3. Replug: back to USB without a reload, once Windows has the cable network up again.
  4. Draw through an unplug: the stroke ends, and no line joins it to the next.
  5. Plug and unplug three times within 10 s, cable in at the end: no flapping.
  6. On Wi-Fi, let the tablet sleep for a minute, then wake it: it reconnects by itself; the chip
     says `connection lost`, then `no answer (tablet asleep?)`.
  7. The search, with nothing remembered (needs `INKBOARD_TABLET_MAC`): stop `serve`, delete
     `link.json` (README, Settings), cable out, start `serve`. The log shows `link.search …
     found=1`. Windows usually still knows the tablet's address, so the answer often comes from
     its neighbour cache without any search traffic.
  8. A dead address is skipped: choose an unused address on your network, check that
     `Test-NetConnection <unused address> -Port 22` fails, delete `link.json`, set
     `INKBOARD_WIFI_ADDRESSES=<unused address>,<tablet address>` in `.env`, and start `serve` with
     the cable out: the tablet connects after a 3 s wait on the unused address.
- (g) **Image export:** draw a small diagram with a few words, press **Copy image**, paste it into
  an agent chat and ask what is drawn and written; then press **Save PNG** and open the file: 1404 ×
  1872 (or 1872 × 1404 in Horizontal), black ink on white. Pasting into Claude Code in a Windows
  terminal needs **Alt+V**.
- (h) **Screen view:** blank notebook page, page on **Screen**. Draw a few shapes: a new picture
  about 2 s after each lift of the pen, none during one long stroke, the tablet's own ink smooth.
  Erase with the toolbar Eraser: Screen shows it. Save PNG in Screen and in Sketch: the drawing sits
  in the same place. Turn the tablet to landscape and click Vertical → Horizontal: the picture
  updates by itself. Unplug: with SSH over Wi-Fi on, the link moves to Wi-Fi and Screen fetches a
  fresh picture; without it, the chip says Down and the last picture stays until you replug. Hide
  the tab and draw: no grab until it is shown again. Copy image with Copy menu off: the tablet's
  menu is painted white.

## D7 — screen offset and menu layouts after a firmware update

The Screen view reads one frame from xochitl's memory at a measured offset and accepts only the
firmware versions it was measured on ([docs/architecture.md](../architecture.md), "Screen"). After
an update it says "supports firmware …" until the new version is measured; an offset that moved
inside a known version shows "the frame does not look like a screen image". Everything here only
reads the tablet. Keep a **blank notebook page** open: the frame shows whatever is on screen.

1. Version and memory map:

   ```powershell
   C:\Windows\System32\OpenSSH\ssh.exe -o BatchMode=yes remarkable 'grep RELEASE_VERSION /usr/share/remarkable/update.conf; set -- $(pidof xochitl); grep -A1 /dev/fb0 /proc/$1/maps'
   ```

2. Save the script below as `$env:TEMP\screen_check.py` and run it from the repository folder with
   `uv run python $env:TEMP\screen_check.py`. It reads a slightly wide window around the measured
   offset, finds where an all-opaque frame starts and which layout it recognises. It prints
   numbers only and keeps the frame in memory.

   ```python
   """Re-measure the Screen view after a firmware update (read-only on the tablet)."""

   import gzip
   import re
   import subprocess
   import sys

   from inkboard.core.config import Settings
   from inkboard.services.screen import FRAME_BYTES, FRAME_OFFSET, FRAME_PIXELS, detect_layout, icon_mask
   from inkboard.services.sources import ssh_base_argv

   base = FRAME_OFFSET - 4096
   remote = (
       "set -- $(pidof xochitl); pid=$1; "
       "a=$(grep -A1 /dev/fb0 /proc/$pid/maps | tail -n1 | cut -d- -f1); "
       f"{{ dd bs=1 skip=$((0x$a + {base})) count=0 && dd bs={FRAME_BYTES + 8192} count=1; }} "
       "< /proc/$pid/mem 2>/dev/null | gzip -1"
   )
   run = subprocess.run([*ssh_base_argv(Settings()), remote], capture_output=True, timeout=30, check=False)
   raw = gzip.decompress(run.stdout)
   alpha = raw[3::4]
   print("ssh exit", run.returncode, "bytes read", len(raw))
   # The frame is the run of opaque pixels (alpha 255) at least one frame long.
   edges = [-1, *(m.start() for m in re.finditer(rb"[^\xff]", alpha)), len(alpha)]
   runs = [(a + 1, b - a - 1) for a, b in zip(edges, edges[1:], strict=False) if b - a - 1 >= FRAME_PIXELS]
   if not runs:
       sys.exit("no all-opaque frame near the old offset: the memory layout changed - stop and investigate")
   start, length = runs[0]
   frame = raw[4 * start : 4 * start + FRAME_BYTES]
   layout = detect_layout(frame)
   print("frame offset", base + 4 * start, "- in code:", FRAME_OFFSET, "- opaque run", length, "of", FRAME_PIXELS)
   print("layout", layout.name if layout else "NOT RECOGNISED")
   for x, y in ((36, 36), (1328, 36), (36, 1796)):
       print("icon box", (x, y), icon_mask(frame, x, y).hex()[:16])
   ```

3. Accept when the layout is recognised: `portrait-open` with the toolbar open in portrait (the
   icons line up only at the true offset, which matters when the opaque run is longer than one
   frame). Repeat with the toolbar closed and in landscape mode.
4. Change `SUPPORTED_FIRMWARE` (and `FRAME_OFFSET` if it moved) in
   `src/inkboard/services/screen.py`. If a layout is not recognised, the icons changed: take the new
   40 × 40 masks from the frame (`icon_mask(frame, x, y).hex()` in full) into `MASKS`. Update the
   tests in `tests/test_screen.py`, run the gates (CONTRIBUTING.md) and check the Screen view live
   (D6 h). A pull request with the new version is welcome.
5. Delete `$env:TEMP\screen_check.py`.

## D8 — full tablet backup

**Take one before every change on the tablet** (D3, D3b, anything written outside `/tmp`). One
backup covers the changes made in the same sitting. Everything here only reads the tablet. Plug the
cable in and keep the tablet awake for about ten minutes (tap the blank page now and then):

```powershell
uv run python scripts/tablet_backup.py "$env:USERPROFILE\Backups\remarkable\$(Get-Date -Format yyyy-MM-dd)-<label>"
```

The script works in `<dest>.partial-<time>` and renames it to `<dest>` only when every check passed.
It refuses a `<dest>` that already exists or that lies inside a git repository, creates the folder
readable by its owner only (on Linux and macOS), and needs the storage's raw size plus 5 GB free on
the target drive (about 12.7 GB; the backup itself is about 440 MB). What it holds and checks is in
the script's docstring and in the `README.txt` it writes: a raw image of `mmcblk2` and both boot
areas, a file copy of `/home` and `/etc` with the tablet's own file list and SHA-256 sums, and
`SHA256SUMS` over all of it. Partitions mounted read-write (p1, p2, p4) change while the tablet
runs, so their image hash is reported rather than required; p4 is checked through the file copy.

Check a backup later (Git for Windows ships `sha256sum`):

```powershell
Set-Location "$env:USERPROFILE\Backups\remarkable\<backup folder>"
& 'C:\Program Files\Git\usr\bin\sha256sum.exe' -c SHA256SUMS
```

Every line must end in `OK`.

**Restore one file.** The script streams one member of the tar into `/tmp/restore-test` on the
tablet (a RAM disk; this writes to the tablet) and prints its SHA-256; moving it into place is a
separate ssh command you run yourself:

```powershell
uv run python scripts/tablet_backup.py "$env:USERPROFILE\Backups\remarkable\<backup folder>" --restore-test etc/hostname
C:\Windows\System32\OpenSSH\ssh.exe -o BatchMode=yes remarkable 'sha256sum /tmp/restore-test/etc/hostname /etc/hostname'
C:\Windows\System32\OpenSSH\ssh.exe -o BatchMode=yes remarkable 'rm -r /tmp/restore-test'
```

The disk images are the last resort: writing them back needs the tablet's USB recovery mode, which
this project has not tested.

**A backup holds secrets** (Wi-Fi passwords, cloud tokens, SSH host keys) **and every notebook.**
Keep it on your PC; never sync, share or commit it.

## Troubleshooting

| Symptom (page detail or ssh error) | Cause | Fix |
|---|---|---|
| `Connection timed out` / `No route to host` | tablet asleep, unplugged, or no USB network adapter | wake the tablet; a running server reconnects by itself. Otherwise replug and check the RNDIS adapter (D1) |
| Chip `Down - connection lost (retrying)` | a running stream ended: cable out, tablet asleep, Wi-Fi gone (ssh's own line is in the server log, `source.down`) | nothing; inkboard tries USB, then Wi-Fi, by itself. Wake the tablet if it slept |
| Chip `USB: no answer (tablet asleep?)` / `<address>: no answer (tablet asleep?)` | the tablet sleeps (the PC keeps its cable address while the tablet sleeps on the cable) | wake it |
| Chip `… no Wi-Fi search while the USB cable is in` | the cable is in, so inkboard does not search the network | wake the tablet; it connects over USB |
| Chip `… Wi-Fi search off - set INKBOARD_TABLET_MAC` | no tablet MAC is configured, so inkboard never searches | set `INKBOARD_TABLET_MAC` in `.env` (D3b step 5), or list the address in `INKBOARD_WIFI_ADDRESSES` |
| Chip `… next Wi-Fi search in N min` | the search runs once when the tablet is lost, then after 5, 15 and 60 minutes, to keep the network quiet | wake the tablet: the remembered address is tried every few seconds; or plug in once |
| Chip `… searched <network> - tablet not found` | the tablet sleeps on this network (its known address is in the searched network) | wake it |
| Chip `… tablet not found (guest or company Wi-Fi may block devices from reaching each other)` | client isolation on the Wi-Fi, the tablet on another network, or asleep on a network inkboard has not seen it on | plug in once (the tablet then reports its Wi-Fi address), or use the cable; on a guest network, use another network |
| Chip `… too large to search (over /22) - plug in once or set INKBOARD_WIFI_ADDRESSES` | the PC's network has more than 1,022 addresses, so inkboard does not search it | plug in once, or set `INKBOARD_WIFI_ADDRESSES` to the tablet's address |
| Chip `… routed through a VPN, not searched` | the VPN carries the local network's traffic, so a search would leave the PC | turn the VPN off, or plug in |
| Chip `Wi-Fi search unavailable (… failed)` | PowerShell or `arp` could not run (policy, or not Windows) | list the address in `INKBOARD_WIFI_ADDRESSES`, or plug in once |
| Chip `<address>: SSH refused - SSH over Wi-Fi may be off on the tablet` / `tablet found at <address> but SSH over Wi-Fi is off` | the marker is gone (`rm-ssh-over-wlan off`, a reset) | D3b |
| Chip `<address>: different SSH host key - not the tablet, or the tablet after a firmware update` | another device has the tablet's old address, or a firmware update changed the key; the address is skipped for 10 minutes and the login key was never offered | plug in; if USB also refuses the key, see `REMOTE HOST IDENTIFICATION HAS CHANGED` below |
| Cable back in, but the chip stays on Wi-Fi for up to about 30 s | Windows is bringing the cable network back | wait; `Get-NetIPAddress -AddressFamily IPv4 \| Where-Object IPAddress -like '10.11.99.*'` shows when it is back |
| `ssh root@10.11.99.1` asks for a password | the key belongs to the `remarkable` entry | use `ssh remarkable` |
| `Permission denied (publickey)` | the key is not in `authorized_keys`, or another key is used | redo D3; `ssh -G remarkable` shows which key is used |
| `no matching host key type … ssh-rsa` | an old SSH server on an older firmware | add the two `+ssh-rsa` lines to the `remarkable` block only (D2) |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | a factory reset or a firmware update (3.24 to 3.29 changed the key) | confirm it really is your tablet, over the USB cable (`10.11.99.1`), then `ssh-keygen -R remarkable` and `ssh -o StrictHostKeyChecking=accept-new remarkable "echo ok"`; if the key login also fails, redo D3 |
| Connected, but nothing draws | wrong input device | `uv run inkboard probe`; set `INKBOARD_PEN_DEVICE` |
| `frame misalignment` | wrong record size or device | `uv run inkboard probe`; set `INKBOARD_EVENT_SIZE` |
| Strokes rotated or mirrored | the page view does not match the tablet's mode, or the orientation is wrong | switch Vertical / Horizontal; otherwise `uv run inkboard probe` and set `INKBOARD_ORIENTATION` |
| Probe: "the taps match no orientation" | a tap off its corner, or the tablet not in portrait | run probe again, tapping each corner after its prompt |
| `pidof dd` shows a reader after the server stopped | a clean stop leaves one until the next pen movement; one from a connection that broke while the tablet slept lingers until the tablet's TCP stack gives up | harmless: it only reads the pen, and exits on the next pen movement once that connection is closed |
| `[Errno 10048]` address in use | another local server on port 8000 | `Get-NetTCPConnection -LocalPort 8000 -State Listen`; or `uv run inkboard serve --port 8010` |
| Page loads, status stays "Starting…" | the browser kept an old cached script | reload with Ctrl+Shift+R |
| Page loads, WebSocket refused | the page was opened from another origin | use `http://127.0.0.1:8000` or `http://localhost:8000` |
| Erasing on the tablet draws a line in Sketch | with the standard Marker the toolbar Eraser is used with the pen tip, which the pen reports as an ordinary stroke | switch to **Screen**, which shows the erasure; or press **Clear** and draw again |
| Copy image and Save PNG greyed out, tooltip "Nothing to export yet" | Sketch: no pen strokes on the page (eraser strokes do not count) | draw first |
| Copy image and Save PNG greyed out, tooltip "No screen frame yet" | Screen: no picture has arrived | wait about 2 s, or press **Refresh** |
| Screen shows an old picture after a page turn or a menu | finger actions are not in the pen stream, so they do not trigger a grab | press **Refresh** |
| Screen picture turned the wrong way | the page view does not match the tablet's mode | switch Vertical / Horizontal; the switch also fetches a fresh picture |
| "Screen: Screen view supports firmware 3.24 and 3.29; the tablet has …" | a firmware version that was never measured; nothing is read | measure it (D7) |
| "Screen: the frame does not look like a screen image - re-measure (see docs/operations/device-setup.md, D7)" | the memory offset moved inside a known version | measure it (D7) |
| "Screen: screen memory not found" / "screen memory could not be read" / "unexpected frame size" | xochitl's memory layout changed | measure it (D7) |
| "Screen: the tablet app is not running (restarting?)" | xochitl is restarting (after an update, or a crash) | wait, then **Refresh** |
| "Screen: the tablet did not answer within 10 s" | the tablet is asleep or busy | wake it, then **Refresh** |
| Notice "Copied - menu kept" (hover: "menu layout not recognised") | Copy menu is off but no known toolbar layout was found: a pop-up over the toolbar, the file library, or the left-handed layout | close the pop-up and copy again, or keep the menu in the image |
| Copy image greyed out, tooltip "This browser cannot copy images" | the browser cannot put images on the clipboard | use Save PNG, or a current Chrome or Edge |
| "✕ Copy failed - use Save PNG" (hover it for the error name) | the page was not focused, or it was opened from an address other than 127.0.0.1 / localhost | click the page and press Copy image again; open `http://127.0.0.1:8000`; or use Save PNG |
| Pasting into Claude Code in a terminal does nothing | on Windows, Ctrl+V pastes text only | press **Alt+V** |

## After a tablet firmware update

Over the cable, in this order, before trusting live use:

1. `C:\Windows\System32\OpenSSH\ssh.exe -o BatchMode=yes remarkable "echo ok"`: expect a changed
   host key (see the troubleshooting table), and replace it only after confirming it is your
   tablet.
2. `uv run inkboard probe` (D4).
3. D7 for the Screen view.
4. D8, a new backup, before the next step changes the tablet.
5. D3b steps 1–2, if you use SSH over Wi-Fi: the update replaces `/etc`, which removes the Wi-Fi
   unit file, while the marker in `/home` survives, so **SSH over Wi-Fi comes back with password
   login** until the unit file is installed again. Install and prove it at once, or turn SSH over
   Wi-Fi off first (`rm-ssh-over-wlan off`).
6. D3b step 3 if you turned it off, then the step 4 proofs.

## Rollback

The D8 backup restores any file.

- **SSH over Wi-Fi** (in this order): `rm-ssh-over-wlan off`; then
  `rm /etc/systemd/system/dropbear-wlan@.service` and `systemctl daemon-reload`, each as its own
  command; then delete inkboard's remembered address (`link.json`, see README, Settings). USB keeps
  working throughout.
- **Key login:** on the tablet, remove the `inkboard` line from `/home/root/.ssh/authorized_keys`.
  On the PC, `ssh-keygen -R remarkable`, remove the `Host remarkable` block from `~/.ssh/config`
  (or restore your backup of it), and delete the `id_ed25519_remarkable` key pair.
