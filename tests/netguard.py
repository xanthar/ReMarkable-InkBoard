"""A guard for tests of the link and the search: any real network use beyond loopback, and any
child process, fails the test. The code under test gets its I/O injected; this proves it."""

import socket
import subprocess
from typing import Any

import pytest


def _loopback(address: Any) -> bool:
    return isinstance(address, tuple) and str(address[0]).startswith("127.")


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_sendto = socket.socket.sendto

    def connect(self: socket.socket, address: Any) -> None:
        if not _loopback(address):
            raise AssertionError(f"test tried the real network: connect {address}")
        real_connect(self, address)

    def sendto(self: socket.socket, data: bytes, *args: Any) -> int:
        address = args[-1]
        if not _loopback(address):
            raise AssertionError(f"test tried the real network: sendto {address}")
        return real_sendto(self, data, *args)

    def no_process(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"test tried to start a process: {args[:1]}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(subprocess, "run", no_process)
