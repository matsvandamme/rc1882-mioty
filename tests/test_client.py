"""Client tests, using the FakeSerial double from conftest.py.

Several of these pin down exactly the failure modes found during hardware
stress testing (see rc1882_mioty.client's docstring) — they are regression
tests for real, observed hardware behavior, not just API-shape checks.
"""

from __future__ import annotations

import time

import pytest

from rc1882_mioty import (
    MiotyCommandError,
    MiotyIncompleteResponseError,
    MiotyTimeoutError,
    RC1882Mioty,
)


def test_get_modem_info_parses_identification_line(modem, fake_serial):
    fake_serial.queue_response(b"I:Radiocrafts;MIOTY1_2.0.0\r\n0\r\n")
    info = modem.get_modem_info()
    assert info.fields == ["Radiocrafts", "MIOTY1_2.0.0"]
    assert info.vendor == "Radiocrafts"


def test_get_eui_decodes_hex_on_the_first_try(modem, fake_serial):
    fake_serial.queue_response(b"-MEUI:8\t00124B001CBCE31B\x1a\r\n0\r\n")
    assert modem.get_eui() == bytes.fromhex("00124B001CBCE31B")
    assert len(fake_serial.writes) == 1, "must not retry when the first response is complete"


def test_get_eui_retries_once_when_field_is_missing_then_succeeds(modem, fake_serial):
    # Regression test for the exact failure mode found under stress testing:
    # the module reports success (status 0) but omits the requested field.
    fake_serial.queue_response(b"0\r\n")  # success, but no -MEUI field
    fake_serial.queue_response(b"-MEUI:8\t00124B001CBCE31B\x1a\r\n0\r\n")
    assert modem.get_eui() == bytes.fromhex("00124B001CBCE31B")
    assert len(fake_serial.writes) == 2, "must have retried exactly once"
    assert fake_serial.writes[0] == fake_serial.writes[1] == b"AT-MEUI?\r"


def test_get_eui_raises_incomplete_response_after_exhausting_retries(modem, fake_serial):
    fake_serial.queue_response(b"0\r\n")
    fake_serial.queue_response(b"0\r\n")  # default query_retries=1 -> 2 total attempts
    with pytest.raises(MiotyIncompleteResponseError) as exc_info:
        modem.get_eui()
    assert exc_info.value.field == "MEUI"
    assert len(fake_serial.writes) == 2


def test_query_retries_can_be_overridden_per_call(modem, fake_serial):
    fake_serial.queue_response(b"0\r\n")
    fake_serial.queue_response(b"0\r\n")
    fake_serial.queue_response(b"0\r\n")
    with pytest.raises(MiotyIncompleteResponseError):
        modem.get_eui(retries=2)
    assert len(fake_serial.writes) == 3


def test_command_error_raised_on_nonzero_status(modem, fake_serial):
    fake_serial.queue_response(b"AT!ERR:3\r\n2\r\n")
    with pytest.raises(MiotyCommandError) as exc_info:
        modem.set_uplink_mode(5)
    assert exc_info.value.status == 2
    assert exc_info.value.info_lines == ["AT!ERR:3"]


def test_timeout_raised_when_module_never_responds(modem, fake_serial):
    # No response queued at all.
    with pytest.raises(MiotyTimeoutError) as exc_info:
        modem.get_modem_info()
    assert exc_info.value.command == "ATI"


def test_send_unidirectional_encodes_payload_and_parses_packet_counter(modem, fake_serial):
    fake_serial.queue_response(b"-MPCT:1\r\n-TXA:1\r\n-TXA:0\r\n0\r\n")
    result = modem.send_unidirectional(b"HelloWorld")
    assert fake_serial.writes == [b"AT-U=10\t48656C6C6F576F726C64\x1a\r"]
    assert result.packet_counter == 1


def test_send_unidirectional_raw_has_no_packet_counter(modem, fake_serial):
    # AT-TU has no MAC layer, so there's no -MPCT field (manual §6.2.6).
    fake_serial.queue_response(b"-TXA:1\r\n-TXA:0\r\n0\r\n")
    result = modem.send_unidirectional_raw(b"hi")
    assert result.packet_counter is None


def test_send_bidirectional_decodes_downlink_payload(modem, fake_serial):
    fake_serial.queue_response(
        b"-MPCT:1\r\n-TXA:1\r\n-TXA:0\r\n-RXA:1\r\n-UACK:1\r\n-RXA:0\r\n-B:7\t48695468657265\r\n0\r\n"
    )
    result = modem.send_bidirectional(b"HelloWorld")
    assert result.acknowledged is True
    assert result.payload == b"HiThere"


