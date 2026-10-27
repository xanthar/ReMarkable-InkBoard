"""Map raw digitizer coordinates onto the portrait page.

The page is the tablet in portrait mode (3:4). Coordinates leave this module normalised:
``u`` runs left to right and ``v`` top to bottom, both in [0, 1]; pressure is in [0, 1].
The digitizer's axes are rotated relative to the screen, and which of the eight
swap/flip combinations is correct is measured on the device with ``inkboard probe``.
"""

from dataclasses import dataclass

_FLAGS = ("swap_xy", "flip_x", "flip_y")


@dataclass(frozen=True, slots=True)
class PenRanges:
    """Axis maxima of the rM2 pen digitizer (all minima are 0)."""

    max_x: int = 20967
    max_y: int = 15725
    max_pressure: int = 4095


@dataclass(frozen=True, slots=True)
class Orientation:
    """Swap is applied first, then the flips act on the page axes."""

    swap_xy: bool = False
    flip_x: bool = False
    flip_y: bool = False

    @classmethod
    def parse(cls, text: str) -> "Orientation":
        """Parse ``"swap_xy,flip_y"``; ``""`` or ``"none"`` means no transform."""
        cleaned = text.strip().lower()
        if cleaned in ("", "none"):
            return cls()
        parts = [part.strip() for part in cleaned.split(",") if part.strip()]
        unknown = sorted(set(parts) - set(_FLAGS))
        if unknown:
            raise ValueError(f"unknown orientation flag(s) {unknown}; allowed: {list(_FLAGS)}")
        return cls(swap_xy="swap_xy" in parts, flip_x="flip_x" in parts, flip_y="flip_y" in parts)

    def __str__(self) -> str:
        flags = [name for name in _FLAGS if getattr(self, name)]
        return ",".join(flags) if flags else "none"


RM2_RANGES = PenRanges()


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def to_page(
    x: int, y: int, pressure: int, orientation: Orientation, ranges: PenRanges = RM2_RANGES
) -> tuple[float, float, float]:
    """Return ``(u, v, p)`` for one raw sample."""
    u = _unit(x / ranges.max_x)
    v = _unit(y / ranges.max_y)
    if orientation.swap_xy:
        u, v = v, u
    if orientation.flip_x:
        u = 1.0 - u
    if orientation.flip_y:
        v = 1.0 - v
    return u, v, _unit(pressure / ranges.max_pressure)


def to_raw(u: float, v: float, orientation: Orientation, ranges: PenRanges = RM2_RANGES) -> tuple[int, int]:
    """Inverse of :func:`to_page` for position: page ``(u, v)`` back to raw ``(x, y)``."""
    if orientation.flip_x:
        u = 1.0 - u
    if orientation.flip_y:
        v = 1.0 - v
    if orientation.swap_xy:
        u, v = v, u
    return round(_unit(u) * ranges.max_x), round(_unit(v) * ranges.max_y)
