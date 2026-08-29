"""One real-hardware integration test, per the building-python-mcp-servers
skill: "keep at least one integration test that invokes the real wrapped
tool" — a green suite that only mocks the transport proves nothing about
whether the tools actually work against the real module.

Skipped unless RC1882_MIOTY_TEST_PORT is set, e.g.:
    RC1882_MIOTY_TEST_PORT=/dev/ttyUSB0 pytest tests/test_mcp_integration.py
"""

from __future__ import annotations

import json
import os

import pytest

from rc1882_mioty_mcp import ServerConfig, build_server

TEST_PORT = os.environ.get("RC1882_MIOTY_TEST_PORT")

pytestmark = pytest.mark.skipif(
    not TEST_PORT, reason="set RC1882_MIOTY_TEST_PORT to run against real hardware"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_get_modem_info_against_real_hardware():
    mcp = build_server(ServerConfig(port=TEST_PORT))
    mcp.modem.open()  # type: ignore[attr-defined]
    try:
        result = await mcp.call_tool("get_modem_info", {})
    finally:
        mcp.modem.close()  # type: ignore[attr-defined]

    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["result"]["vendor"], "expected a non-empty vendor string from real hardware"
    assert payload["result"]["raw"].startswith("I:")
