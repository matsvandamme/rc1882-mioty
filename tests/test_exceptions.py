from rc1882_mioty import MiotyCommandError, MiotyIncompleteResponseError, MiotyTimeoutError


def test_timeout_error_message_includes_command_and_duration():
    err = MiotyTimeoutError("AT-MEUI?", 2.0)
    assert "AT-MEUI?" in str(err)
    assert "2.0" in str(err)


def test_incomplete_response_error_message_includes_command_and_field():
    err = MiotyIncompleteResponseError("AT-MEUI?", "MEUI")
    assert "AT-MEUI?" in str(err)
    assert "MEUI" in str(err)


def test_command_error_decodes_documented_mnfo_code():
    err = MiotyCommandError("AT-UM=5", 4, ["-MNFO:4"])
    assert err.status == 4
    assert "Argument Out of Range" in str(err)


def test_command_error_decodes_documented_merr_code():
    err = MiotyCommandError("AT-U=10\t...", 6, ["-MERR:6"])
    assert "End-point Not Attached" in str(err)


def test_command_error_does_not_invent_a_meaning_for_undocumented_at_err():
    # AT!ERR is not documented anywhere in the manual (found empirically on
    # real hardware) — the library must surface it raw, not guess at English
    # text for it the way it does for -MNFO/-MERR.
    err = MiotyCommandError("AT-UM=5", 2, ["AT!ERR:3"])
    assert err.status == 2
    assert err.info_lines == ["AT!ERR:3"]
    # None of the documented MNFO/MERR descriptions should leak in here.
    assert "Argument" not in str(err)
    assert "Attached" not in str(err)
    # But the raw line must still be visible for debugging.
    assert "AT!ERR:3" in str(err)


def test_command_error_with_no_info_lines_still_reports_status():
    err = MiotyCommandError("AT-MDLO", 1, [])
    assert "1" in str(err)
