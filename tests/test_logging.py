import pytest
import structlog

from inkboard.core.logging import HANDWRITING_KEYS, configure_logging, drop_handwriting


def test_handwriting_keys_are_removed() -> None:
    event = {"event": "stroke.end", "stroke_id": 3, "points": 184, "x": 1, "y": 2, "pts": [[0.1, 0.2, 0.3]]}
    out = drop_handwriting(None, "info", dict(event))
    assert out == {"event": "stroke.end", "stroke_id": 3}


def test_every_handwriting_key_is_covered() -> None:
    event = {key: 1 for key in HANDWRITING_KEYS} | {"event": "x"}
    assert drop_handwriting(None, "info", event) == {"event": "x"}


def test_configured_chain_drops_handwriting_before_rendering() -> None:
    configure_logging()
    processors = structlog.get_config()["processors"]
    assert drop_handwriting in processors
    assert processors.index(drop_handwriting) < len(processors) - 1


def test_screen_frames_are_never_logged() -> None:
    event = {"event": "screen.grab", "ms": 812, "frame": b"\x00", "rgba": b"\x00", "bgra": b"\x00", "raw": b"\x00"}
    assert drop_handwriting(None, "info", event) == {"event": "screen.grab", "ms": 812}


def test_tracebacks_never_show_local_variables(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    marker = "local-value-that-must-not-print"
    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger("test").exception("failed")
    err = capsys.readouterr().err
    assert "ValueError: boom" in err and marker not in err
