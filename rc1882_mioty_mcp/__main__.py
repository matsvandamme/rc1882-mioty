"""CLI entrypoint. All argv/env parsing lives here — never at import time in
server.py — so the rest of the package stays importable and testable without
a real port or real command-line arguments."""

from __future__ import annotations

import argparse
import os

from rc1882_mioty.constants import DEFAULT_BAUDRATE

from .config import ServerConfig
from .server import build_server


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        default=os.environ.get("RC1882_MIOTY_PORT"),
        help="Serial port the module is connected to (or set RC1882_MIOTY_PORT)",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=int(os.environ.get("RC1882_MIOTY_BAUDRATE", DEFAULT_BAUDRATE)),
    )
    parser.add_argument(
        "--allow-transmit",
        action="store_true",
        default=_bool_env("RC1882_MIOTY_ALLOW_TRANSMIT"),
        help=(
            "Register the RF transmit tools (send_unidirectional, etc.). "
            "Off by default — these emit real RF on a shared ISM band. "
            "Also settable via RC1882_MIOTY_ALLOW_TRANSMIT=1."
        ),
    )
    args = parser.parse_args()

    if not args.port:
        parser.error("--port is required (or set RC1882_MIOTY_PORT)")

    config = ServerConfig(
        port=args.port, baudrate=args.baudrate, allow_transmit=args.allow_transmit
    )
    mcp = build_server(config)
    mcp.run()


if __name__ == "__main__":
    main()
