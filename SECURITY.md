# Security policy

## Supported versions

| Version | Supported |
|---|---|
| latest release | yes |

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub:
open <https://github.com/xanthar/ReMarkable-InkBoard/security/advisories/new> ("Report a
vulnerability"). If you cannot use GitHub, e-mail moppleit@hotmail.com.

Include what you found, the steps to reproduce it, and what an attacker could do with it. You will
get an answer within a week; a fix follows as soon as it is understood, and you are credited in
the advisory unless you prefer not to be.

## What inkboard protects, and how

inkboard handles two sensitive things: the user's handwriting, and root SSH access to their
reMarkable 2. The design keeps both on the user's own PC.

- **The page is local only.** The server binds to `127.0.0.1` and has no option to bind
  elsewhere. The WebSocket accepts only the page's own origins (`http://127.0.0.1:<port>`,
  `http://localhost:<port>`), any other `Host` header is refused (DNS rebinding), and every
  response carries a strict Content Security Policy and framing headers. Another website open in
  the same browser can neither read the strokes nor start a screen read.
- **Handwriting is never logged.** Coordinates, pressure and screen frames are dropped from the
  logs, and errors print without the values of local variables; the logs do carry network
  addresses and ssh's own messages. A screen frame is held in memory only while a page shows the
  Screen view, never written to disk by the server, and served with `no-store` to the page's own
  same-origin request only, with no side effect.
- **Nothing is installed on the tablet for the pen or the screen.** inkboard only reads; every
  remote command is fixed text built in code from validated settings. Programs are started with
  an argument list, never with text for a shell to parse; the only local commands are two fixed
  Windows reads for the opt-in Wi-Fi search (`arp.exe -a`, and a fixed `Get-NetAdapter` query
  through `powershell.exe`).
- **Over Wi-Fi, the pen stream and the screen reads cross the local network** inside SSH. The
  remembered `link.json` holds two private addresses and their times, nothing else.
- **Over Wi-Fi the tablet proves who it is first.** Every Wi-Fi connection checks the tablet's
  known SSH host key with `StrictHostKeyChecking=yes` before the login key is offered; inkboard
  never writes to `known_hosts`, and an address that fails is skipped for ten minutes.
- **The network search is opt-in and quiet.** It runs only when the user has set their tablet's
  MAC, never while the USB cable is in and never over a VPN, and it contacts the SSH port only on
  the device with that MAC.

## Known limits

- **Any program on the same PC can read the strokes.** The page has no login because it never
  leaves `127.0.0.1`. That is right for a personal PC; on a shared machine, another local user or
  process can connect to the page. For the same reason there is no CSRF protection or rate
  limiting: the page has no forms, and its only actions (Clear, and switching the Screen view on,
  off or refreshing it) are WebSocket messages behind the Origin check. Serving beyond loopback
  would need all three.
- **Copy image uses the system clipboard.** With clipboard history on (on Windows, Win+V), every
  copied image stays in that history until you clear it, including Screen images, which can show
  other notebooks. The page never reads the clipboard.
- **The SSH key has no passphrase** so that inkboard can run SSH in batch mode. Use a key only for
  the tablet, tied to one host entry (see the README), or load a passphrase key into your SSH agent.
- **Turning on SSH over Wi-Fi changes the tablet.** Port 22 then answers on every network the
  tablet joins. The runbook (`docs/operations/device-setup.md`, D3b) turns password login off for
  Wi-Fi; a firmware update turns it back on until the step is repeated.
- **A tablet backup holds secrets** (Wi-Fi passwords, cloud tokens, SSH host keys) and every
  notebook. `scripts/tablet_backup.py` creates the folder readable by its owner only on Linux and
  macOS (on Windows it inherits the permissions of the folder you choose, normally your user
  profile) and refuses to write it inside a git repository; keep it on your PC.
