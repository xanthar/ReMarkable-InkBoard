"""Test isolation from the machine the tests run on.

inkboard reads ``INKBOARD_*`` environment variables and a ``.env`` file in the working folder. A
developer's own values (their tablet's MAC, their Wi-Fi addresses) must never reach a test, so
every test starts with no ``INKBOARD_*`` variable, no ``XDG_STATE_HOME`` and no ``.env`` file.
"""

import os
from collections.abc import Iterator

import pytest

from inkboard.core.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.upper().startswith("INKBOARD_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    yield
