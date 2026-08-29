# py-RC1882-mioty

A Python driver — and optional [MCP](https://modelcontextprotocol.io) server — for the
[Radiocrafts RC1882CEF-MIOTY1](https://radiocrafts.com/products/mioty-network/) mioty®
radio module, built directly against its documented AT command interface.

mioty is a Low-Power Wide-Area Network (LPWAN) standard (ETSI TS 103 357) built for
massive, long-range IoT deployments — think smart metering, smart city sensors, and
industrial telemetry. The RC1882CEF-MIOTY1 is a UART-controlled radio module that speaks
mioty on your behalf; this project gives you a clean, typed Python API for it, plus a way
to let an LLM agent (Claude, or any other MCP-compatible client) drive it directly.

## Features

- **Full AT command coverage** — identification, sending data (with or without the mioty
  MAC layer, unidirectional or bidirectional), radio configuration, MAC attach/detach
  (locally or over the air), sleep/test modes, and UART baud rate control.
- **Hardened by real hardware stress testing**, not just read from the datasheet. Every
  timeout, retry, and pacing decision in this library exists because it was measured
  against a real module under load — see [Reliability design](#reliability-design) below.
- **Typed and tested**: `ruff` + `mypy --strict` clean, and a pytest suite (`FakeSerial`)
  that runs without any hardware attached.
- **Optional MCP server** exposing the driver as tools for an LLM agent, with real-RF
  transmit tools gated behind an explicit opt-in flag — see [MCP server](#mcp-server).

## Hardware

You need a Radiocrafts RC1882CEF-MIOTY1 module connected over USB-serial (this was
developed against a Silicon Labs CP210x USB-UART bridge; any adapter exposing a
`/dev/ttyUSB*` / `COM*` port at 115200 8N1 works). The AT command set implemented here is
documented in Radiocrafts' `MIOTY1 User Manual`, which is **not included in this repo** —
it's Radiocrafts' proprietary document, available from
[radiocrafts.com](https://radiocrafts.com/) (registration required). This project
independently documents the commands it implements below.

On Linux, make sure your user is in the `dialout` group to access the serial port without
root:
```bash
sudo usermod -aG dialout $USER   # log out/in afterward
```

## Installation

```bash
pip install -e .                 # core library only
pip install -e ".[dev]"          # + ruff, mypy, pytest
pip install -e ".[mcp]"          # + the MCP server
```

## Quick start

```python
from rc1882_mioty import RC1882Mioty

with RC1882Mioty("/dev/ttyUSB0") as modem:
    print(modem.get_modem_info())        # I:Radiocrafts;MIOTY1_2.0.0
    print(modem.get_eui().hex())         # the module's unique EUI64
    modem.send_unidirectional(b"HelloWorld")
```

Every method maps to one documented AT command and returns a typed result (a
`dataclass`, an `IntEnum`, or plain `bytes`/`int`) rather than a raw string. Errors are
raised as one of the typed exceptions below rather than returned as ambiguous strings —
see `rc1882_mioty/exceptions.py`.

## AT command reference

| AT command | Library method | Description |
|---|---|---|
| `ATI` | `get_modem_info()` | Vendor/product/firmware identification |
| `AT-LIBV` | `get_library_version()` | Version of the underlying mioty protocol library |
| `ATZ` | `factory_reset()` | Erase all data and restart |
| `AT-RST` | `reset()` | Restart without erasing configuration |
| `AT-U` | `send_unidirectional(payload)` | Unidirectional uplink with the mioty MAC layer |
| `AT-B` | `send_bidirectional(payload)` | Bidirectional uplink; waits for the base station's response |
| `AT-UMPF` | `send_unidirectional_mpf(mpf, payload)` | Unidirectional uplink with an Uplink MPF field |
| `AT-BMPF` | `send_bidirectional_mpf(mpf, payload)` | Bidirectional uplink with an Uplink MPF field |
| `AT-TU` | `send_unidirectional_raw(payload)` | Unidirectional uplink, no MAC layer |
| `AT-TB` | `send_bidirectional_raw(payload)` | Bidirectional uplink, no MAC layer |
| `AT-UM` | `set_uplink_mode(mode)` | Uplink mode: Standard / Retransmission / Low Delay |
| `AT-US` | `set_sync_burst(enabled)` | Uplink synchronisation burst (for gateways that can't listen on all channels) |
| `AT-UP` | `set_uplink_profile(profile)` | Uplink profile: EU0 / EU1 / EU2 / US0 |
| `AT-UTPL` | `set_uplink_tx_power(dbm)` | Uplink TX power, 0–14 dBm |
| `AT-MNWK` | `set_network_key(key)` | Set the network encryption key (write-only) |
| `AT-MSAD` | `get_short_address()` / `set_short_address(addr)` | The 2-byte MAC short address |
| `AT-MEUI` | `get_eui()` / `set_eui(eui)` | The module's EUI64 (factory-fixed; `set_eui` is expected to be rejected) |
| `AT-MIP6` | `set_ipv6_subnet_key(key)` | Set the MAC IPv6 subnet mask key (write-only) |
| `AT-MPCT` | `get_packet_counter()` | Read the MAC packet counter |
| `AT-MAOA` / `AT-MDOA` | `attach_over_air(nonce)` / `detach_over_air(nonce)` | MAC attach/detach over the air |
| `AT-MALO` / `AT-MDLO` | `attach_local()` / `detach_local()` | MAC attach/detach locally (unidirectional networks) |
| `AT-MRDR` | `get_mac_header_response_flag()` / `set_...()` | MAC header response flag — **the manual doesn't document this command's wire format**; implemented by inference from every other Set/Get command's convention |
| `AT%SLEEP` | `sleep()` | Enter sleep mode |
| `AT%MOD` | `enter_test_mode()` | Continuous modulated test transmission |
| `?` | `cancel()` / `wake()` / `exit_test_mode()` | Cancel sleep or test mode |
| `AT+IPR` | `set_baud_rate(baud)` / `get_uart_baudrate_options()` | UART baud rate control |

All commands run over a 115200 8N1 UART link with no flow control. Payloads cross the
Python API as `bytes` (the library handles the AT protocol's hex encoding internally).

## Reliability design

This library's defaults aren't guesses — they came from stress-testing the driver against
real hardware (thousands of rapid AT commands, and dozens of real over-the-air
transmissions):

- **`min_command_interval` (default 100ms)** — firing AT commands back-to-back with zero
  gap made the module intermittently report success while silently omitting the requested
  data. A 100ms gap between commands eliminated this outright.
- **`attach_timeout` covers `attach_local()`/`detach_local()` too**, not just the
  over-the-air variants — they were found to reliably need several seconds (up to ~2s
  observed, sometimes more) whenever they have real work to do.
- **`MiotyIncompleteResponseError`** with automatic single-retry on `get_eui()`,
  `get_short_address()`, and `get_packet_counter()` — covers the "module said OK but left
  the field out" failure mode found above; safe to retry since these are pure reads.
- **A real over-the-air uplink takes several seconds** (mioty's telegram splitting
  deliberately spreads sub-packets over time for interference robustness) — a 10-byte
  payload took ~4.7s in testing; a 200-byte one took over 30s. Size your timeouts
  accordingly if you change the defaults.
- **`threading.RLock`** serializes all hardware access internally — the AT protocol is
  fundamentally one-command-at-a-time, so this library is safe to call from multiple
  threads without external locking.

### A note on RF and duty cycle

The RC1882CEF-MIOTY1 transmits on a shared, regulated ISM band (868/915 MHz). European
sub-bands carry real duty-cycle limits under ETSI EN 300 220 (as low as 0.1%, up to 10%
depending on the exact channel) — this library does **not** enforce those limits for you.
If you're scripting repeated transmissions, pace them yourself according to the
regulations that apply in your region and to the sub-band you're using.

## MCP server

`rc1882_mioty_mcp` exposes the driver as tools for an LLM agent over the
[Model Context Protocol](https://modelcontextprotocol.io), built on the official `mcp`
Python SDK (v2, `MCPServer`).

**RF transmit tools are off by default.** `send_unidirectional`, `send_bidirectional`,
`attach_over_air`, and friends are only registered if the server is started with
`--allow-transmit` — an LLM client shouldn't be able to key up a real radio on a shared
band without an explicit operator opt-in. `factory_reset` additionally requires
`confirm=True` in the call itself, since it erases all device data.

### Running it standalone

```bash
rc1882-mioty-mcp --port /dev/ttyUSB0                    # safe tools only
rc1882-mioty-mcp --port /dev/ttyUSB0 --allow-transmit    # + real RF transmit tools
```

Or via environment variables (equivalent to the flags above):
```bash
export RC1882_MIOTY_PORT=/dev/ttyUSB0
export RC1882_MIOTY_BAUDRATE=115200          # optional, defaults to 115200
export RC1882_MIOTY_ALLOW_TRANSMIT=1         # optional, defaults to off
rc1882-mioty-mcp
```

### Tool reference

**Always available** (no RF, no data loss): `get_modem_info`, `get_library_version`,
`get_eui`, `get_short_address`, `get_packet_counter`, `get_mac_header_response_flag`,
`set_uplink_mode`, `set_uplink_profile`, `set_uplink_tx_power`, `set_sync_burst`,
`set_mac_header_response_flag`, `set_network_key`, `attach_local`, `detach_local`,
`sleep`, `wake`, `enter_test_mode`, `exit_test_mode`, `reset`.

**Requires `confirm=True` in the call:** `factory_reset(confirm, boot_timeout=5.0)`.

**Only registered with `--allow-transmit`:** `send_unidirectional`, `send_bidirectional`,
`send_unidirectional_mpf`, `send_bidirectional_mpf`, `send_unidirectional_raw`,
`send_bidirectional_raw`, `attach_over_air`, `detach_over_air`.

Every tool returns `{"ok": true, "result": {...}}` on success, or
`{"ok": false, "error_type": "...", "error": "..."}` on failure — `error_type` is one of
`timeout`, `command_error`, `incomplete_response`, `invalid_argument`,
`confirmation_required`, or `mioty_error`.

### Using it with an AI client

The server speaks standard MCP over stdio, so it works with any MCP-compatible client.
The registration shape is the same everywhere — a `command` plus `args`; only the config
file's location and wrapper key differ.

**Claude Code** (CLI):
```bash
claude mcp add rc1882-mioty -- rc1882-mioty-mcp --port /dev/ttyUSB0
```
Add `--allow-transmit` at the end of that command to enable the transmit tools too. See
`claude mcp list` / `claude mcp get rc1882-mioty` to check connection status. Since MCP
servers load at session start, restart your session (`claude -c` continues the current
conversation) after adding one for the tools to appear.

**Claude Desktop** — edit the config file for your OS and add an entry under
`mcpServers`:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "rc1882-mioty": {
      "command": "rc1882-mioty-mcp",
      "args": ["--port", "/dev/ttyUSB0"]
    }
  }
}
```
Restart Claude Desktop after editing.

**Cursor** — add to `~/.cursor/mcp.json` (available in every project) or
`<project>/.cursor/mcp.json` (this project only):
```json
{
  "mcpServers": {
    "rc1882-mioty": {
      "command": "rc1882-mioty-mcp",
      "args": ["--port", "/dev/ttyUSB0"]
    }
  }
}
```

**Windsurf** — add the same block to `~/.codeium/windsurf/mcp_config.json` (or use the
MCPs icon in Cascade's panel → *Configure*, which opens this file directly).

**Any other MCP client** (Zed, Continue.dev, VS Code agent mode, etc.) — look for an
`mcpServers`-style config accepting a `command`/`args` pair and use the same
`rc1882-mioty-mcp --port /dev/ttyUSB0` invocation; consult that client's own docs for the
exact file location and key name.

## Development

```bash
ruff check rc1882_mioty rc1882_mioty_mcp
mypy rc1882_mioty rc1882_mioty_mcp
pytest                                              # mocked, no hardware needed
RC1882_MIOTY_TEST_PORT=/dev/ttyUSB0 pytest tests/test_mcp_integration.py   # real hardware
```

`test.py` and `stress_test.py` at the repo root are interactive/manual tools for playing
with and stress-testing a connected module — not part of the pytest suite.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) — free for any noncommercial use;
commercial use requires a separate arrangement with the copyright holder. Note this
project is independent of, and not affiliated with, Radiocrafts AS or the mioty alliance;
using the RC1882CEF-MIOTY1 module in an end product is subject to a separate mioty IPR
license fee from [Sisvel](https://www.sisvel.com/), per Radiocrafts' own datasheet.
