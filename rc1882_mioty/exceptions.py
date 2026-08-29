"""Exceptions raised by the RC1882-MIOTY1 AT command driver."""

from __future__ import annotations


class MiotyError(Exception):
    """Base class for all errors raised by this library."""


class MiotyTimeoutError(MiotyError):
    """No terminating status line was received from the module in time."""

    def __init__(self, command: str, timeout: float):
        self.command = command
        self.timeout = timeout
        super().__init__(f"Timed out after {timeout:.1f}s waiting for a response to {command!r}")


class MiotyIncompleteResponseError(MiotyError):
    """The module reported success (status 0) but the expected field was
    missing from its response.

    Observed under sustained rapid-fire command load (see rc1882_mioty stress
    testing notes) — a 100ms minimum gap between commands eliminated it in
    practice, but it's still reported distinctly (rather than as a generic
    MiotyError) so callers can specifically retry it: for a read-only query,
    retrying has no side effects.
    """

    def __init__(self, command: str, field: str):
        self.command = command
        self.field = field
        super().__init__(
            f"{command!r} reported success but did not include the expected {field!r} field"
        )


class MiotyCommandError(MiotyError):
    """The module responded with a non-zero status code.

    Attributes:
        command: The AT command that was sent (without the trailing CR).
        status: The non-zero status code reported by the module.
        info_lines: Any response lines received before the status line
            (e.g. "-MNFO:4" or "-MERR:7"), which usually explain the failure.
    """

    def __init__(self, command: str, status: int, info_lines: list[str]):
        self.command = command
        self.status = status
        self.info_lines = info_lines

        detail = _describe_status(status, info_lines)
        message = f"AT command {command!r} failed with status {status}"
        if detail:
            message += f" ({detail})"
        if info_lines:
            message += f" — response: {info_lines}"
        super().__init__(message)


def _describe_status(status: int, info_lines: list[str]) -> str | None:
    """Best-effort human-readable explanation using the -MNFO/-MERR tables (manual §7).

    Note: some failures (e.g. an out-of-range AT-UM value) instead report an
    "AT!ERR:<code>" info line, which is not documented anywhere in the manual —
    that code space is intentionally left undecoded here rather than guessed at.
    The raw info_lines are still included in the exception message either way.
    """
    from . import constants

    for line in info_lines:
        name, _, value = line.partition(":")
        name = name.lstrip("-")
        if name == "MNFO" and value.isdigit():
            return constants.MNFO_CODES.get(int(value))
        if name == "MERR" and value.isdigit():
            return constants.MERR_CODES.get(int(value))
    return None
