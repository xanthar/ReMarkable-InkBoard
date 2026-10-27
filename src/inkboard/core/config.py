"""Settings, read from ``INKBOARD_*`` environment variables and a ``.env`` file, and validated.

The ``.env`` file is read from the folder inkboard starts in; real environment variables win over
it. It holds the values that belong to one person and one tablet (the tablet's Wi-Fi MAC, its
addresses) and is never committed; ``.env.example`` documents every key.

``pen_device`` and ``ssh_host`` end up in an SSH command line, so both are held to strict
patterns: a host can never start with ``-`` (an ssh option) and the device can only be an
input event node. Wi-Fi addresses are parsed as IPv4 addresses, so their text can never be an
option either, and only private LAN addresses outside the USB link are accepted.
"""

import sys
from ipaddress import IPv4Address, IPv4Network
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from inkboard.services.geometry import Orientation

WINDOWS_SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"
USB_NET = IPv4Network("10.11.99.0/29")  # the reMarkable 2's USB network link, the same on every tablet
MAX_WIFI_ADDRESSES = 16
# The private LAN ranges (RFC 1918): the only places a tablet on a home or office Wi-Fi can be.
PRIVATE_LANS = (IPv4Network("10.0.0.0/8"), IPv4Network("172.16.0.0/12"), IPv4Network("192.168.0.0/16"))


def is_wifi_candidate(address: IPv4Address) -> bool:
    """A private LAN address the tablet could have on a local Wi-Fi, never the USB link. Anything
    else (public, broadcast, reserved, benchmarking or documentation ranges, carrier-grade NAT) is
    refused: a typo or a planted value must not send the pen stream anywhere else."""
    return any(address in net for net in PRIVATE_LANS) and address not in USB_NET


def default_ssh_bin() -> str:
    """The Windows OpenSSH client on Windows (Git's ssh may come first on PATH); ``ssh`` elsewhere."""
    return WINDOWS_SSH if sys.platform == "win32" else "ssh"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INKBOARD_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    ssh_bin: str = Field(default_factory=default_ssh_bin, min_length=1)
    ssh_host: str = Field(default="remarkable", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    pen_device: str = Field(default="/dev/input/event1", pattern=r"^/dev/input/event\d+$")
    event_size: int = 16
    # The reMarkable 2's pen axes onto the portrait page (four corner taps; `inkboard probe` checks it).
    orientation: str = "swap_xy,flip_y"
    batch_ms: int = Field(default=16, ge=4, le=100)
    port: int = Field(default=8000, ge=1024, le=65535)
    # Tried in order after USB and the remembered address: INKBOARD_WIFI_ADDRESSES="a,b".
    wifi_addresses: Annotated[list[IPv4Address], NoDecode] = Field(default_factory=list)
    # Your tablet's Wi-Fi MAC; without it inkboard never searches the network for the tablet.
    tablet_mac: str | None = Field(default=None, pattern=r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

    @field_validator("wifi_addresses", mode="before")
    @classmethod
    def _split_addresses(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("wifi_addresses")
    @classmethod
    def _check_addresses(cls, value: list[IPv4Address]) -> list[IPv4Address]:
        if len(value) > MAX_WIFI_ADDRESSES:
            raise ValueError(f"at most {MAX_WIFI_ADDRESSES} Wi-Fi addresses")
        if len(set(value)) != len(value):
            raise ValueError("a Wi-Fi address is listed twice")
        for address in value:
            if not is_wifi_candidate(address):
                raise ValueError(f"{address} is not a private Wi-Fi address outside the USB link")
        return value

    @field_validator("tablet_mac", mode="before")
    @classmethod
    def _normalise_mac(cls, value: Any) -> Any:
        """Upper case with colons; an empty value (``INKBOARD_TABLET_MAC=`` in .env) means not set."""
        if isinstance(value, str):
            value = value.strip()
            return value.upper().replace("-", ":") if value else None
        return value

    @field_validator("event_size")
    @classmethod
    def _check_event_size(cls, value: int) -> int:
        if value not in (16, 24):
            raise ValueError("event_size must be 16 or 24")
        return value

    @field_validator("orientation")
    @classmethod
    def _check_orientation(cls, value: str) -> str:
        return str(Orientation.parse(value))

    @property
    def orientation_value(self) -> Orientation:
        return Orientation.parse(self.orientation)
