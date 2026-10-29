# Contributing to inkboard

Contributions are welcome: bug reports, measurements from other tablets and firmware versions,
documentation fixes and code. This file describes how we work on inkboard, so that a change can
be reviewed and merged without surprises. By taking part you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md), and your contributions are licensed under the project's
[MIT License](LICENSE).

## Reporting problems

Open an issue with the bug or feature template. For a security problem, do not open an issue: see
[SECURITY.md](SECURITY.md). When you paste a server log, check it first: the log never contains
handwriting, but it can contain your network addresses.

## Setting up

```powershell
git clone https://github.com/xanthar/ReMarkable-InkBoard.git
Set-Location ReMarkable-InkBoard
uv sync --locked
```

Your own settings (your tablet's MAC, its Wi-Fi address) go in a `.env` file in the repository
folder; `.env.example` lists every key. `.env` is git-ignored, and the tests never read it: an
autouse fixture in `tests/conftest.py` clears every `INKBOARD_*` variable and disables the file.

## The gates

Every change passes the same gates locally that CI runs on every push and pull request. You need
Node.js for the first line and the [gitleaks](https://github.com/gitleaks/gitleaks) binary for the
last:

```powershell
node --check src\inkboard\static\app.js
uv run ruff format --check .
uv run ruff check .
uv run mypy src/ scripts/
uv run pytest -q --maxfail=1 --cov=inkboard --cov-fail-under=98
uv run bandit -c pyproject.toml -r src/ scripts/ -ll -ii
uv run pip-audit
uv export --locked --format requirements-txt --no-emit-project -q -o $env:TEMP\inkboard-lock-export.txt
uv run pip-audit -r $env:TEMP\inkboard-lock-export.txt --require-hashes --disable-pip
Remove-Item $env:TEMP\inkboard-lock-export.txt
gitleaks git . --config .gitleaks.toml --redact --no-banner
```

CI also runs the tests on Windows, osv-scanner on the lock file and, on the public repository,
CodeQL. Coverage stays at or above 98%; the floor only ever goes up.

`ruff format --check .` also checks the Python code blocks inside Markdown files (for example the
D7 script in the runbook), so a documentation-only change can fail it. Run `uv run ruff format .`
after you edit one.

`.public-audit-allow` lists the deliberate exceptions for the maintainer's pre-publication privacy
check (for example the tablet's fixed USB address, which is the same on every reMarkable 2). Add a
line there only for a value that is the same for everyone, never for a personal one.

## Tests

The tests need no tablet and no network. A fake device (`tests/fake_device.py`) stands in for SSH,
a real recording (`tests/fixtures/rm2-sample.bin`) pins the decoder and the default orientation,
and `tests/netguard.py` fails any link or search test that tries to reach the network or start a
process.

Two rules keep the test data honest:

- **Every refusal has its control.** A test that proves something is refused also proves the
  closest legitimate case is accepted, so a refusal cannot pass by refusing everything.
- **Test data is documentation data.** MAC addresses come from `00:00:5E:00:53:xx` (RFC 7042),
  addresses from the example LAN `192.168.1.0/24`. Never use a real device, network, path or
  drawing; raw captures of your own drawing go to the git-ignored `recordings/` folder.

## Checking that the Sketch export is unchanged

A change to the page can alter the exported image in ways no unit test sees. We prove it does not
by hashing the exported pixels before and after the change, against a replay of the sample:

```powershell
uv run inkboard serve --source replay --replay tests\fixtures\rm2-sample.bin --speed 0 --port 8010
```

Open <http://127.0.0.1:8010>, reload with Ctrl+Shift+R, wait for "11 strokes", and run this in the
browser console. The first run stores a baseline per view (Vertical and Horizontal); after the
change, the same run must report `same: true` in both views.

```js
(async () => {
  let blob = null;
  const oc0 = URL.createObjectURL, ck0 = HTMLAnchorElement.prototype.click;
  URL.createObjectURL = (b) => { blob = b; return oc0.call(URL, b); };
  HTMLAnchorElement.prototype.click = function () {};
  try {
    document.getElementById("save").click();
    for (let i = 0; i < 60 && !blob; i++) await new Promise(r => setTimeout(r, 50));
    const bmp = await createImageBitmap(blob);
    const oc = new OffscreenCanvas(bmp.width, bmp.height);
    const x = oc.getContext("2d"); x.drawImage(bmp, 0, 0);
    const d = x.getImageData(0, 0, bmp.width, bmp.height).data;
    const h = [...new Uint8Array(await crypto.subtle.digest("SHA-256", d))].map(b => b.toString(16).padStart(2, "0")).join("");
    const key = "export." + document.getElementById("sheet").dataset.view;
    const base = localStorage.getItem(key);
    if (!base) localStorage.setItem(key, h);
    return { w: bmp.width, h: bmp.height, prefix: h.slice(0, 8), same: base ? base === h : "baseline" };
  } finally { URL.createObjectURL = oc0; HTMLAnchorElement.prototype.click = ck0; }
})()
```

Take baselines only after the replay has finished: a page that connects while the replay is still
running can receive a few points twice.

## Supporting a new firmware version

The Screen view reads the screen at a measured memory offset and refuses firmware it was not
measured on. To add a version, follow section D7 of
[docs/operations/device-setup.md](docs/operations/device-setup.md) on a tablet with that firmware,
then change `SUPPORTED_FIRMWARE` (and `FRAME_OFFSET` if it moved) in
`src/inkboard/services/screen.py`, extend `tests/test_screen.py`, and say in the pull request
which tablet and firmware you measured on.

## Screenshots

`docs/images/inkboard.png` (the README) and `docs/images/social-preview.png` (the repository's
1280 × 640 social preview) are screenshots of the page. When you retake one, connect over the USB
cable so the chip shows no network address, and draw nothing personal.

## Commits and pull requests

- Work on a branch and open a pull request against `main`; CI must be green.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat`, `fix`, `docs`, `test`, `refactor`, `build`, `chore`) and say why, not only what.
- Update `CHANGELOG.md` (Keep a Changelog) in the same pull request.
- Keep personal values out: no `.env`, no `*.local.md`, no real addresses or device identifiers.
  CI refuses tracked local-only files.

## Architecture and decisions

[docs/architecture.md](docs/architecture.md) describes how the pieces fit, the exact image export
format and the rules the code keeps (localhost only, no handwriting in logs, the SSH argv built
from validated settings). [docs/adr/](docs/adr/) records the decisions behind them. If you work
with an AI coding agent, [CLAUDE.md](CLAUDE.md) gives it the same contract.
