"""v2 tests: the bridge agent, POS unit resolution, SMS payments, forecasting.

All pure-function — no DB, no network. These cover the three things v2 added that
can silently corrupt stock or money:

  1. UNIT RESOLUTION. phAMACore writes quantities as '2W0P' (2 whole packs). Reading
     that as 2 pieces understates a 30s pack by 30x. This is the single easiest way
     for the ledger to start lying, and it fails silently.
  2. EXPORT SHAPE DETECTION. The agent absorbs whatever phAMACore exports. Misrouting
     a monthly-totals file into the sales stream would invent transactions.
  3. FORWARDED M-PESA SMS. A parser that accepts 'I paid already please check' as a
     payment confirmation hands over goods for free.

Run:  pytest tests/ -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))


# ============================================================ agent: quantities
@pytest.mark.parametrize("raw,expected", [
    ("5W0P", (5, 0, True)),       # 5 whole packs
    ("2W5P", (2, 5, True)),       # 2 packs and 5 loose
    ("3w0p", (3, 0, True)),       # phAMACore is not consistent about case
    ("1W", (1, 0, True)),
    ("-3W", (-3, 0, True)),       # a return / reversal
    ("120", (0, 120, False)),     # plain number = loose pieces
    ("1,200", (0, 1200, False)),
])
def test_parse_qty_keeps_packs_and_pieces_apart(raw, expected):
    """The agent cannot know pack_size, so it must NOT collapse packs into pieces.

    It reports packs and loose separately and lets the cloud multiply. If this ever
    returns a bare int again, every pack-denominated sale silently understates by
    the pack size and the stock number becomes fiction.
    """
    from agent import _parse_qty
    assert _parse_qty(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "abc", "smudged"])
def test_parse_qty_unreadable_is_zero_not_a_guess(raw):
    from agent import _parse_qty
    assert _parse_qty(raw) == (0, 0, False)


def test_pack_notation_is_never_reported_as_pieces():
    """Regression guard for the 30x understatement."""
    from agent import _parse_qty
    packs, loose, is_packs = _parse_qty("2W0P")
    assert is_packs is True
    assert (packs, loose) == (2, 0)


# ============================================================ cloud: unit resolution
def test_resolve_pieces_multiplies_packs_by_pack_size():
    from agent_api import _resolve_pieces
    raw = {"qty_is_packs": True, "qty_packs": 2, "qty_loose": 0}
    assert _resolve_pieces(raw, 0, 30) == 60


def test_resolve_pieces_adds_loose_pieces():
    from agent_api import _resolve_pieces
    raw = {"qty_is_packs": True, "qty_packs": 2, "qty_loose": 5}
    assert _resolve_pieces(raw, 0, 30) == 65


def test_resolve_pieces_falls_back_for_older_agents():
    """An older agent, or a hand-built payload, sends only qty_pieces."""
    from agent_api import _resolve_pieces
    assert _resolve_pieces({"qty_pieces": 120}, 120, 30) == 120
    assert _resolve_pieces(None, 120, 30) == 120


def test_resolve_pieces_missing_pack_size_does_not_zero_the_sale():
    """pack_size null/0 must degrade to 1, not multiply the quantity to nothing."""
    from agent_api import _resolve_pieces
    assert _resolve_pieces({"qty_is_packs": True, "qty_packs": 4, "qty_loose": 0},
                           0, None) == 4
    assert _resolve_pieces({"qty_is_packs": True, "qty_packs": 4, "qty_loose": 0},
                           0, 0) == 4


def test_pack_rows_are_not_excluded_by_the_apply_filter():
    """REGRESSION. A '2W0P' row lands with qty_pieces = 0 and its real quantity in
    raw.qty_packs. apply_pos_sales() once filtered `qty_pieces > 0`, which dropped
    every pack-denominated sale: never applied, no apply_error, stock never
    decremented — the exact drift the agent exists to prevent, failing silently.

    The filter must consult raw.qty_is_packs too.
    """
    import inspect

    import agent_api
    src = inspect.getsource(agent_api.apply_pos_sales)
    where = src[src.index("from pos_sales"):src.index("order by sold_at")]
    assert "qty_is_packs" in where, (
        "apply_pos_sales no longer admits pack-denominated rows; every 'NWNP' POS "
        "sale will be silently skipped and stock will drift")


# ============================================================ agent: export shapes
def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_transaction_level_export_is_read_as_sales(tmp_path):
    from agent import classify_and_parse
    p = _write(tmp_path, "sales.csv",
               "Sale Date,SaleID,ItemCode,ItemName,Qty,UnitPrice,Total,Payment\n"
               "26/07/2026 10:14,INV001,PRN255,PRENOR 25/5MG TABS 30S,2W0P,450,900,MPESA\n")
    kind, rows = classify_and_parse(p)
    assert kind == "sale"
    assert len(rows) == 1
    r = rows[0]
    assert r["external_id"] == "INV001"
    assert r["legacy_code"] == "PRN255"
    assert r["sold_at"].startswith("2026-07-26")
    assert (r["qty_packs"], r["qty_is_packs"]) == (2, True)


def test_monthly_totals_export_is_not_mistaken_for_sales(tmp_path):
    """Routing a monthly-totals file into the sales stream would invent transactions
    and double-count demand against the live ledger."""
    from agent import classify_and_parse
    p = _write(tmp_path, "monthly.csv",
               "StockPeriod,ItemCode,ItemName,Qty,Value\n"
               "2026-06,PRN255,PRENOR 25/5MG TABS 30S,290,130500\n")
    kind, rows = classify_and_parse(p)
    assert kind == "history_monthly"
    assert rows[0]["period"] == "2026-06-01"      # normalised to first of month
    assert rows[0]["qty_pieces"] == 290


def test_stock_snapshot_export_is_reconciliation_only(tmp_path):
    from agent import classify_and_parse
    p = _write(tmp_path, "snapshot.csv",
               "ItemCode,Description,Qty\nPRN255,PRENOR 25/5MG TABS 30S,3W0P\n")
    kind, rows = classify_and_parse(p)
    assert kind == "snapshot"
    assert rows[0]["qty_packs"] == 3


def test_unrecognised_export_is_rejected_not_guessed(tmp_path):
    """Unknown files must land in \\rejected\\ rather than being partly imported."""
    from agent import classify_and_parse
    p = _write(tmp_path, "junk.csv", "Foo,Bar\n1,2\n")
    kind, rows = classify_and_parse(p)
    assert kind == "unknown"
    assert rows == []


def test_semicolon_delimited_export_still_parses(tmp_path):
    """Windows regional settings routinely produce ';' CSVs."""
    from agent import classify_and_parse
    p = _write(tmp_path, "semi.csv",
               "Sale Date;SaleID;ItemCode;ItemName;Qty;Total\n"
               "26/07/2026;INV002;PRN255;PRENOR;1W0P;450\n")
    kind, rows = classify_and_parse(p)
    assert kind == "sale"
    assert rows[0]["external_id"] == "INV002"


def test_rows_without_an_id_get_a_stable_synthetic_one(tmp_path):
    """Re-exporting the same file must not double-count. The synthetic id is a hash
    of the row, so the server's unique(external_id) catches the replay."""
    from agent import classify_and_parse
    text = ("Sale Date,ItemCode,ItemName,Qty,Total\n"
            "26/07/2026,PRN255,PRENOR,1W0P,450\n")
    first = classify_and_parse(_write(tmp_path, "a.csv", text))[1]
    second = classify_and_parse(_write(tmp_path, "b.csv", text))[1]
    assert first[0]["external_id"] == second[0]["external_id"]
    assert first[0]["external_id"]


