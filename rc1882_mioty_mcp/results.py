"""The one consistent {"ok": ...} result/error shape used by every MCP tool.

Per the building-python-mcp-servers skill: return structured errors, don't let
exceptions leak to the LLM client as opaque protocol errors, and pick one
shape everywhere so callers can reliably check `result["ok"]`.
"""

from __future__ import annotations

from typing import Any

from rc1882_mioty import (
    MiotyCommandError,
    MiotyIncompleteResponseError,
    MiotyTimeoutError,
)

_ERROR_KINDS: dict[type[Exception], str] = {
    MiotyTimeoutError: "timeout",
    MiotyCommandError: "command_error",
    MiotyIncompleteResponseError: "incomplete_response",
    ValueError: "invalid_argument",
}


def to_result(value: Any) -> dict[str, Any]:
    return {"ok": True, "result": value}


def to_error(exc: Exception) -> dict[str, Any]:
    kind = _ERROR_KINDS.get(type(exc), "mioty_error")
    error: dict[str, Any] = {"ok": False, "error_type": kind, "error": str(exc)}
    if isinstance(exc, MiotyCommandError):
        error["status"] = exc.status
        error["info_lines"] = exc.info_lines
    return error
