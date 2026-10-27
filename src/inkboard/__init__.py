"""inkboard: live drawing board for a reMarkable 2."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("inkboard")
except PackageNotFoundError:  # pragma: no cover - only when run from a tree that was never installed
    __version__ = "0.0.0"
