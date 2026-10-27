"""scripts/tablet_backup.py: the parts that decide before the tablet is ever read. Nothing here runs
ssh; every path is under tmp_path."""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tablet_backup.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tablet_backup", SCRIPT)
    assert spec is not None and spec.loader is not None  # nosec B101 - test-only
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tb = load()


def test_a_folder_inside_a_git_repository_is_found(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert tb.inside_git_work_tree(tmp_path / "repo" / "backups" / "today") == tmp_path / "repo"
    assert tb.inside_git_work_tree(tmp_path / "elsewhere" / "today") is None  # control


def test_backup_refuses_a_destination_inside_a_repository_before_reading_the_tablet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)

    def must_not_run() -> None:
        raise AssertionError("the tablet was read")

    monkeypatch.setattr(tb, "read_layout", must_not_run)
    with pytest.raises(tb.BackupError, match="inside the git repository"):
        tb.backup(tmp_path / "repo" / "backup-1")


def test_backup_outside_a_repository_goes_on_to_the_tablet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reached() -> None:
        raise tb.BackupError("reached the tablet")

    monkeypatch.setattr(tb, "read_layout", reached)
    with pytest.raises(tb.BackupError, match="reached the tablet"):  # control: no refusal before it
        tb.backup(tmp_path / "backups" / "backup-1")


def test_an_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "backup-1").mkdir()
    with pytest.raises(tb.BackupError, match="never overwritten"):
        tb.backup(tmp_path / "backup-1")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows ignores the mode; the profile's permissions apply")
def test_the_work_folder_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    tb.private_dir(tmp_path / "work")
    assert (tmp_path / "work").stat().st_mode & 0o777 == 0o700


def test_the_work_folder_is_created(tmp_path: Path) -> None:
    tb.private_dir(tmp_path / "work")
    assert (tmp_path / "work").is_dir()


def test_the_readme_uses_iso_dates_and_points_at_the_runbook(tmp_path: Path) -> None:
    layout = tb.Layout(firmware="3.29.0.153", devices={"mmcblk2": 2048}, partitions={1: (512, 1024)}, read_write={1})
    started = datetime(2026, 1, 2, 3, 4, 5).astimezone()
    tb.write_readme(tmp_path, started, layout, ["mmcblk2:before-p1: same"], tb.FileCheck(entries=3, files=2))
    text = (tmp_path / "README.txt").read_text(encoding="utf-8")
    assert "started 2026-01-02 03:04:05" in text
    assert "docs/operations/device-setup.md, D8" in text
    assert "never sync, share or commit it" in text


@pytest.mark.parametrize("member", ["../etc/passwd", "/etc/passwd", "etc/../../x", "etc/host name"])
def test_restore_test_refuses_an_unsafe_member(tmp_path: Path, member: str) -> None:
    with pytest.raises(tb.BackupError, match="plain relative path"):
        tb.restore_test(tmp_path, member)


def test_main_reports_a_stop_and_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "backup-1").mkdir()
    assert tb.main([str(tmp_path / "backup-1")]) == 1
    assert "STOPPED:" in capsys.readouterr().out
