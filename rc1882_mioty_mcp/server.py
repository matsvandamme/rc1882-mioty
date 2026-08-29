"""MCP server exposing RC1882Mioty as tools for an LLM client.

Built against `mcp` v2's `MCPServer` (the v1 `FastMCP` name was removed in
mcp>=2; see https://py.sdk.modelcontextprotocol.io/v2/migration/). Tool
handlers are plain sync functions — v2 runs every sync handler on a worker
thread automatically (`anyio.to_thread.run_sync`), so a multi-second AT
command (a real mioty uplink is ~5s; the manual's oversized-payload boundary
case took 37s during stress testing) does not block the protocol event loop
or other concurrent tool calls. RC1882Mioty already serializes hardware
access internally via its own RLock, so concurrent tool calls from multiple
worker threads are safe — they simply queue at the lock the way the one-
command-at-a-time AT protocol requires.

RF transmit tools are only registered when `config.allow_transmit` is set —
see rc1882_mioty_mcp/config.py and the project plan for why.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer

from rc1882_mioty import (
    AttachResult,
    AttachState,
    DownlinkResult,
    LibraryVersion,
    MiotyError,
    ModemInfo,
    RC1882Mioty,
    UplinkResult,
)

from .config import ServerConfig
from .results import to_error, to_result


def _safe(fn: Callable[[], Any]) -> dict[str, Any]:
    """Run `fn`, converting any MiotyError/ValueError into the error shape.

    Every tool body is a one-liner around this — see results.py for the
    contract every tool follows: {"ok": True, "result": ...} or
    {"ok": False, "error_type": ..., "error": ...}.
    """
    try:
        return to_result(fn())
    except (MiotyError, ValueError) as e:
        return to_error(e)


def _do(action: Callable[[], None], value: Any) -> Any:
    """Run a side-effecting call that returns None, then return `value`."""
    action()
    return value


def _from_hex(name: str, value: str) -> bytes:
    try:
        return bytes.fromhex(value)
    except ValueError as e:
        raise ValueError(f"{name} must be a valid hex string: {e}") from e


def _modem_info(info: ModemInfo) -> dict[str, Any]:
    return {"raw": info.raw, "fields": info.fields, "vendor": info.vendor}


def _library_version(v: LibraryVersion) -> dict[str, Any]:
    return {"raw": v.raw, "fields": v.fields, "vendor": v.vendor}


def _uplink_result(r: UplinkResult) -> dict[str, Any]:
    return {"packet_counter": r.packet_counter}


def _downlink_result(r: DownlinkResult) -> dict[str, Any]:
    return {
        "packet_counter": r.packet_counter,
        "acknowledged": r.acknowledged,
        "payload_hex": r.payload.hex() if r.payload is not None else None,
    }


def _attach_result(r: AttachResult) -> dict[str, Any]:
    return {
        "packet_counter": r.packet_counter,
        "acknowledged": r.acknowledged,
        "mac_state": r.mac_state.name,
        "mac_state_value": int(r.mac_state),
    }


def _attach_state(state: AttachState) -> dict[str, Any]:
    return {"mac_state": state.name, "mac_state_value": int(state)}


def build_server(config: ServerConfig) -> MCPServer:
    """Build (but do not run) the MCP server for one RC1882Mioty connection.

    A pure factory — no module-level state, no argv/env parsing here (that
    belongs in __main__.py) — so this is importable and testable with a fake
    serial port and no real config.
    """
    modem = RC1882Mioty(config.port, baudrate=config.baudrate)

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        modem.open()
        try:
            yield
        finally:
            modem.close()

    mcp = MCPServer("rc1882-mioty", lifespan=lifespan)
    # MCPServer has no public hook to invoke `lifespan` outside of a real
    # client session, so anything that needs the connection open/closed
    # without a full transport (e.g. the hardware integration test) drives
    # this directly instead.
    mcp.modem = modem  # type: ignore[attr-defined]

    # -- identification ---------------------------------------------------

    @mcp.tool()
    def get_modem_info() -> dict:
        """Vendor/product/firmware identification (AT command: ATI)."""
        return _safe(lambda: _modem_info(modem.get_modem_info()))

    @mcp.tool()
    def get_library_version() -> dict:
        """Identity/version of the underlying mioty protocol library (AT-LIBV)."""
        return _safe(lambda: _library_version(modem.get_library_version()))

    @mcp.tool()
    def get_eui() -> dict:
        """The module's unique EUI64 as a hex string (AT-MEUI?)."""
        return _safe(lambda: {"eui_hex": modem.get_eui().hex()})

    @mcp.tool()
    def get_short_address() -> dict:
        """The 2-byte MAC short address as a hex string (AT-MSAD?)."""
        return _safe(lambda: {"address_hex": modem.get_short_address().hex()})

    @mcp.tool()
    def get_packet_counter() -> dict:
        """Read-only MAC packet counter (AT-MPCT?)."""
        return _safe(lambda: {"count": modem.get_packet_counter()})

    @mcp.tool()
    def get_mac_header_response_flag() -> dict:
        """MAC header response flag. NOTE: AT-MRDR's wire format is not
        documented in the manual — see RC1882Mioty.get_mac_header_response_flag."""
        return _safe(lambda: {"enabled": modem.get_mac_header_response_flag()})

    # -- radio configuration ------------------------------------------------

    @mcp.tool()
    def set_uplink_mode(mode: int) -> dict:
        """Set uplink mode: 0=Standard, 1=Retransmission, 2=Low Delay (AT-UM)."""
        return _safe(
            lambda: {"mode": (m := modem.set_uplink_mode(mode)).name, "mode_value": int(m)}
        )

    @mcp.tool()
    def set_uplink_profile(profile: int) -> dict:
        """Set uplink profile: 0=EU0, 1=EU1, 2=EU2, 3=US0 (AT-UP)."""
        return _safe(
            lambda: {
                "profile": (p := modem.set_uplink_profile(profile)).name,
                "profile_value": int(p),
            }
        )

    @mcp.tool()
    def set_uplink_tx_power(dbm: int) -> dict:
        """Set uplink TX power in dBm, 0-14, default 14 (AT-UTPL)."""
        return _safe(lambda: {"dbm": modem.set_uplink_tx_power(dbm)})

    @mcp.tool()
    def set_sync_burst(enabled: bool) -> dict:
        """Set uplink synchronisation burst mode (AT-US)."""
        return _safe(lambda: {"enabled": modem.set_sync_burst(enabled)})

    @mcp.tool()
    def set_mac_header_response_flag(enabled: bool) -> dict:
        """See get_mac_header_response_flag for the format caveat (AT-MRDR)."""
        return _safe(lambda: {"enabled": modem.set_mac_header_response_flag(enabled)})

    # -- MAC / network identity (local, no RF) -------------------------------

    @mcp.tool()
    def set_network_key(key_hex: str) -> dict:
        """Set the 16-byte network encryption key (AT-MNWK). Write-only —
        the module never returns the current key. The MAC attach state must
        be Detached before this can be sent; call detach_local or
        detach_over_air first if the module is already attached."""
        return _safe(
            lambda: _do(
                lambda: modem.set_network_key(_from_hex("key_hex", key_hex)),
                {"key_set": True},
            )
        )

    # -- attach / detach (local, no RF) ------------------------------------

    @mcp.tool()
    def attach_local() -> dict:
        """Attach locally, for unidirectional networks. Resets the packet
        counter to zero (AT-MALO)."""
        return _safe(lambda: _attach_state(modem.attach_local()))

    @mcp.tool()
    def detach_local() -> dict:
        """Set the attached state to detached locally (AT-MDLO)."""
        return _safe(lambda: _attach_state(modem.detach_local()))

    # -- power / test modes -----------------------------------------------

    @mcp.tool()
    def sleep() -> dict:
        """Enter sleep mode. Wake with the wake tool (AT%SLEEP)."""
        return _safe(lambda: _do(modem.sleep, {"sleeping": True}))

    @mcp.tool()
    def wake() -> dict:
        """Wake the module from sleep, or cancel test mode."""
        return _safe(lambda: _do(modem.wake, {"awake": True}))

    @mcp.tool()
    def enter_test_mode() -> dict:
        """Continuously transmit a fixed test payload as fast as possible
        (AT%MOD). Emits real RF — this is always available regardless of
        allow_transmit since it's a fixed, bounded test pattern, not
        arbitrary application data."""
        return _safe(lambda: _do(modem.enter_test_mode, {"test_mode": True}))

    @mcp.tool()
    def exit_test_mode() -> dict:
        """Exit continuous test mode."""
        return _safe(lambda: _do(modem.exit_test_mode, {"test_mode": False}))

    # -- reset --------------------------------------------------------------

    @mcp.tool()
    def reset(boot_timeout: float = 5.0) -> dict:
        """Restart without erasing configuration (AT-RST). Returns the
        post-reboot identification banner if it arrives in time."""
        return _safe(
            lambda: {
                "banner": _modem_info(info)
                if (info := modem.reset(boot_timeout=boot_timeout))
                else None
            }
        )

    @mcp.tool()
    def factory_reset(confirm: bool, boot_timeout: float = 5.0) -> dict:
        """Erase ALL device data and restart (ATZ). Destructive — requires
        confirm=True or it returns an error instead of executing."""
        if not confirm:
            return {
                "ok": False,
                "error_type": "confirmation_required",
                "error": "factory_reset erases all device data. Call again with confirm=True.",
            }
        return _safe(
            lambda: {
                "banner": _modem_info(info)
                if (info := modem.factory_reset(boot_timeout=boot_timeout))
                else None
            }
        )

    # -- RF transmit tools: opt-in only -------------------------------------

    if config.allow_transmit:

        @mcp.tool()
        def send_unidirectional(payload_hex: str) -> dict:
            """Send a unidirectional uplink with the mioty MAC layer (AT-U).
            Emits real RF. `payload_hex` is the payload as a hex string."""
            return _safe(
                lambda: _uplink_result(
                    modem.send_unidirectional(_from_hex("payload_hex", payload_hex))
                )
            )

        @mcp.tool()
        def send_bidirectional(payload_hex: str) -> dict:
            """Send a bidirectional uplink and wait for the base station's
            response (AT-B). Emits real RF."""
            return _safe(
                lambda: _downlink_result(
                    modem.send_bidirectional(_from_hex("payload_hex", payload_hex))
                )
            )

        @mcp.tool()
        def send_unidirectional_mpf(mpf: int, payload_hex: str) -> dict:
            """Unidirectional uplink with an Uplink MPF field, 0-255 (AT-UMPF).
            Emits real RF."""
            return _safe(
                lambda: _uplink_result(
                    modem.send_unidirectional_mpf(mpf, _from_hex("payload_hex", payload_hex))
                )
            )

        @mcp.tool()
        def send_bidirectional_mpf(mpf: int, payload_hex: str) -> dict:
            """Bidirectional uplink with an Uplink MPF field (AT-BMPF). Emits real RF."""
            return _safe(
                lambda: _downlink_result(
                    modem.send_bidirectional_mpf(mpf, _from_hex("payload_hex", payload_hex))
                )
            )

        @mcp.tool()
        def send_unidirectional_raw(payload_hex: str) -> dict:
            """Unidirectional uplink with no MAC layer (AT-TU). Emits real RF."""
            return _safe(
                lambda: _uplink_result(
                    modem.send_unidirectional_raw(_from_hex("payload_hex", payload_hex))
                )
            )

        @mcp.tool()
        def send_bidirectional_raw(payload_hex: str) -> dict:
            """Bidirectional uplink with no MAC layer (AT-TB). Emits real RF."""
            return _safe(
                lambda: _downlink_result(
                    modem.send_bidirectional_raw(_from_hex("payload_hex", payload_hex))
                )
            )

        @mcp.tool()
        def attach_over_air(nonce_hex: str) -> dict:
            """Attach over the air for bidirectional communication (AT-MAOA).
            `nonce_hex` must be exactly 4 bytes of hex. Emits real RF."""
            return _safe(
                lambda: _attach_result(modem.attach_over_air(_from_hex("nonce_hex", nonce_hex)))
            )

        @mcp.tool()
        def detach_over_air(nonce_hex: str) -> dict:
            """Detach over the air from the base station (AT-MDOA). Emits real RF."""
            return _safe(
                lambda: _attach_result(modem.detach_over_air(_from_hex("nonce_hex", nonce_hex)))
            )

    return mcp
