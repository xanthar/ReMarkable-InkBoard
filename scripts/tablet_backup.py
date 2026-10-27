"""Back up everything on the reMarkable 2 to this PC and verify it (docs/operations/device-setup.md, D8).

Read-only on the tablet. Run it from the repo root before every change on the tablet:

    uv run python scripts/tablet_backup.py <dest>
    uv run python scripts/tablet_backup.py <dest> --restore-test <member>

A backup holds:

- ``<device>.img.gz`` for every whole eMMC device (``mmcblk2`` and its two boot areas): read raw
  with ``dd`` over ssh and compressed here, because the tablet's own gzip manages only about
  2 MB/s;
- ``home-etc.tar.gz``: a file copy of ``/home`` and ``/etc``, with the tablet's own file list
  (``home-etc.list``) and SHA-256 sums (``home-etc.sha256``);
- ``README.txt`` (what was read and how it was checked) and ``SHA256SUMS`` over the files above.

Checks, all against values computed on the tablet:

- every image decompresses to exactly the device size, with a valid gzip CRC;
- the area before the first partition, each boot area and every partition that is not mounted
  read-write hash the same on both sides (read-write partitions are reported, not required);
- every regular file in the tar hashes the same as on the tablet, and the tar holds exactly the
  tablet's entries (sockets and pipes cannot be archived and are listed apart).

The work happens in ``<dest>.partial-<time>`` and is renamed to ``<dest>`` only when every check
passed; an existing ``<dest>`` is never touched. The backup holds secrets (Wi-Fi passwords, cloud
tokens, SSH host keys) and handwriting: the folder is readable by its owner only (on Linux and
macOS), a destination inside a git repository is refused, and this script prints sizes, counts
and hashes only.

``--restore-test`` streams one member of the tar into ``/tmp/restore-test`` on the tablet (a RAM
disk) and prints its SHA-256, to prove the restore path. It writes to the tablet, so run it only
when you mean to.
"""

import argparse
import gzip
import hashlib
import io
import re
import shutil
import subprocess  # nosec B404 - runs the user's own ssh client with a fixed argv
import sys
import tarfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from inkboard.core.config import Settings
from inkboard.services.sources import NEW_SESSION, SPAWN_FLAGS, ssh_base_argv

CHUNK = 1 << 20
SECTOR = 512
STALL_S = 60.0
SPARE_BYTES = 5 * 1024**3
PROGRESS_BYTES = 512 * CHUNK
DEVICES = ("mmcblk2", "mmcblk2boot0", "mmcblk2boot1")
SAFE_PATH = re.compile(r"^[A-Za-z0-9._/@+-]+$")
RECHECK_LIMIT = 50


class BackupError(Exception):
    """A check failed or the tablet stopped answering; the message says which."""


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def mb(n: int) -> str:
    return f"{n / CHUNK:,.0f} MB"


def inside_git_work_tree(path: Path) -> Path | None:
    """The repository root that ``path`` would land in, or None. A backup holds secrets and
    handwriting, so it must never sit where a ``git add`` could pick it up."""
    for folder in [path, *path.parents]:
        if (folder / ".git").exists():
            return folder
    return None


def private_dir(path: Path) -> None:
    """Create ``path`` readable by its owner only (0o700 on Linux and macOS; Windows ignores the mode
    and keeps the user-profile permissions of the parent)."""
    path.mkdir(mode=0o700)


# ---- talking to the tablet -----------------------------------------------------------------


def ssh_argv(remote: str, *, stdin: bool = False) -> list[str]:
    argv = ssh_base_argv(Settings())
    if stdin:
        argv.remove("-n")  # -n would feed the remote command /dev/null instead of our data
    return [*argv, remote]


def run_text(remote: str, timeout: float = 300) -> str:
    try:
        done = subprocess.run(  # nosec B603 - argv list, no shell
            ssh_argv(remote),
            capture_output=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=SPAWN_FLAGS,
            start_new_session=NEW_SESSION,
        )
    except subprocess.TimeoutExpired:
        raise BackupError(f"the tablet did not finish within {timeout:g} s") from None
    if done.returncode != 0:
        raise BackupError(f"tablet command failed (exit {done.returncode}): {last_line(done.stderr)}")
    return done.stdout.decode("utf-8", errors="replace")


