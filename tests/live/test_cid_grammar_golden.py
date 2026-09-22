"""The client-order-id grammar, pinned as LITERAL strings.

A client order id is state that lives at the broker. A GTC stop placed by one
build has to be recognised by the next build, or the next build never manages
it and places a second one: a duplicated protective order, and on Webull the
"reverse an existing position" rejection. Every other test of these helpers
builds its expected value by calling the same helpers, so a change to the
format changes both sides and the test still passes. These do not: the strings
below were produced by the code on 2026-09-20 and are written out by hand.

If one of these fails, the format changed. Orders already resting at a broker
carry the OLD format. Do not update the literal unless the change ships with a
way to keep recognising the old ids.
"""
import pytest

from dqengine.live import executor as be
from dqengine.live import journal

# Synthetic on purpose: a real deployment or connection id is a hash input
# here, and the literals below are its output. These two were made up, and
# every literal was re-derived from them by the code that ships.
DEP = "11111111-2222-4333-8444-555555555555"
CONN = "66666666-7777-4888-8999-aaaaaaaaaaaa"


class _FixedUuid:
    hex = "abcdef0123456789abcdef0123456789"


@pytest.fixture()
def fixed_uuid(monkeypatch):
    monkeypatch.setattr(be.uuid, "uuid4", lambda: _FixedUuid)


# ---- market orders: deterministic, because the journal's UNIQUE(conn, cid) is the duplicate gate

def test_market_cid_is_a_fixed_hash_of_the_intent():
    assert journal.market_cid(CONN, "2026-09-21", "TQQQ", "buy", 100.0, 3) == "sl-mkt-TQQQ-ba34f48f"
    assert journal.market_cid(CONN, "2026-09-21", "TQQQ", "sell", 12.5, 0) == "sl-mkt-TQQQ-2385e311"


def test_market_cid_depends_on_how_the_quantity_is_spelled():
    """The quantity goes into the hash through str(): 100.0 hashes as
    "100.0" and 100 as "100". Two passes that compute the same intent must
    spell it the same way, or the second pass gets a different id, the
    journal sees a new order, and the account buys twice -- the
    duplicate-order class of bug. Changing the type the caller passes is a
    format change."""
    as_float = journal.market_cid(CONN, "2026-09-21", "TQQQ", "buy", 100.0, 3)
    as_int = journal.market_cid(CONN, "2026-09-21", "TQQQ", "buy", 100, 3)
    assert as_float == "sl-mkt-TQQQ-ba34f48f"
    assert as_int == "sl-mkt-TQQQ-5e75cc4f"
    assert as_float != as_int


def test_market_cid_changes_with_every_field():
    base = ("c", "2026-09-21", "TQQQ", "buy", 100.0, 3)
    seen = {journal.market_cid(*base)}
    for i, other in enumerate(("c2", "2026-09-22", "QQQ", "sell", 101.0, 4)):
        args = list(base)
        args[i] = other
        seen.add(journal.market_cid(*args))
    assert len(seen) == 7


# ---- exit prefixes

def test_take_profit_prefix_is_byte_identical_to_the_original_format():
    # 18 characters of the deployment id, no symbol, no rule token: never changed
    assert be._exit_cid_prefix(DEP, "limit", "TQQQ") == "sl-tp-11111111-2222-4333"
    assert be._exit_cid_prefix(DEP, "limit", "GOOGL", "r-take-profit-2") == "sl-tp-11111111-2222-4333"


@pytest.mark.parametrize("kind,code", [("stop", "stp"), ("stop_limit", "stl"), ("trailing_stop", "trl")])
def test_other_exit_prefixes_carry_symbol_short_dep_id_and_rule_token(kind, code):
    assert be._exit_cid_prefix(DEP, kind, "TQQQ") == f"sl-{code}-TQQQ-1111"
    assert be._exit_cid_prefix(DEP, kind, "GOOGL", "r-take-profit-2") == f"sl-{code}-GOOGL-1111-bm2"


def test_entry_prefixes_are_their_own_family():
    assert be._entry_cid_prefix(DEP, "limit", "TQQQ", "r7") == "en-lmt-TQQQ-1111-h8k"
    assert be._entry_cid_prefix(DEP, "stop", "TQQQ", "r7") == "en-stp-TQQQ-1111-h8k"
    assert be._entry_cid_prefix(DEP, "stop_limit", "TQQQ", "r7") == "en-stl-TQQQ-1111-h8k"


