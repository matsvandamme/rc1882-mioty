"""Shared fixtures for the rc1882_mioty test suite.

Tests never touch a real serial port: FakeSerial stands in for pyserial's
Serial, so RC1882Mioty's wire-protocol logic (framing, timeouts, retries,
pacing) can be exercised deterministically and fast.
"""

from __future__ import annotations

import pytest

from rc1882_mioty import RC1882Mioty


@pytest.fixture
def anyio_backend() -> str:
    """Run @pytest.mark.anyio tests (rc1882_mioty_mcp) on asyncio only."""
    return "asyncio"


class FakeSerial:
    """Minimal stand-in for serial.Serial.

    Call `queue_response(bytes)` once per expected write — the response bytes
    become readable immediately after the matching write() call, mirroring
    how the real module answers each AT command in turn.
    """

    def __init__(self) -> None:
        self.is_open = False
        self.timeout: float | None = None
        self.port: str | None = None
        self.baudrate: int | None = None
        self.bytesize = None
        self.parity = None
        self.stopbits = None
        self.xonxoff: bool | None = None
        self.rtscts: bool | None = None
        self.dsrdtr: bool | None = None
        self.writes: list[bytes] = []
        self._pending_responses: list[bytes] = []
        self._read_buffer = bytearray()

    def queue_response(self, data: bytes) -> None:
        self._pending_responses.append(data)

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def reset_input_buffer(self) -> None:
        self._read_buffer.clear()

    def flush(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        if self._pending_responses:
            self._read_buffer += self._pending_responses.pop(0)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if not self._read_buffer:
            return b""
        chunk = bytes(self._read_buffer[:size])
        del self._read_buffer[:size]
        return chunk


@pytest.fixture
def fake_serial(mocker) -> FakeSerial:
    fake = FakeSerial()
    mocker.patch("rc1882_mioty.client.serial.Serial", return_value=fake)
    return fake


@pytest.fixture
def modem(fake_serial: FakeSerial) -> RC1882Mioty:
    """A modem with short timeouts and no pacing, for fast, deterministic tests.

    Individual tests that specifically exercise timeout durations or pacing
    construct their own RC1882Mioty instead of using this fixture.
    """
    m = RC1882Mioty(
        "/dev/ttyFAKE",
        default_timeout=0.05,
        tx_timeout=0.05,
        attach_timeout=0.05,
        min_command_interval=0,
    )
    m.open()
    yield m
    m.close()
