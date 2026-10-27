import sys
from pathlib import Path

import pytest

from inkboard import cli
from inkboard.services.demo import generate, generate_bytes
from inkboard.services.events import EventDecoder
from inkboard.services.strokes import StrokeBuilder, StrokeStarted

FIXTURE = Path(__file__).parent / "fixtures" / "demo.bin"
FAKE = str(Path(__file__).with_name("fake_device.py"))


def test_demo_writes_the_generated_stream(tmp_path: Path) -> None:
    out = tmp_path / "sub" / "demo.bin"
    assert cli.main(["demo", "--out", str(out)]) == 0
    assert out.read_bytes() == generate_bytes()


def test_committed_fixture_matches_the_generator() -> None:
    """Regenerate with: uv run inkboard demo --out tests/fixtures/demo.bin"""
    assert FIXTURE.read_bytes() == generate_bytes()


def test_demo_is_deterministic_and_draws_four_strokes() -> None:
    assert generate() == generate()
    builder = StrokeBuilder()
    starts = [s for e in EventDecoder().feed(generate_bytes()) for s in builder.feed(e) if isinstance(s, StrokeStarted)]
    assert len(starts) == 4


def test_record_writes_raw_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "pen_stream_argv", lambda settings, reader="dd": [sys.executable, FAKE, "stream-exit"])
    out = tmp_path / "rec.bin"
    assert cli.main(["record", "--out", str(out), "--seconds", "10"]) == 0
    assert len(out.read_bytes()) % 16 == 0 and out.stat().st_size > 0


def test_record_reports_ssh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "pen_stream_argv", lambda settings, reader="dd": [sys.executable, FAKE, "fail-auth"])
    out = tmp_path / "rec.bin"
    assert cli.main(["record", "--out", str(out), "--seconds", "10"]) == 1
    assert "Permission denied (publickey)" in capsys.readouterr().out
    assert not out.exists()


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
