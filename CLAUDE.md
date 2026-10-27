# inkboard — contributor and agent contract

inkboard is a live drawing board for the reMarkable 2: it reads the pen over SSH and draws every
stroke in a browser page on the user's PC (**Sketch**), and shows the tablet's own screen a moment
after the pen lifts (**Screen**), over the USB cable or the tablet's Wi-Fi. One user, localhost
only. This file tells a contributor, or an AI coding agent working for one, how to change it
safely.

## Where things are

| Read | For |
|---|---|
| [README.md](README.md) | what it does, setup, settings, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | data flow, modules, export formats, **the rules the code keeps** |
| [docs/adr/](docs/adr/) | the decisions behind the pen stream, the screen read and the USB or Wi-Fi link |
| [docs/operations/device-setup.md](docs/operations/device-setup.md) | the tablet runbook (sections D0–D8; messages in the code cite them) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | the gates, the test-data rules, the Sketch-unchanged check |
| [SECURITY.md](SECURITY.md) | the threat model and how to report a problem |
| [ROADMAP.md](ROADMAP.md), [CHANGELOG.md](CHANGELOG.md) | what comes next, what changed |

## How to work here

1. **Read before changing.** Read the module, its tests and the rule in `docs/architecture.md` it
   touches. Never act on an assumption about the code or the tablet.
2. **Work on a branch**, one focused change per pull request, Conventional Commits, and the
   CHANGELOG entry in the same change.
3. **Run the gates** (CONTRIBUTING.md) before every commit. They are the same checks CI runs;
   coverage stays at or above 98%.
4. **Keep the rules** in `docs/architecture.md`: the page binds to 127.0.0.1 only, no handwriting
   or frame ever reaches a log or the disk, the SSH argv is built in code from validated settings,
   every address connection checks the host key, the network search stays opt-in and bounded, the
   screen is only read. A change that needs to break one is a design decision: write an ADR.
5. **Test honestly.** Every refusal test has its control. Test data uses documentation values only
   (MAC `00:00:5E:00:53:xx`, addresses in `192.168.1.0/24`); no real device, network, path or
   drawing. Never change a test to match changed code unless the behaviour change was intended and
   is stated.
6. **Never change a user's tablet from code or a test.** The runbook's tablet steps are run by a
   person, deliberately, after a backup (D8).

## Personal settings and notes stay local

- A user's own values (their tablet's MAC, its Wi-Fi addresses) live in a git-ignored `.env`
  (`.env.example` lists every key). The code never ships them as defaults.
- Personal agent instructions go in a git-ignored `CLAUDE.local.md` beside this file; Claude Code
  reads it right after this one.
- Planning notes and review records stay out of the repository: `docs/analysis/` is git-ignored.
- CI refuses a tracked `.env`, `CLAUDE.local.md` or `*.local.md`.

## Gotchas

- **Two ssh clients on PATH on Windows.** Git's `usr\bin\ssh.exe` comes first in Git Bash;
  inkboard defaults to `C:\Windows\System32\OpenSSH\ssh.exe` (`INKBOARD_SSH_BIN`). Use the same
  client for the runbook steps.
- **The venv's `python.exe` is a launcher** that starts a second Python process. Stop a background
  server with a targeted command, never a blanket kill:

  ```powershell
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*inkboard*serve*' | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
  ```

- **A browser that cached a bad response keeps it** after a fix (304 revalidation keeps the old
  headers): reload with Ctrl+Shift+R.
- **Starlette's test client uses `httpx2`**; plain `httpx` triggers a deprecation warning.
- **Copy image needs the page focused** and opened as 127.0.0.1 or localhost (a secure context). A
  scripted `click()` on the button has no user activation, so the clipboard write never settles.
- **Browser automation:** a Chrome screenshot can time out while the mouse rests on an element with
  a tooltip (`title`); move the mouse off first.
- **`#board { display: block }` beats the browser's `[hidden]` rule**; `styles.css` restates
  `[hidden]` for both canvases.
- **A page that connects while a replay is still running** can receive a few points twice, so take
  Sketch pixel hashes only after the replay has finished.
- **Importing `services/screen.py` costs about 0.8 s**: `tests/fake_device.py` imports it only in
  its screen modes, so a pen-mode child starts quickly.
- **`probe` and `record` use the `remarkable` host entry as written**, which is the USB cable; only
  `serve` chooses between USB and Wi-Fi.
- **A firmware update changes the tablet's SSH host key** (seen from 3.24 to 3.29) and can move the
  screen offset: see the README's firmware-update section and runbook D7.
