# ADR 0001 — Read the pen device over the system OpenSSH client, in threads

- **Status:** accepted
- **Date:** 2026-10-25
- **Decided by:** the maintainer

## Context

inkboard must stream the reMarkable 2's pen events (`/dev/input/event1`, 16-byte
`input_event` frames) to a Python service on a Windows PC with low latency. The tablet offers
root SSH over USB (`10.11.99.1`) and, depending on firmware, over Wi-Fi. (The same client now
runs over either link, chosen per connection: ADR 0003.)

Options considered:

1. **System OpenSSH (`ssh.exe`) started with `subprocess.Popen`, read in threads.**
2. paramiko, as remarkable_mouse does.
3. asyncssh, or an asyncio subprocess.

## Decision

Option 1. `SshSource` starts `C:\Windows\System32\OpenSSH\ssh.exe -T -n -o BatchMode=yes …
remarkable "echo inkboard-ready >&2; exec dd bs=<size> if=<device> 2>/dev/null"` and reads
stdout unbuffered in a thread; a second thread drains stderr. Results reach the asyncio loop
through `call_soon_threadsafe`.

## Reasons

- No crypto dependency; keys, `known_hosts` and `~/.ssh/config` work exactly as in the user's own
  terminal.
- Independent of the asyncio loop type: a Selector loop on Windows cannot start subprocesses,
  and uvicorn may choose one.
- The stderr marker gives a precise "connected", and ssh's own error lines become the status
  detail on the page.

## Consequences

- inkboard supervises a child process: backoff restarts (1 → 2 → 3 s), a process group (a new
  session off Windows) so Ctrl+C reaches only inkboard, terminate/kill on stop.
- Windows PCs often have two ssh clients on PATH (Git's and the system's); the binary is a setting
  (`INKBOARD_SSH_BIN`) with the System32 client as the Windows default.
- The remote `dd` exits only when it next writes into a closed pipe, so a reader can stay on
  the tablet until the next pen movement — longer when the connection broke while the tablet
  slept. It only reads the pen node, which several readers can share.
- Key login must work without prompts (`BatchMode=yes`): a key without a passphrase scoped to
  the tablet's host block, or a passphrase key loaded into the SSH agent. Over Wi-Fi the host key
  is checked before that key is offered (ADR 0003).