def test_prefix_families_the_matching_code_relies_on():
    assert be._EXIT_CID_PREFIXES == ("sl-tp-", "sl-stp-", "sl-stl-", "sl-trl-")
    assert be._ENTRY_CID_PREFIXES == ("en-lmt-", "en-stp-", "en-stl-")
    # reconcile() decides an open order is OURS by these two prefixes
    assert all(p.startswith(("sl-", "en-")) for p in be._EXIT_CID_PREFIXES + be._ENTRY_CID_PREFIXES)
    assert journal.market_cid(CONN, "2026-09-21", "TQQQ", "buy", 1.0, 0).startswith("sl-")


def test_rule_token_is_a_three_character_crc32_in_base36():
    assert be._rule_token("r-take-profit-2") == "-bm2"
    assert be._rule_token("ir:exit_stop") == "-bze"
    assert be._rule_token(None) == "" and be._rule_token("") == ""


# ---- level tokens

@pytest.mark.parametrize("value,token", [
    (0, "00000"), (0.01, "00001"), (2.03, "0005n"), (58.17, "004hl"),
    (380.0045, "00tbk"),          # quantised to 380.00 before encoding
    (604661.75, "zzzzz"),         # the largest level five base36 digits hold
])
def test_level_tokens(value, token):
    assert be._encode_level(value) == token


def test_a_level_too_large_for_the_token_is_left_out_not_truncated():
    assert be._encode_level(604661.76) is None


def test_quantising_is_what_makes_a_round_trip_stable():
    assert be._quantize_level(380.0045) == 380.0
    assert be._decode_level("sl-stp-TQQQ-1111-0m4-abcd-L003kq") == 46.34
    assert be._decode_stop_limit_legs("sl-stl-GOOGL-1111-0m4-abcd-B00d9s00d7x") == (172.0, 171.33)
    assert be._decode_level("sl-stp-TQQQ-1111-0m4-abcd") is None
    assert be._decode_stop_limit_legs("sl-stl-GOOGL-1111-abcd-L003kq") == (None, None)


# ---- whole ids

def test_whole_client_order_ids(fixed_uuid):
    tp = be._cid_with_level(be._exit_cid_prefix(DEP, "limit", "TQQQ"), "limit", 58.17, None, None)
    stop = be._cid_with_level(be._exit_cid_prefix(DEP, "stop", "TQQQ", "r1"), "stop", None, 46.54, None)
    stl = be._cid_with_level(be._exit_cid_prefix(DEP, "stop_limit", "GOOGL", "r1"),
                             "stop_limit", 171.25, 172.0, None)
    trl = be._cid_with_level(be._exit_cid_prefix(DEP, "trailing_stop", "TQQQ", "r1"),
                             "trailing_stop", None, None, 0.05)
    entry = be._cid_with_level(be._entry_cid_prefix(DEP, "stop_limit", "GOOGL", "r1"),
                               "stop_limit", 171.25, 172.0, None)
    assert tp == "sl-tp-11111111-2222-4333-abcdef"             # 6 hex chars, no level token
    assert stop == "sl-stp-TQQQ-1111-fu9-abcd-L003la"          # 4 hex chars, -L<stop>
    assert stl == "sl-stl-GOOGL-1111-fu9-abcd-B00d9s00d7p"     # -B<stop><limit>, both legs
    assert trl == "sl-trl-TQQQ-1111-fu9-abcd-L000dw"           # -L<trail x 100>
    assert entry == "en-stl-GOOGL-1111-fu9-abcd-B00d9s00d7p"
    # and each decodes back to the level it was built from
    assert be._decode_level(stop) == 46.54
    assert be._decode_stop_limit_legs(stl) == (172.0, 171.25)
    assert be._decode_level(trl) == 5.0


def test_every_id_fits_the_brokers_forty_characters(fixed_uuid):
    assert be.MAX_CID_LEN == 40
    longest = be._cid_with_level(be._exit_cid_prefix(DEP, "stop_limit", "GOOGL", "r1"),
                                 "stop_limit", 171.25, 172.0, None)
    assert len(longest) == 38 <= be.MAX_CID_LEN


def test_an_id_that_would_be_truncated_raises_instead(fixed_uuid):
    too_long = "sl-stl-GOOGL-1111-fu9-" + "x" * 10
    with pytest.raises(ValueError, match="exceeds MAX_CID_LEN"):
        be._cid_with_level(too_long, "stop_limit", 171.25, 172.0, None)


def test_the_fixed_prefixes_written_inline_in_reconcile():
    """sl-moc- and sl-qb- are formatted inline rather than by a helper. The
    quote-breach cooldown finds its own history with LIKE 'sl-qb-%', so the
    spelling is load-bearing."""
    import inspect
    src = inspect.getsource(be)
    assert 'f"sl-moc-{sym}-{dep_id[:8]}"' in src
    assert 'f"sl-qb-{sym}-{dep_id[:8]}-{uuid.uuid4().hex[:8]}"' in src
    assert src.count('.like("sl-qb-%")') == 2