def test_uplink_tx_power_rejects_out_of_range_value_without_touching_hardware(modem, fake_serial):
    with pytest.raises(ValueError):
        modem.set_uplink_tx_power(99)
    assert fake_serial.writes == []


def test_attach_local_uses_the_attach_timeout_not_the_query_timeout(fake_serial):
    # Regression test for the stress-testing finding: attach_local/detach_local
    # reliably need more than a couple of seconds. Prove the *relationship*
    # (uses the long timeout, not the short one) rather than pinning either
    # constant exactly.
    modem = RC1882Mioty(
        "/dev/ttyFAKE", default_timeout=0.02, attach_timeout=0.2, min_command_interval=0
    )
    modem.open()
    start = time.perf_counter()
    with pytest.raises(MiotyTimeoutError):
        modem.attach_local()
    elapsed = time.perf_counter() - start
    assert elapsed > 0.1, "attach_local() timed out too fast to have used attach_timeout"


def test_detach_local_uses_the_attach_timeout_not_the_query_timeout(fake_serial):
    modem = RC1882Mioty(
        "/dev/ttyFAKE", default_timeout=0.02, attach_timeout=0.2, min_command_interval=0
    )
    modem.open()
    start = time.perf_counter()
    with pytest.raises(MiotyTimeoutError):
        modem.detach_local()
    elapsed = time.perf_counter() - start
    assert elapsed > 0.1, "detach_local() timed out too fast to have used attach_timeout"


def test_min_command_interval_is_enforced_between_commands(fake_serial, mocker):
    sleeps: list[float] = []
    mocker.patch("rc1882_mioty.client.time.sleep", sleeps.append)
    modem = RC1882Mioty("/dev/ttyFAKE", default_timeout=1.0, min_command_interval=0.2)
    modem.open()
    fake_serial.queue_response(b"I:Radiocrafts;MIOTY1_2.0.0\r\n0\r\n")
    fake_serial.queue_response(b"I:Radiocrafts;MIOTY1_2.0.0\r\n0\r\n")

    modem.get_modem_info()
    assert sleeps == [], "the very first command must not be paced"

    modem.get_modem_info()
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.2, abs=0.05)


def test_min_command_interval_disabled_when_zero(fake_serial, mocker):
    sleeps: list[float] = []
    mocker.patch("rc1882_mioty.client.time.sleep", sleeps.append)
    modem = RC1882Mioty("/dev/ttyFAKE", default_timeout=1.0, min_command_interval=0)
    modem.open()
    fake_serial.queue_response(b"I:Radiocrafts;MIOTY1_2.0.0\r\n0\r\n")
    fake_serial.queue_response(b"I:Radiocrafts;MIOTY1_2.0.0\r\n0\r\n")

    modem.get_modem_info()
    modem.get_modem_info()
    assert sleeps == []


def test_reset_captures_post_reboot_banner(modem, fake_serial):
    # ATZ/AT-RST: the module acks the command, then reboots and emits an
    # unsolicited identification banner (manual §6.2.9, §6.2.16).
    fake_serial.queue_response(b"0\r\nI:Radiocrafts;MIOTY1_2.0.0\r\n0\r\n")
    info = modem.reset()
    assert info is not None
    assert info.fields == ["Radiocrafts", "MIOTY1_2.0.0"]


def test_reset_returns_none_if_boot_banner_never_arrives(modem, fake_serial):
    fake_serial.queue_response(b"0\r\n")  # ack only, no banner follows
    info = modem.reset(boot_timeout=0.02)
    assert info is None


def test_cancel_swallows_timeout_since_the_module_may_not_reply(modem, fake_serial):
    # No response queued — must not raise (manual notes a falling edge alone
    # can be enough to wake the module; a reply isn't guaranteed).
    modem.cancel(timeout=0.02)


def test_set_eui_requires_exactly_eight_bytes(modem, fake_serial):
    with pytest.raises(ValueError):
        modem.set_eui(b"\x00" * 7)
    assert fake_serial.writes == []


def test_set_network_key_requires_exactly_sixteen_bytes(modem, fake_serial):
    with pytest.raises(ValueError):
        modem.set_network_key(b"\x00" * 15)
    assert fake_serial.writes == []
