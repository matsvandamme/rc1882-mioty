"""Enums and lookup tables from the MIOTY1 User Manual (rev. 2.01).

References below are to sections/tables of MIOTY1_User_Manual.pdf.
"""

from __future__ import annotations

from enum import IntEnum

DEFAULT_BAUDRATE = 115200
STATUS_OK = 0

# Control characters used to frame binary payloads in AT commands (manual §6.1).
HT = "\t"  # Horizontal Tab (0x09) — separates a byte count from a hex payload.
SUB = "\x1a"  # Substitute (0x1A) — terminates a hex payload before <CR>.


class UplinkMode(IntEnum):
    """AT-UM values (manual Table 4)."""

    STANDARD_TRANSMISSION = 0
    RETRANSMISSION = 1  # More reliable but uses additional battery.
    LOW_DELAY = 2  # Reduced transmission time and reliability.


class UplinkProfile(IntEnum):
    """AT-UP values (manual Table 5)."""

    EU0 = 0
    EU1 = 1
    EU2 = 2
    US0 = 3


class AttachState(IntEnum):
    """-MSTA values (manual Table 6)."""

    DETACHED = 0
    OTA_DETACH_PENDING = 1
    OTA_ATTACHED = 2
    OTA_ATTACH_REQUESTED = 3
    LOCALLY_ATTACHED = 4


# -MNFO status codes: Core Library Information (manual Table 6).
MNFO_CODES: dict[int, str] = {
    3: "Argument Size Mismatch",
    4: "Argument Out of Range",
    5: "Buffer Size Insufficient",
    11: "Uplink Packing Error",
    12: "No Downlink Received",
    14: "CRC Error",
}

# -MERR status codes: MAC Error Information (manual Table 6).
MERR_CODES: dict[int, str] = {
    1: "Generic MAC Error",
    2: "MAC Framing Error",
    6: "End-point Not Attached",
    7: "Network Key Not Set",
    8: "Already Attached",
    10: "Downlink Not Available",
    12: "No Downlink Received",
    13: "Option not allowed",
    14: "Downlink Corrupted",
    15: "Factory Defaults Not Set (This message should never show)",
}