def last_line(stderr: bytes) -> str:
    lines = [line.strip() for line in stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return lines[-1] if lines else "(no message)"


def stream(remote: str, sink: Callable[[bytes], None], label: str, expected: int | None = None) -> int:
    """Run ``remote`` and hand its stdout to ``sink`` chunk by chunk; return the byte count."""
    process = subprocess.Popen(  # nosec B603 - argv list, no shell
        ssh_argv(remote),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=SPAWN_FLAGS,
        start_new_session=NEW_SESSION,
    )
    errors: list[bytes] = []
    last = [time.monotonic()]
    stalled = threading.Event()
    finished = threading.Event()

    def drain() -> None:
        assert process.stderr is not None  # nosec B101 - set by stderr=PIPE
        errors.append(process.stderr.read())

    def watchdog() -> None:
        while not finished.wait(1.0):
            if time.monotonic() - last[0] > STALL_S:
                stalled.set()
                process.kill()
                return

    threads = [threading.Thread(target=drain, daemon=True), threading.Thread(target=watchdog, daemon=True)]
    for thread in threads:
        thread.start()
    total = 0
    next_report = PROGRESS_BYTES
    out = process.stdout
    assert isinstance(out, io.BufferedReader)  # nosec B101 - stdout=PIPE with the default buffering
    try:
        while chunk := out.read1(CHUNK):
            sink(chunk)
            total += len(chunk)
            last[0] = time.monotonic()
            if total >= next_report:
                of = f" of {mb(expected)}" if expected else ""
                say(f"  {label}: {mb(total)}{of}")
                next_report += PROGRESS_BYTES
        returncode = process.wait(timeout=30)
    finally:
        finished.set()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=5)
    if stalled.is_set():
        raise BackupError(f"{label}: no data for {STALL_S:g} s - is the tablet asleep?")
    if returncode != 0:
        raise BackupError(f"{label}: ssh exited {returncode}: {last_line(b''.join(errors))}")
    return total


# ---- what the tablet has -------------------------------------------------------------------


@dataclass
class Layout:
    firmware: str = "?"
    devices: dict[str, int] = field(default_factory=dict)  # name -> size in bytes
    partitions: dict[int, tuple[int, int]] = field(default_factory=dict)  # n -> (start, size) bytes
    read_write: set[int] = field(default_factory=set)  # partitions mounted read-write


def read_layout() -> Layout:
    text = run_text(
        "grep RELEASE_VERSION /usr/share/remarkable/update.conf; "
        f"for d in {' '.join(DEVICES)}; do [ -e /sys/block/$d ] && echo dev $d $(cat /sys/block/$d/size); done; "
        "for p in 1 2 3 4 5 6 7 8 9; do [ -e /sys/class/block/mmcblk2p$p ] && "
        "echo part $p $(cat /sys/class/block/mmcblk2p$p/start) $(cat /sys/class/block/mmcblk2p$p/size) "
        "$(cat /sys/class/block/mmcblk2p$p/dev); done; "
        "cat /proc/self/mountinfo",
        timeout=60,
    )
    layout = Layout()
    numbers: dict[str, int] = {}  # "major:minor" -> partition
    mounted_rw: set[str] = set()
    for line in text.splitlines():
        words = line.split()
        if line.startswith("REMARKABLE_RELEASE_VERSION="):
            layout.firmware = line.split("=", 1)[1].strip()
        elif len(words) == 3 and words[0] == "dev":
            layout.devices[words[1]] = int(words[2]) * SECTOR
        elif len(words) == 5 and words[0] == "part":
            layout.partitions[int(words[1])] = (int(words[2]) * SECTOR, int(words[3]) * SECTOR)
            numbers[words[4]] = int(words[1])
        elif len(words) > 5 and re.fullmatch(r"\d+:\d+", words[2]) and words[5].split(",")[0] == "rw":
            # mountinfo names devices by number: the root shows as /dev/root, never as its partition.
            mounted_rw.add(words[2])
    layout.read_write = {numbers[n] for n in mounted_rw if n in numbers}
    if "mmcblk2" not in layout.devices or 1 not in layout.partitions:
        raise BackupError("could not read the tablet's disk layout")
    return layout


# ---- images --------------------------------------------------------------------------------


