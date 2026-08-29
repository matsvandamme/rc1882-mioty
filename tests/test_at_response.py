from rc1882_mioty.client import AtResponse


def test_text_returns_value_for_present_field():
    resp = AtResponse(["-MSTA:4"])
    assert resp.text("MSTA") == "4"


def test_text_returns_none_for_missing_field():
    resp = AtResponse(["-MSTA:4"])
    assert resp.text("NOPE") is None


def test_text_returns_default_for_missing_field():
    resp = AtResponse([])
    assert resp.text("NOPE", "fallback") == "fallback"


def test_int_parses_plain_value():
    resp = AtResponse(["-MPCT:83"])
    assert resp.int("MPCT") == 83


def test_int_returns_default_when_missing():
    resp = AtResponse([])
    assert resp.int("MPCT", 7) == 7


def test_int_returns_default_when_unparseable():
    # e.g. a hex-payload field being misread as a plain int
    resp = AtResponse(["-MEUI:8\t0011223344556677\x1a"])
    assert resp.int("MEUI", -1) == -1


def test_bytes_decodes_hex_with_sub_terminator():
    resp = AtResponse(["-MEUI:8\t00124B001CBCE31B\x1a"])
    assert resp.bytes("MEUI") == bytes.fromhex("00124B001CBCE31B")


def test_bytes_decodes_hex_without_sub_terminator():
    # Not every response line in the manual includes the trailing SUB byte
    # (e.g. the AT-B/-TB payload examples) — must decode either way.
    resp = AtResponse(["-B:7\t48695468657265"])
    assert resp.bytes("B") == b"HiThere"


def test_bytes_returns_none_when_field_has_no_tab():
    # A plain integer field (no HT) must not be mistaken for a hex payload.
    resp = AtResponse(["-MSTA:4"])
    assert resp.bytes("MSTA") is None


def test_all_text_returns_every_occurrence_in_order():
    # -TXA appears twice in a real AT-U exchange: once for TX start, once for
    # TX finish (manual §6.2.2) — order and count both matter.
    resp = AtResponse(["-MPCT:1", "-TXA:1", "-TXA:0"])
    assert resp.all_text("TXA") == ["1", "0"]


def test_identification_line_with_no_leading_dash_is_parsed_like_other_fields():
    # "I:" lines have no leading "-", unlike every other field prefix.
    resp = AtResponse(["I:Radiocrafts;MIOTY1_2.0.0"])
    assert resp.text("I") == "Radiocrafts;MIOTY1_2.0.0"
