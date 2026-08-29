"""Tests for the MCP server, using the same FakeSerial double as the core
library's tests — no real hardware needed. Covers the error contract, the
allow_transmit tool-gating, factory_reset's confirm requirement, and (per the
building-python-mcp-servers skill) that a slow tool doesn't block a
concurrent fast one.

These tests call tools directly via `mcp.call_tool(...)` without going
through the lifespan hook (FakeSerial doesn't require open() to function) —
that's deliberate: they exercise tool *logic*, not the real port lifecycle.
The lifespan/open/close path is exercised by the real-hardware integration
test in test_mcp_integration.py.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from rc1882_mioty_mcp import ServerConfig, build_server


async def call(mcp, name: str, **kwargs) -> dict:
    result = await mcp.call_tool(name, kwargs)
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_get_modem_info_returns_ok_result(fake_serial):
    fake_serial.queue_response(b"I:Radiocrafts;MIOTY1_2.0.0\r\n0\r\n")
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE"))
    result = await call(mcp, "get_modem_info")
    assert result == {
        "ok": True,
        "result": {
            "raw": "I:Radiocrafts;MIOTY1_2.0.0",
            "fields": ["Radiocrafts", "MIOTY1_2.0.0"],
            "vendor": "Radiocrafts",
        },
    }


@pytest.mark.anyio
async def test_get_eui_returns_hex_string(fake_serial):
    fake_serial.queue_response(b"-MEUI:8\t00124B001CBCE31B\x1a\r\n0\r\n")
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE"))
    result = await call(mcp, "get_eui")
    assert result == {"ok": True, "result": {"eui_hex": "00124b001cbce31b"}}


@pytest.mark.anyio
async def test_command_error_returns_error_shape_not_a_protocol_error(fake_serial):
    fake_serial.queue_response(b"AT!ERR:3\r\n2\r\n")
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE"))
    result = await call(mcp, "set_uplink_mode", mode=5)
    assert result["ok"] is False
    assert result["error_type"] == "command_error"
    assert result["status"] == 2


@pytest.mark.anyio
async def test_invalid_client_side_input_returns_error_shape(fake_serial):
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE"))
    result = await call(mcp, "set_uplink_tx_power", dbm=99)
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert fake_serial.writes == [], "must not touch hardware on a client-side validation failure"


@pytest.mark.anyio
async def test_factory_reset_refuses_without_confirm(fake_serial):
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE"))
    result = await call(mcp, "factory_reset", confirm=False)
    assert result["ok"] is False
    assert result["error_type"] == "confirmation_required"
    assert fake_serial.writes == [], "must not touch hardware without confirm=True"


@pytest.mark.anyio
async def test_factory_reset_proceeds_with_confirm(fake_serial):
    fake_serial.queue_response(b"0\r\nI:Radiocrafts;MIOTY1_2.0.0\r\n0\r\n")
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE"))
    result = await call(mcp, "factory_reset", confirm=True, boot_timeout=0.05)
    assert result["ok"] is True
    assert result["result"]["banner"]["vendor"] == "Radiocrafts"


@pytest.mark.anyio
async def test_transmit_tools_absent_when_allow_transmit_is_false(fake_serial):
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE", allow_transmit=False))
    names = {t.name for t in await mcp.list_tools()}
    assert "send_unidirectional" not in names
    assert "attach_over_air" not in names
    # Non-transmit tools are still present.
    assert "get_modem_info" in names
    assert "attach_local" in names


@pytest.mark.anyio
async def test_transmit_tools_present_when_allow_transmit_is_true(fake_serial):
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE", allow_transmit=True))
    names = {t.name for t in await mcp.list_tools()}
    assert "send_unidirectional" in names
    assert "attach_over_air" in names


@pytest.mark.anyio
async def test_send_unidirectional_encodes_payload_when_enabled(fake_serial):
    fake_serial.queue_response(b"-MPCT:1\r\n-TXA:1\r\n-TXA:0\r\n0\r\n")
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE", allow_transmit=True))
    result = await call(mcp, "send_unidirectional", payload_hex="48656c6c6f")
    assert result == {"ok": True, "result": {"packet_counter": 1}}
    assert fake_serial.writes == [b"AT-U=5\t48656C6C6F\x1a\r"]


@pytest.mark.anyio
async def test_invalid_hex_payload_returns_error_without_touching_hardware(fake_serial):
    mcp = build_server(ServerConfig(port="/dev/ttyFAKE", allow_transmit=True))
    result = await call(mcp, "send_unidirectional", payload_hex="not-hex")
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert fake_serial.writes == []


@pytest.mark.anyio
async def test_slow_tool_does_not_block_the_event_loop(fake_serial):
    """Per the skill: a timing assertion on the slow call alone can't prove
    the event loop isn't starved — a concurrent *unrelated* request must
    actually complete first.

    Note this deliberately races against list_tools(), not another
    modem-touching tool: RC1882Mioty's own RLock correctly serializes real
    hardware access (the AT protocol is genuinely one-command-at-a-time), so
    two tools that both touch the modem *should* wait for each other — that's
    not event-loop starvation, it's the intended hardware constraint. What
    this test actually proves is that v2's automatic worker-thread offload
    for sync handlers keeps the event loop itself free for protocol-level
    traffic (tool listing, pings) while a slow tool runs, independent of
    RC1882Mioty's own locking.
    """
    fake_serial.queue_response(b"-MPCT:1\r\n-TXA:1\r\n-TXA:0\r\n0\r\n")

    real_read = fake_serial.read

    def slow_read(size: int = 1) -> bytes:
        time.sleep(0.2)
        return real_read(size)

    fake_serial.read = slow_read

    mcp = build_server(ServerConfig(port="/dev/ttyFAKE", allow_transmit=True))

    slow_task = asyncio.create_task(call(mcp, "send_unidirectional", payload_hex="48656c6c6f"))
    await asyncio.sleep(0.02)  # let the slow call actually start

    fast_result = await asyncio.wait_for(mcp.list_tools(), timeout=0.5)
    assert any(t.name == "get_modem_info" for t in fast_result)
    assert not slow_task.done(), "list_tools() should finish while the slow tool is still running"

    slow_result = await slow_task
    assert slow_result == {"ok": True, "result": {"packet_counter": 1}}