class RangeHashes:
    """SHA-256 of named byte ranges of one stream, fed in order."""

    def __init__(self, ranges: dict[str, tuple[int, int]]) -> None:
        self.ranges = ranges  # name -> (start, end)
        self.hashes = {name: hashlib.sha256() for name in ranges}
        self.offset = 0

    def feed(self, chunk: bytes) -> None:
        lo, hi = self.offset, self.offset + len(chunk)
        for name, (start, end) in self.ranges.items():
            a, b = max(lo, start), min(hi, end)
            if a < b:
                self.hashes[name].update(chunk[a - lo : b - lo])
        self.offset = hi

    def hex(self) -> dict[str, str]:
        return {name: h.hexdigest() for name, h in self.hashes.items()}


def image_ranges(name: str, size: int, layout: Layout) -> dict[str, tuple[int, int]]:
    ranges = {"whole": (0, size)}
    if name == "mmcblk2":
        ranges["before-p1"] = (0, layout.partitions[1][0])
        for n, (start, length) in layout.partitions.items():
            ranges[f"p{n}"] = (start, start + length)
    return ranges


def image(name: str, size: int, layout: Layout, work: Path) -> dict[str, str]:
    path = work / f"{name}.img.gz"
    ranges = RangeHashes(image_ranges(name, size, layout))
    with gzip.open(path, "wb", compresslevel=1) as gz:

        def sink(chunk: bytes) -> None:
            gz.write(chunk)
            ranges.feed(chunk)

        got = stream(f"dd if=/dev/{name} bs=1M 2>/dev/null", sink, name, expected=size)
    if got != size:
        raise BackupError(f"{name}: read {got:,} bytes, the device has {size:,}")
    hashes = ranges.hex()
    # Read the file back: gzip checks its CRC and length at the end, and the bytes must be the ones received.
    back = hashlib.sha256()
    length = 0
    with gzip.open(path, "rb") as gz:
        while block := gz.read(16 * CHUNK):
            back.update(block)
            length += len(block)
    if length != size or back.hexdigest() != hashes["whole"]:
        raise BackupError(f"{name}: the file on disk does not decompress to the bytes received")
    say(f"  {name}: {mb(size)} read, {mb(path.stat().st_size)} on disk, read back OK")
    return hashes


def tablet_hashes(layout: Layout) -> dict[str, str]:
    """Hash on the tablet what the image check needs: boot areas, the area before p1, and every
    partition up to 1 GB (the large read-write home partition is checked by size and file copy)."""
    targets = [f"/dev/{d}" for d in layout.devices if d != "mmcblk2"]
    targets += [f"/dev/mmcblk2p{n}" for n, (_, size) in sorted(layout.partitions.items()) if size <= 1024**3]
    before = layout.partitions[1][0] // SECTOR
    text = run_text(f"sha256sum {' '.join(targets)}; dd if=/dev/mmcblk2 bs=512 count={before} 2>/dev/null | sha256sum")
    found: dict[str, str] = {}
    for line in text.splitlines():
        digest, _, name = line.strip().partition("  ")
        if name == "-":
            found["mmcblk2:before-p1"] = digest
        elif name.startswith("/dev/mmcblk2p"):
            found[f"mmcblk2:p{name.removeprefix('/dev/mmcblk2p')}"] = digest
        elif name.startswith("/dev/"):
            found[f"{name.removeprefix('/dev/')}:whole"] = digest
    return found


# ---- files ---------------------------------------------------------------------------------


@dataclass
class FileCheck:
    entries: int = 0
    files: int = 0
    unarchivable: list[str] = field(default_factory=list)
    rechecked: int = 0
    problems: list[str] = field(default_factory=list)