@pytest.mark.parametrize("raw,starts", [
    ("2026-06", "2026-06-01"),
    ("26/07/2026", "2026-07-26"),
    ("Jul-2026", "2026-07-01"),
])
def test_date_formats_seen_in_phamacore_exports(raw, starts):
    from agent import _parse_dt
    assert _parse_dt(raw).startswith(starts)


def test_unparseable_date_is_none_rather_than_today():
    """Defaulting to today would silently backdate history onto the wrong month."""
    from agent import _parse_dt
    assert _parse_dt("nonsense") is None
    assert _parse_dt("") is None


# ============================================================ M-Pesa SMS
# Three real Safaricom wordings. The format has drifted over the years, so the
# parser matches loosely on what has stayed stable: receipt code, amount, keyword.
PAYBILL_SMS = ("QGH7XYZ12K Confirmed. Ksh1,500.00 sent to DISHII PHARMACY for account "
               "4A7B2C91 on 26/7/26 at 10:14 AM. New M-PESA balance is Ksh3,240.00.")
RECEIVED_SMS = ("SFJ4K2L9MN Confirmed. You have received Ksh450.00 from JOHN DOE "
                "254712345678 on 26/7/26 at 9:02 AM.")
PAID_TO_SMS = ("TDK9P1QW3E confirmed. Ksh2,300.00 paid to PHARMACY PAYBILL 4166919. "
               "on 26/7/26 at 4:45 PM. New M-PESA balance is Ksh1,120.00. "
               "Transaction cost, Ksh0.00.")


