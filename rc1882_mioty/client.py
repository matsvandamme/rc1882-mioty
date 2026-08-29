"""AT command driver for the Radiocrafts RC1882CEF-MIOTY1 mioty module.

Wraps the textual AT command protocol documented in MIOTY1_User_Manual.pdf (rev. 2.01),
spoken over a serial UART link (115200 8N1, no flow control). Every command is ASCII
terminated with a carriage return; every response is one or more lines terminated with
CRLF, ending in a bare-integer status line (0 = OK).

Example:
    from rc1882_mioty import RC1882Mioty

    with RC1882Mioty("/dev/ttyUSB0") as modem:
        print(modem.get_modem_info())
        print(modem.get_eui().hex())
        modem.send_unidirectional(b"HelloWorld")
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from types import TracebackType
from typing import overload

import serial

from .constants import (
    DEFAULT_BAUDRATE,
    HT,
    STATUS_OK,
    SUB,
    AttachState,
    UplinkMode,
    UplinkProfile,
)
from .exceptions import MiotyCommandError, MiotyIncompleteResponseError, MiotyTimeoutError

_STATUS_LINE_RE = re.compile(r"\d+")


def _hex(data: bytes) -> str:
    return data.hex().upper()


class AtResponse:
    """Parsed non-terminal lines of a single AT command response.

    Each line is either "I:<fields>" / "-NAME:<fields>" (identification banners),
    "-NAME:<int>" (a plain value), or "-NAME:<len><HT><hex>[<SUB>]" (a binary
    payload), per manual §6.1. This class exposes the raw lines plus small
    accessors to pull specific fields out of them.
    """

    def __init__(self, lines: list[str]):
        self.lines = lines
        self._fields: list[tuple[str, str]] = []
        for line in lines:
            name, _, rest = line.partition(":")
            self._fields.append((name.lstrip("-"), rest))

    @overload
    def text(self, name: str) -> str | None: ...
    @overload
    def text(self, name: str, default: str) -> str: ...

    def text(self, name: str, default: str | None = None) -> str | None:
        """The value of the first field named `name`, or `default`."""
        for field_name, value in self._fields:
            if field_name == name:
                return value
        return default

    def all_text(self, name: str) -> list[str]:
        """Every value of fields named `name`, in the order they appeared."""
        return [value for field_name, value in self._fields if field_name == name]

    @overload
    def int(self, name: str) -> int | None: ...
    @overload
    def int(self, name: str, default: int) -> int: ...

    def int(self, name: str, default: int | None = None) -> int | None:
        value = self.text(name)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    @overload
    def bytes(self, name: str) -> bytes | None: ...
    @overload
    def bytes(self, name: str, default: bytes) -> bytes: ...

    def bytes(self, name: str, default: bytes | None = None) -> bytes | None:
        """Decode a "<len><HT><hex>[<SUB>]" field into raw bytes."""
        value = self.text(name)
        if value is None or HT not in value:
            return default
        _, hex_part = value.split(HT, 1)
        hex_part = hex_part.rstrip(SUB)
        try:
            return bytes.fromhex(hex_part)
        except ValueError:
            return default


@dataclass(frozen=True)
class ModemInfo:
    """Response to ATI / the unsolicited post-reboot identification banner."""

    raw: str
    fields: list[str]

    @property
    def vendor(self) -> str | None:
        return self.fields[0] if self.fields else None

    def __str__(self) -> str:
        return self.raw


@dataclass(frozen=True)
class LibraryVersion:
    """Response to AT-LIBV (the underlying mioty protocol library identity)."""

    raw: str
    fields: list[str]

    @property
    def vendor(self) -> str | None:
        return self.fields[0] if self.fields else None

    def __str__(self) -> str:
        return self.raw


@dataclass(frozen=True)
class UplinkResult:
    """Result of a unidirectional send (AT-U / AT-UMPF / AT-TU).

    packet_counter is None for AT-TU, which has no MAC layer and therefore no
    packet counter (manual §6.2.6).
    """

    packet_counter: int | None


@dataclass(frozen=True)
class DownlinkResult:
    """Result of a bidirectional send (AT-B / AT-BMPF / AT-TB)."""

    packet_counter: int | None
    acknowledged: bool
    payload: bytes | None


@dataclass(frozen=True)
class AttachResult:
    """Result of an over-the-air attach/detach (AT-MAOA / AT-MDOA)."""

    packet_counter: int | None
    acknowledged: bool
    mac_state: AttachState


class RC1882Mioty:
    """Driver for a Radiocrafts RC1882CEF-MIOTY1 module over a serial port.

    The port is not opened automatically; call `open()` or use as a context
    manager. `default_timeout` covers simple query/config commands; sending
    data over the air takes real time (telegram splitting, optional downlink
    RX window), so those commands use the larger `tx_timeout` default instead.
    `attach_timeout` covers the whole attach/detach family (over-the-air *and*
    local) — attach_local()/detach_local() were found under stress testing to
    reliably need more than a couple of seconds whenever they have real work
    to do, not just the over-the-air variants. Any method accepts a `timeout`
    override.

    `min_command_interval` is a minimum gap enforced between the end of one
    command and the start of the next (default 100ms). Stress testing found
    that firing AT commands back-to-back with no gap at all made the module
    intermittently report success while omitting the requested data, or miss
    its response window entirely — a 100ms gap eliminated that outright. Set
    it to 0 to disable if you've verified your own use case doesn't need it.

    `query_retries` is how many extra attempts a handful of read-only getters
    (get_eui, get_short_address, get_packet_counter) make if the module
    reports success but leaves out the expected field — see
    MiotyIncompleteResponseError. Retrying is safe here since these are pure,
    side-effect-free queries.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        default_timeout: float = 2.0,
        tx_timeout: float = 15.0,
        attach_timeout: float = 30.0,
        min_command_interval: float = 0.1,
        query_retries: int = 1,
    ):
        self._t_query = default_timeout
        self._t_tx = tx_timeout
        self._t_attach = attach_timeout
        self._min_command_interval = min_command_interval
        self._query_retries = query_retries

        self._lock = threading.RLock()
        self._last_command_end = 0.0

        self._serial = serial.Serial()
        self._serial.port = port
        self._serial.baudrate = baudrate
        self._serial.bytesize = serial.EIGHTBITS
        self._serial.parity = serial.PARITY_NONE
        self._serial.stopbits = serial.STOPBITS_ONE
        self._serial.xonxoff = False
        self._serial.rtscts = False
        self._serial.dsrdtr = False

    # -- lifecycle -----------------------------------------------------

    def open(self) -> None:
        if not self._serial.is_open:
            self._serial.open()

    def close(self) -> None:
        if self._serial.is_open:
            self._serial.close()

    @property
    def is_open(self) -> bool:
        return bool(self._serial.is_open)

    def __enter__(self) -> RC1882Mioty:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- low-level wire protocol ----------------------------------------

    def _read_line(self, deadline: float, label: str, total_timeout: float) -> str:
        buf = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MiotyTimeoutError(label, total_timeout)
            self._serial.timeout = remaining
            chunk = self._serial.read(1)
            if not chunk:
                # An empty read only means "the deadline passed" for a Serial
                # implementation that blocks for the full configured .timeout
                # before giving up. Don't assume that — loop back and check
                # the actual deadline ourselves.
                continue
            buf += chunk
            if buf.endswith(b"\r\n"):
                return bytes(buf[:-2]).decode("ascii", errors="replace")

    def _enforce_min_interval(self) -> None:
        """Sleep off whatever remains of `min_command_interval` since the last
        command finished (success or failure) — see class docstring."""
        if self._min_command_interval <= 0:
            return
        remaining = self._min_command_interval - (time.monotonic() - self._last_command_end)
        if remaining > 0:
            time.sleep(remaining)

    def _write_and_collect(self, wire: bytes, label: str, timeout: float) -> AtResponse:
        # The AT protocol is strictly one-command-at-a-time over a single
        # UART, so the whole exchange — pacing, write, and read — is one
        # critical section. RLock (not Lock) because factory_reset()/reset()
        # hold it across their own nested _transact() + boot-banner read.
        with self._lock:
            self._enforce_min_interval()
            try:
                self._serial.reset_input_buffer()
                self._serial.write(wire)
                self._serial.flush()
                deadline = time.monotonic() + timeout
                lines: list[str] = []
                while True:
                    line = self._read_line(deadline, label, timeout)
                    if _STATUS_LINE_RE.fullmatch(line):
                        status = int(line)
                        if status != STATUS_OK:
                            raise MiotyCommandError(label, status, lines)
                        return AtResponse(lines)
                    lines.append(line)
            finally:
                self._last_command_end = time.monotonic()

    def _transact(self, command: str, timeout: float) -> AtResponse:
        return self._write_and_collect(command.encode("ascii") + b"\r", command, timeout)

    def _query_bytes(
        self, command: str, field: str, timeout: float | None, retries: int | None
    ) -> bytes:
        """Send `command` and extract a "<len><HT><hex>" field, retrying (by
        default `query_retries` times) if the module reports success but
        leaves the field out — see MiotyIncompleteResponseError."""
        attempts = 1 + (self._query_retries if retries is None else retries)
        for _ in range(attempts):
            resp = self._transact(command, timeout or self._t_query)
            value = resp.bytes(field)
            if value is not None:
                return value
        raise MiotyIncompleteResponseError(command, field)

    def _query_int(
        self, command: str, field: str, timeout: float | None, retries: int | None
    ) -> int:
        """Integer counterpart of `_query_bytes`."""
        attempts = 1 + (self._query_retries if retries is None else retries)
        for _ in range(attempts):
            resp = self._transact(command, timeout or self._t_query)
            value = resp.int(field)
            if value is not None:
                return value
        raise MiotyIncompleteResponseError(command, field)

    def _read_boot_banner(self, timeout: float) -> ModemInfo | None:
        """Best-effort capture of the unsolicited "I:..." banner the module emits
        once it finishes rebooting after ATZ / AT-RST (manual §6.2.9, §6.2.16)."""
        deadline = time.monotonic() + timeout
        try:
            line = self._read_line(deadline, "boot banner", timeout)
        except MiotyTimeoutError:
            return None
        if not line.startswith("I:"):
            return None
        try:
            self._read_line(deadline, "boot banner status", timeout)  # discard status line
        except MiotyTimeoutError:
            pass
        return ModemInfo(raw=line, fields=line[2:].split(";"))

    # -- identification ---------------------------------------------------

    def get_modem_info(self, timeout: float | None = None) -> ModemInfo:
        """ATI — vendor/product/firmware identification (manual §6.2.15)."""
        resp = self._transact("ATI", timeout or self._t_query)
        raw = resp.lines[0] if resp.lines else ""
        fields = raw[2:].split(";") if raw.startswith("I:") else []
        return ModemInfo(raw=raw, fields=fields)

    def get_library_version(self, timeout: float | None = None) -> LibraryVersion:
        """AT-LIBV — identity/version of the underlying mioty protocol library."""
        resp = self._transact("AT-LIBV", timeout or self._t_query)
        raw = resp.lines[0] if resp.lines else ""
        _, _, rest = raw.partition(":")
        return LibraryVersion(raw=raw, fields=rest.split(";") if rest else [])

    # -- reset --------------------------------------------------------------

    def factory_reset(
        self, timeout: float | None = None, boot_timeout: float = 5.0
    ) -> ModemInfo | None:
        """ATZ — erase all data and restart (manual §6.2.16).

        Returns the post-reboot identification banner if it arrives within
        `boot_timeout`, else None (the banner is unsolicited, not a direct reply).
        """
        with self._lock:
            self._transact("ATZ", timeout or self._t_query)
            return self._read_boot_banner(boot_timeout)

    def reset(self, timeout: float | None = None, boot_timeout: float = 5.0) -> ModemInfo | None:
        """AT-RST — restart without erasing configuration (manual §6.2.9)."""
        with self._lock:
            self._transact("AT-RST", timeout or self._t_query)
            return self._read_boot_banner(boot_timeout)

    # -- sending data -----------------------------------------------------

    def send_unidirectional(self, payload: bytes, timeout: float | None = None) -> UplinkResult:
        """AT-U — unidirectional message with the mioty MAC layer (manual §6.2.2)."""
        cmd = f"AT-U={len(payload)}{HT}{_hex(payload)}{SUB}"
        resp = self._transact(cmd, timeout or self._t_tx)
        return UplinkResult(packet_counter=resp.int("MPCT"))

    def send_bidirectional(self, payload: bytes, timeout: float | None = None) -> DownlinkResult:
        """AT-B — bidirectional message with the mioty MAC layer; waits for the
        base station's response (manual §6.2.3)."""
        cmd = f"AT-B={len(payload)}{HT}{_hex(payload)}{SUB}"
        resp = self._transact(cmd, timeout or self._t_tx)
        return DownlinkResult(
            packet_counter=resp.int("MPCT"),
            acknowledged=resp.int("UACK") == 1,
            payload=resp.bytes("B"),
        )

    def send_unidirectional_mpf(
        self, mpf: int, payload: bytes, timeout: float | None = None
    ) -> UplinkResult:
        """AT-UMPF — unidirectional message with an Uplink MPF field (manual §6.2.4)."""
        if not 0 <= mpf <= 0xFF:
            raise ValueError("mpf must be a single byte (0-255)")
        body = bytes([mpf]) + payload
        cmd = f"AT-UMPF={len(body)}{HT}{_hex(body)}{SUB}"
        resp = self._transact(cmd, timeout or self._t_tx)
        return UplinkResult(packet_counter=resp.int("MPCT"))

    def send_bidirectional_mpf(
        self, mpf: int, payload: bytes, timeout: float | None = None
    ) -> DownlinkResult:
        """AT-BMPF — bidirectional message with an Uplink MPF field (manual §6.2.5)."""
        if not 0 <= mpf <= 0xFF:
            raise ValueError("mpf must be a single byte (0-255)")
        body = bytes([mpf]) + payload
        cmd = f"AT-BMPF={len(body)}{HT}{_hex(body)}{SUB}"
        resp = self._transact(cmd, timeout or self._t_tx)
        return DownlinkResult(
            packet_counter=resp.int("MPCT"),
            acknowledged=resp.int("UACK") == 1,
            payload=resp.bytes("BMPF"),
        )

    def send_unidirectional_raw(self, payload: bytes, timeout: float | None = None) -> UplinkResult:
        """AT-TU — unidirectional message without a MAC layer; add your own MAC
        framing if needed (manual §6.2.6)."""
        cmd = f"AT-TU={len(payload)}{HT}{_hex(payload)}{SUB}"
        resp = self._transact(cmd, timeout or self._t_tx)
        return UplinkResult(packet_counter=resp.int("MPCT"))

    def send_bidirectional_raw(
        self, payload: bytes, timeout: float | None = None
    ) -> DownlinkResult:
        """AT-TB — bidirectional message without a MAC layer (manual §6.2.7)."""
        cmd = f"AT-TB={len(payload)}{HT}{_hex(payload)}{SUB}"
        resp = self._transact(cmd, timeout or self._t_tx)
        return DownlinkResult(
            packet_counter=resp.int("MPCT"),
            acknowledged=resp.int("UACK") == 1,
            payload=resp.bytes("B"),
        )

    # -- radio configuration ------------------------------------------------

    def set_uplink_mode(self, mode: UplinkMode | int, timeout: float | None = None) -> UplinkMode:
        """AT-UM — uplink mode (manual §6.2.17, Table 4)."""
        resp = self._transact(f"AT-UM={int(mode)}", timeout or self._t_query)
        return UplinkMode(resp.int("UM", int(mode)))

    def set_sync_burst(self, enabled: bool, timeout: float | None = None) -> bool:
        """AT-US — uplink synchronisation burst, for base stations that can't
        always listen on all channels (manual §6.2.18)."""
        resp = self._transact(f"AT-US={int(enabled)}", timeout or self._t_query)
        return resp.int("US", int(enabled)) == 1

    def set_uplink_profile(
        self, profile: UplinkProfile | int, timeout: float | None = None
    ) -> UplinkProfile:
        """AT-UP — uplink profile (manual §6.2.19, Table 5)."""
        resp = self._transact(f"AT-UP={int(profile)}", timeout or self._t_query)
        return UplinkProfile(resp.int("UP", int(profile)))

    def set_uplink_tx_power(self, dbm: int, timeout: float | None = None) -> int:
        """AT-UTPL — uplink TX power in dBm, 0-14, default 14 (manual §6.2.20)."""
        if not 0 <= dbm <= 14:
            raise ValueError("dbm must be between 0 and 14")
        resp = self._transact(f"AT-UTPL={dbm}", timeout or self._t_query)
        return resp.int("UTPL", dbm)

    # -- MAC / network identity ----------------------------------------------

    def set_network_key(self, key: bytes, timeout: float | None = None) -> None:
        """AT-MNWK — write-only network encryption key. The MAC attach state
        must be Detached before this can be sent (manual §6.2.8)."""
        if len(key) != 16:
            raise ValueError("key must be exactly 16 bytes")
        cmd = f"AT-MNWK={len(key)}{HT}{_hex(key)}{SUB}"
        self._transact(cmd, timeout or self._t_query)

    def get_short_address(self, timeout: float | None = None, retries: int | None = None) -> bytes:
        """AT-MSAD? — the 2-byte MAC short address (manual §6.2.13)."""
        return self._query_bytes("AT-MSAD?", "MSAD", timeout, retries)

    def set_short_address(self, address: bytes, timeout: float | None = None) -> None:
        """AT-MSAD= — set the 2-byte MAC short address.

        Table 3 lists AT-MSAD as "Set/Get", but the manual only shows a worked
        example for the query form; this follows the same write convention used
        by every other Set/Get command in the table.
        """
        if len(address) != 2:
            raise ValueError("address must be exactly 2 bytes")
        cmd = f"AT-MSAD={len(address)}{HT}{_hex(address)}{SUB}"
        self._transact(cmd, timeout or self._t_query)

    def get_eui(self, timeout: float | None = None, retries: int | None = None) -> bytes:
        """AT-MEUI? — the module's unique EUI64 (manual §6.2.1)."""
        return self._query_bytes("AT-MEUI?", "MEUI", timeout, retries)

    def set_eui(self, eui: bytes, timeout: float | None = None) -> None:
        """AT-MEUI= — Table 3 lists this as "Set/Get EUI64", but the datasheet
        states the EUI64 is tied to the module and cannot be changed (datasheet
        §2.1.3); expect this to be rejected by the module.
        """
        if len(eui) != 8:
            raise ValueError("eui must be exactly 8 bytes")
        cmd = f"AT-MEUI={len(eui)}{HT}{_hex(eui)}{SUB}"
        self._transact(cmd, timeout or self._t_query)

    def set_ipv6_subnet_key(self, key: bytes, timeout: float | None = None) -> None:
        """AT-MIP6 — write-only MAC IPv6 subnet mask key (manual Table 3)."""
        cmd = f"AT-MIP6={len(key)}{HT}{_hex(key)}{SUB}"
        self._transact(cmd, timeout or self._t_query)

    def get_packet_counter(self, timeout: float | None = None, retries: int | None = None) -> int:
        """AT-MPCT? — read-only MAC packet counter (manual §6.2.14)."""
        return self._query_int("AT-MPCT?", "MPCT", timeout, retries)

    # -- attach / detach ------------------------------------------------------

    def attach_over_air(self, nonce: bytes, timeout: float | None = None) -> AttachResult:
        """AT-MAOA — attach over the air for bidirectional communication. Resets
        the packet counter to zero (manual §6.2.21)."""
        if len(nonce) != 4:
            raise ValueError("nonce must be exactly 4 bytes")
        cmd = f"AT-MAOA={len(nonce)}{HT}{_hex(nonce)}{SUB}"
        resp = self._transact(cmd, timeout or self._t_attach)
        return AttachResult(
            packet_counter=resp.int("MPCT"),
            acknowledged=resp.int("UACK") == 1,
            mac_state=AttachState(resp.int("MSTA", AttachState.DETACHED)),
        )

    def detach_over_air(self, nonce: bytes, timeout: float | None = None) -> AttachResult:
        """AT-MDOA — detach over the air from the base station (manual §6.2.22)."""
        if len(nonce) != 4:
            raise ValueError("nonce must be exactly 4 bytes")
        cmd = f"AT-MDOA={len(nonce)}{HT}{_hex(nonce)}{SUB}"
        resp = self._transact(cmd, timeout or self._t_attach)
        return AttachResult(
            packet_counter=resp.int("MPCT"),
            acknowledged=resp.int("UACK") == 1,
            mac_state=AttachState(resp.int("MSTA", AttachState.DETACHED)),
        )

    def attach_local(self, timeout: float | None = None) -> AttachState:
        """AT-MALO — attach locally, for unidirectional networks. Resets the
        packet counter to zero (manual §6.2.23).

        Uses the same (longer) timeout as the over-the-air attach commands:
        stress testing found this reliably takes more than a couple of
        seconds whenever it has real work to do, not just AT-MAOA/AT-MDOA.
        """
        resp = self._transact("AT-MALO", timeout or self._t_attach)
        return AttachState(resp.int("MSTA", AttachState.DETACHED))

    def detach_local(self, timeout: float | None = None) -> AttachState:
        """AT-MDLO — set the attached state to detached locally (manual §6.2.24).

        See attach_local() for why this uses the attach-family timeout.
        """
        resp = self._transact("AT-MDLO", timeout or self._t_attach)
        return AttachState(resp.int("MSTA", AttachState.DETACHED))

    # -- MAC header response flag (undocumented format) ------------------------

    def get_mac_header_response_flag(self, timeout: float | None = None) -> bool:
        """AT-MRDR? — get the MAC header response flag.

        CAVEAT: Table 3 lists "AT-MRDR | Set/Get MAC header response flag", but
        unlike every other command, the manual gives no worked example or
        payload format for it. This follows the query-suffix / bare-integer
        convention used by every other Set/Get flag command (e.g. AT-US) since
        that pattern is consistent throughout the rest of the protocol — but it
        has not been verified against real hardware.
        """
        resp = self._transact("AT-MRDR?", timeout or self._t_query)
        return resp.int("MRDR") == 1

    def set_mac_header_response_flag(self, enabled: bool, timeout: float | None = None) -> bool:
        """AT-MRDR= — see get_mac_header_response_flag() for the format caveat."""
        resp = self._transact(f"AT-MRDR={int(enabled)}", timeout or self._t_query)
        return resp.int("MRDR", int(enabled)) == 1

    # -- power / test modes -----------------------------------------------------

    def sleep(self, timeout: float | None = None) -> None:
        """AT%SLEEP — enter sleep mode. Wake with cancel()/wake() (manual §6.2.10)."""
        self._transact("AT%SLEEP", timeout or self._t_query)

    def enter_test_mode(self, timeout: float | None = None) -> None:
        """AT%MOD — continuously transmit a fixed test payload as fast as
        possible. Exit with exit_test_mode()/cancel() (manual §6.2.11)."""
        self._transact("AT%MOD", timeout or self._t_query)

    def cancel(self, timeout: float = 2.0) -> None:
        """Send "?" to cancel AT%SLEEP or AT%MOD (manual Table 3).

        A falling edge on RX is enough to wake the module from sleep, so unlike
        other commands this tolerates no response arriving in time (e.g. right
        after sleep(), before the module has actually gone to sleep) — a
        MiotyTimeoutError here is swallowed rather than raised.
        """
        try:
            self._write_and_collect(b"?", "?", timeout)
        except MiotyTimeoutError:
            pass

    wake = cancel
    exit_test_mode = cancel

    # -- UART baud rate --------------------------------------------------------

    def set_baud_rate(self, baud: int, timeout: float | None = None) -> int:
        """AT+IPR= — set the UART baud rate (manual §6.2.12).

        The module only applies the new rate after a reset() — call reset() and
        then apply_local_baudrate(baud) on this driver to match it afterwards.
        """
        resp = self._transact(f"AT+IPR={baud}", timeout or self._t_query)
        # The manual's own example shows the confirmation field spelled "-+IPR:",
        # not "-IPR:" like every other field — an apparent doc typo, handled here.
        return resp.int("+IPR", baud)

    def get_uart_baudrate_options(self, timeout: float | None = None) -> list[str]:
        """AT+IPR=? — available UART baud rates.

        The manual does not document the exact multi-value response format for
        this query, so the raw response lines are returned as-is.
        """
        resp = self._transact("AT+IPR=?", timeout or self._t_query)
        return resp.lines

    def apply_local_baudrate(self, baud: int) -> None:
        """Update this driver's local serial baudrate to match a module that has
        already confirmed a new AT+IPR value and been reset(). Does not talk to
        the module."""
        self._serial.baudrate = baud