def copy_files(work: Path) -> FileCheck:
    tar_path = work / "home-etc.tar.gz"
    with gzip.open(tar_path, "wb", compresslevel=6) as gz:

        def sink(chunk: bytes) -> None:
            gz.write(chunk)

        stream("tar -C / -cf - home etc", sink, "home-etc tar")
    sums = run_text("cd / && find home etc -type f -exec sha256sum {} +", timeout=900)
    listing = run_text("cd / && find home etc", timeout=300)
    special = run_text("cd / && find home etc -type s -o -type p", timeout=300)
    (work / "home-etc.sha256").write_text(sums, encoding="utf-8", newline="\n")
    (work / "home-etc.list").write_text(listing, encoding="utf-8", newline="\n")

    expected: dict[str, str] = {}
    for line in sums.splitlines():
        digest, _, path = line.partition("  ")
        if path:
            expected[path] = digest
    names = {line for line in listing.splitlines() if line}
    check = FileCheck(unarchivable=sorted(line for line in special.splitlines() if line))
    names -= set(check.unarchivable)

    in_tar: set[str] = set()
    got: dict[str, str] = {}
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar:
            name = member.name.rstrip("/")
            in_tar.add(name)
            if member.isreg() or member.islnk():
                data = tar.extractfile(member)
                if data is None:
                    check.problems.append(f"unreadable in the tar: {name}")
                    continue
                hasher = hashlib.sha256()
                while block := data.read(CHUNK):
                    hasher.update(block)
                got[name] = hasher.hexdigest()
    check.entries, check.files = len(in_tar), len(got)

    check.problems += [f"on the tablet, not in the tar: {n}" for n in sorted(names - in_tar)]
    check.problems += [f"in the tar, not on the tablet: {n}" for n in sorted(in_tar - names)]
    check.problems += [f"file missing from the tar: {p}" for p in sorted(set(expected) - set(got))]
    differ = sorted(p for p in expected if p in got and got[p] != expected[p])
    if differ:
        check.rechecked = len(differ)
        unsafe = [p for p in differ if not SAFE_PATH.match(p)]
        if unsafe or len(differ) > RECHECK_LIMIT:
            check.problems += [f"differs: {p}" for p in differ]
        else:
            # A file that changed while the list was hashed: hash it again; it must now equal the tar.
            again = run_text("cd / && sha256sum " + " ".join(differ))
            now = {path: digest for digest, _, path in (line.partition("  ") for line in again.splitlines())}
            check.problems += [f"changed during the backup: {p}" for p in differ if now.get(p) != got[p]]
    return check


# ---- the backup ----------------------------------------------------------------------------