@pytest.mark.parametrize("sms", [PAYBILL_SMS, RECEIVED_SMS, PAID_TO_SMS])
def test_real_safaricom_wordings_are_recognised(sms):
    from payments_sms import looks_like_mpesa_sms
    assert looks_like_mpesa_sms(sms)


@pytest.mark.parametrize("text", [
    "hi",
    "do you have amoxil",
    "I paid already please check",
    "",
    None,
    "Please send me the price list for next month thank you very much",
])
def test_ordinary_chat_is_not_treated_as_payment(text):
    """A false positive here confirms an order that was never paid for."""
    from payments_sms import looks_like_mpesa_sms
    assert not looks_like_mpesa_sms(text)


def test_parses_receipt_amount_and_account_reference():
    from payments_sms import parse_mpesa_sms
    p = parse_mpesa_sms(PAYBILL_SMS)
    assert p["receipt"] == "QGH7XYZ12K"
    assert p["amount"] == 1500.00
    assert p["account"] == "4A7B2C91"


def test_amount_commas_and_decimals_survive():
    from payments_sms import parse_mpesa_sms
    assert parse_mpesa_sms(PAID_TO_SMS)["amount"] == 2300.00
    assert parse_mpesa_sms(RECEIVED_SMS)["amount"] == 450.00


def test_receipt_code_is_alphanumeric_not_a_bare_number():
    """A pure number in the message is an amount, a date or a phone — never a
    receipt. Picking one up would key the replay guard off the wrong value."""
    from payments_sms import parse_mpesa_sms
    for sms in (PAYBILL_SMS, RECEIVED_SMS, PAID_TO_SMS):
        receipt = parse_mpesa_sms(sms)["receipt"]
        assert receipt.isalnum() and not receipt.isdigit()
        assert len(receipt) == 10


def test_no_account_reference_is_none_not_a_wrong_match():
    """Without an account ref we fall back to the caller's open order. Inventing one
    would credit a stranger's payment to someone else's order."""
    from payments_sms import parse_mpesa_sms
    assert parse_mpesa_sms(RECEIVED_SMS)["account"] is None


# ============================================================ forecast
def _row(avg_daily, season, on_hand, pack_size):
    return {"avg_daily": avg_daily, "season_index": season,
            "on_hand": on_hand, "pack_size": pack_size}


def test_suggest_qty_rounds_up_to_whole_packs():
    """Suppliers sell packs. Asking for 17 pieces of a 30s pack makes the pharmacy
    look like it does not know its trade."""
    from forecast import suggest_qty
    # 8/day * 30 days = 240 needed, 0 on hand, 30s pack -> 8 packs
    assert suggest_qty(_row(8, 1.0, 0, 30)) == 240
    # 0.5/day * 30 = 15 needed -> rounds up to one 30s pack
    assert suggest_qty(_row(0.5, 1.0, 0, 30)) == 30


def test_suggest_qty_subtracts_what_is_already_on_the_shelf():
    from forecast import suggest_qty
    assert suggest_qty(_row(8, 1.0, 200, 30)) == 60      # need 40 -> 2 packs


def test_suggest_qty_never_orders_when_overstocked():
    from forecast import suggest_qty
    assert suggest_qty(_row(1, 1.0, 5000, 30)) == 0


def test_suggest_qty_applies_the_seasonal_index():
    """The pharmacist's actual complaint: some drugs spike in season. A trailing
    average cannot see that; the month-of-year index can."""
    from forecast import suggest_qty
    assert suggest_qty(_row(8, 1.5, 0, 30)) == 360       # 240 * 1.5
    assert suggest_qty(_row(8, 0.5, 0, 30)) == 120


def test_suggest_qty_pack_size_zero_does_not_divide_by_zero():
    from forecast import suggest_qty
    assert suggest_qty(_row(8, 1.0, 0, 0)) == 240
    assert suggest_qty(_row(8, 1.0, 0, None)) == 240
