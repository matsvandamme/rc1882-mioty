"""Server configuration. Kept as a plain object rather than module globals so
`build_server()` stays a pure factory, testable without real argv/env — see
the building-python-mcp-servers skill's "no module-level global state"."""

from __future__ import annotations

from dataclasses import dataclass

from rc1882_mioty.constants import DEFAULT_BAUDRATE


@dataclass(frozen=True)
class ServerConfig:
    port: str
    baudrate: int = DEFAULT_BAUDRATE
    allow_transmit: bool = False