def backup(dest: Path) -> int:
    dest = dest.resolve()
    if dest.exists():
        raise BackupError(f"{dest} exists - a backup is never overwritten; choose a new name")
    repo = inside_git_work_tree(dest)
    if repo is not None:
        raise BackupError(
            f"{dest} is inside the git repository {repo}; a backup holds secrets and handwriting - "
            "choose a folder outside every repository"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now().astimezone()
    work = dest.with_name(f"{dest.name}.partial-{started:%H%M%S}")
    say("reading the tablet's disk layout")
    layout = read_layout()
    raw = sum(layout.devices.values())
    free = shutil.disk_usage(dest.parent).free
    if free < raw + SPARE_BYTES:
        raise BackupError(f"not enough space: {mb(free)} free, need {mb(raw)} + 5 GB to spare")
    say(f"firmware {layout.firmware}; devices {', '.join(f'{d} {mb(s)}' for d, s in layout.devices.items())}")
    private_dir(work)

    images: dict[str, str] = {}
    for name, size in layout.devices.items():
        say(f"imaging {name}")
        images |= {f"{name}:{part}": digest for part, digest in image(name, size, layout, work).items()}

    say("hashing on the tablet")
    tablet = tablet_hashes(layout)
    compare: list[str] = []
    failed: list[str] = []
    required = {"mmcblk2:before-p1", *(f"{d}:whole" for d in layout.devices if d != "mmcblk2")}
    required |= {f"mmcblk2:p{n}" for n, (_, size) in layout.partitions.items() if size <= 1024**3}
    required -= {f"mmcblk2:p{n}" for n in layout.read_write}
    failed += [f"{key}: no hash from the tablet" for key in sorted(required - set(tablet))]
    for key, digest in sorted(tablet.items()):
        if images.get(key) == digest:
            compare.append(f"{key}: same")
        elif key in required:
            failed.append(f"{key}: the image differs from the tablet")
        else:
            compare.append(f"{key}: changed while mounted read-write (reported, not required)")
    if failed:
        for line in compare + failed:
            say(f"  {line}")
        raise BackupError(f"{len(failed)} image check(s) failed; kept in {work.name} for a look")
    for n in sorted(set(layout.partitions) - {int(k.split(":p")[1]) for k in tablet if ":p" in k}):
        rw = "read-write" if n in layout.read_write else "read-only"
        compare.append(f"mmcblk2:p{n}: {mb(layout.partitions[n][1])}, mounted {rw}; checked by size and the file copy")

    say("copying /home and /etc")
    files = copy_files(work)
    if files.problems:
        for problem in files.problems[:20]:
            say(f"  {problem}")
        raise BackupError(f"the file copy has {len(files.problems)} problem(s); kept in {work.name} for a look")

    write_readme(work, started, layout, compare, files)
    write_sums(work)
    work.rename(dest)
    say("")
    say(f"backup OK: {dest}")
    for line in compare:
        say(f"  {line}")
    say(f"  home + etc: {files.entries} entries, {files.files} files, all hashes equal")
    if files.rechecked:
        say(f"  {files.rechecked} file(s) changed while hashed and matched on a second hash")
    if files.unarchivable:
        say(f"  {len(files.unarchivable)} socket(s) or pipe(s) not archivable (listed in README.txt)")
    for path in sorted(dest.iterdir()):
        say(f"  {path.name}: {path.stat().st_size:,} bytes")
    return 0


def write_readme(work: Path, started: datetime, layout: Layout, compare: list[str], files: FileCheck) -> None:
    lines = [
        "reMarkable 2 backup (inkboard scripts/tablet_backup.py; docs/operations/device-setup.md, D8)",
        f"started {started:%Y-%m-%d %H:%M:%S %Z}, finished {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}",
        f"firmware {layout.firmware}",
        "",
        "Devices (bytes):",
        *(f"  {name}: {size:,}" for name, size in layout.devices.items()),
        "Partitions of mmcblk2 (start, size in bytes, mount):",
        *(
            f"  p{n}: {start:,}, {size:,}, {'read-write' if n in layout.read_write else 'not read-write'}"
            for n, (start, size) in sorted(layout.partitions.items())
        ),
        "",
        "Checks:",
        "  every image decompresses to exactly the device size with a valid gzip CRC",
        *(f"  {line}" for line in compare),
        f"  home + etc: {files.entries} entries, {files.files} files, every hash equal to the tablet's",
        *(f"  not archivable (socket or pipe): {path}" for path in files.unarchivable),
        "",
        "Restore one file (it is written to /tmp/restore-test on the tablet, a RAM disk):",
        "  uv run python scripts/tablet_backup.py <this folder> --restore-test <path in the tar>",
        "  then move it into place over ssh yourself, one deliberate step at a time.",
        "Whole disk: the images are the last resort; writing them back needs the tablet's USB",
        "recovery mode, which was not tested.",
        "",
        "This folder holds secrets (Wi-Fi passwords, cloud tokens, SSH host keys) and handwriting.",
        "Keep it on this PC; never sync, share or commit it. Delete an old backup only when you are",
        "sure you will not need it.",
    ]
    (work / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_sums(work: Path) -> None:
    lines = []
    for path in sorted(work.iterdir()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(16 * CHUNK):
                digest.update(block)
        lines.append(f"{digest.hexdigest()}  {path.name}")
    (work / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


# ---- restore test --------------------------------------------------------------------------


def restore_test(dest: Path, member: str) -> int:
    if not SAFE_PATH.match(member) or member.startswith("/") or ".." in member.split("/"):
        raise BackupError("the member must be a plain relative path from the tar, such as etc/hostname")
    with tarfile.open(dest / "home-etc.tar.gz", "r:gz") as tar:
        info = tar.getmember(member)
        data = tar.extractfile(info) if info.isreg() else None
        if data is None:
            raise BackupError(f"{member} is not a regular file in the backup")
        content = data.read()
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w") as out:
        out.addfile(info, io.BytesIO(content))
    done = subprocess.run(  # nosec B603 - argv list, no shell
        ssh_argv("mkdir -p /tmp/restore-test && tar -xf - -C /tmp/restore-test", stdin=True),
        input=packed.getvalue(),
        capture_output=True,
        timeout=60,
        check=False,
        creationflags=SPAWN_FLAGS,
        start_new_session=NEW_SESSION,
    )
    if done.returncode != 0:
        raise BackupError(f"restore failed (exit {done.returncode}): {last_line(done.stderr)}")
    say(f"restored {member} to /tmp/restore-test/{member} on the tablet")
    say(f"  {len(content):,} bytes, sha256 {hashlib.sha256(content).hexdigest()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Back up the reMarkable 2 and verify it (read-only on the tablet).")
    parser.add_argument("dest", type=Path, help="new folder for the backup")
    parser.add_argument(
        "--restore-test", metavar="MEMBER", help="restore one file of DEST's tar into /tmp on the tablet"
    )
    args = parser.parse_args(argv)
    try:
        if args.restore_test:
            return restore_test(args.dest, args.restore_test)
        return backup(args.dest)
    except BackupError as error:
        say(f"STOPPED: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
