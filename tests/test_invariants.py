"""Tests. Two kinds:

1. Pure-function tests (no DB) — always run these, they're instant.
2. The ledger invariant — run against your live pilot DB. If this ever fails your
   product is lying to a pharmacist about their inventory, which is the one failure
   mode that ends a pilot on the spot.

Run:  pytest tests/ -v
      pytest tests/ -v -m "not db"     # skip DB tests
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


# ============================================================ phone
def test_phone_formats_all_resolve_to_one_person():
    from utils import norm_phone
    expected = "254713755274"
    for variant in [
        "0713755274",
        "+254713755274",
        "254713755274",
        "+254 713 755 274",
        "254713755274@s.whatsapp.net",
        "0713 755 274",
        "713755274",
    ]:
        assert norm_phone(variant) == expected, variant


def test_phone_empty_is_empty():
    from utils import norm_phone
    assert norm_phone("") == ""
    assert norm_phone(None) == ""


# ============================================================ units
def test_wp_roundtrip():
    from utils import from_pieces, to_pieces
    assert to_pieces(5, 0, 30) == 150
    assert to_pieces(1, 0, 1) == 1
    assert to_pieces(2, 5, 30) == 65
    assert from_pieces(150, 30) == "5W0P"
    assert from_pieces(65, 30) == "2W5P"
    assert from_pieces(0, 30) == "0W0P"


def test_pack_size_zero_does_not_divide_by_zero():
    from utils import from_pieces, to_pieces
    assert to_pieces(3, 0, 0) == 3
    assert from_pieces(10, 0) == "10W0P"


def test_parse_wp_from_staff_typing():
    from utils import parse_wp
    assert parse_wp("2W") == (2, 0)
    assert parse_wp("2W5P") == (2, 5)
    assert parse_wp("3w0p") == (3, 0)
    assert parse_wp(" 4W ") == (4, 0)
    assert parse_wp("7") == (7, 0)
    assert parse_wp("nonsense") is None


# ============================================================ expiry parsing
@pytest.mark.parametrize("raw,expected", [
    ("01/2028", date(2028, 1, 31)),
    ("03/2030", date(2030, 3, 31)),
    ("2027-08", date(2027, 8, 31)),
    ("2027-08-15", date(2027, 8, 15)),
    ("Jul-28", date(2028, 7, 31)),
    ("jul/2028", date(2028, 7, 31)),
    ("07/28", date(2028, 7, 31)),
    ("02/2028", date(2028, 2, 29)),      # leap year, end of month
    ("12/2027", date(2027, 12, 31)),
])
def test_expiry_formats_from_real_invoices(raw, expected):
    from utils import parse_expiry
    assert parse_expiry(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "smudged", "13/2028", "99/99", "1899-01"])
def test_expiry_returns_none_rather_than_guessing(raw):
    from utils import parse_expiry
    assert parse_expiry(raw) is None


def test_expiry_is_end_of_month_not_start():
    """A batch marked 01/2028 is good through 31 Jan. Using the 1st would write off
    a month of saleable stock on every batch in the system."""
    from utils import parse_expiry
    assert parse_expiry("01/2028").day == 31


# ============================================================ pack size guess
def test_guess_pack_size_from_product_names():
    from grn import _guess_pack_size
    assert _guess_pack_size("PRENOR 25/5MG TABS 30S") == 30
    assert _guess_pack_size("Cavinton 5mg Tabs 50s") == 50
    assert _guess_pack_size("Almax Forte Sachets 30s") == 30
    assert _guess_pack_size("MIXTARD 30 100iu/ml 10ML VIAL") == 1


# ============================================================ DB invariants
DB_AVAILABLE = bool(os.getenv("DATABASE_URL")) and bool(os.getenv("PHARMACY_ID"))
db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL/PHARMACY_ID not set")


@db
def test_ledger_matches_batch_quantities():
    """THE test. Every batch's qty_pieces must equal the sum of its movements.

    If this fails, stop shipping features and fix it. Someone changed a quantity
    without writing a ledger row.
    """
    from db import q
    from config import settings
    drift = q(
        """select b.id, p.name, b.qty_pieces, coalesce(sum(m.delta_pieces),0) as ledger
             from batches b
             join products p on p.id = b.product_id
             left join stock_movements m on m.batch_id = b.id
            where b.pharmacy_id = %s
            group by b.id, p.name, b.qty_pieces
           having b.qty_pieces <> coalesce(sum(m.delta_pieces),0)""",
        (settings.PHARMACY_ID,),
    )
    assert not drift, f"{len(drift)} batches disagree with the ledger: {drift[:5]}"


@db
def test_no_negative_stock():
    from db import q
    from config import settings
    neg = q("select id, qty_pieces from batches where pharmacy_id=%s and qty_pieces < 0",
            (settings.PHARMACY_ID,))
    assert not neg, f"negative stock on {len(neg)} batches: {neg[:5]}"


@db
def test_approved_grns_always_have_a_named_approver():
    """Regulatory: no stock enters without a human on the record."""
    from db import q
    from config import settings
    orphan = q("""select id, invoice_no from grns
                   where pharmacy_id=%s and status='approved' and approved_by is null""",
               (settings.PHARMACY_ID,))
    assert not orphan, f"{len(orphan)} approved GRNs with no approver: {orphan[:5]}"


@db
def test_no_order_left_the_pharmacist_gate_unverified():
    """PPB: an order may not reach payment unless a pharmacist verified the script."""
    from db import q
    from config import settings
    bad = q(
        """select o.id, o.status from orders o
             join prescriptions p on p.id = o.prescription_id
            where o.pharmacy_id = %s
              and o.status in ('awaiting_payment','paid','packed','dispatched','delivered')
              and (p.status <> 'verified' or p.verified_by is null)""",
        (settings.PHARMACY_ID,),
    )
    assert not bad, f"{len(bad)} orders bypassed pharmacist verification: {bad[:5]}"


@db
def test_no_duplicate_mpesa_receipts():
    from db import q
    dup = q("""select mpesa_receipt, count(*) from payments
                where mpesa_receipt is not null
                group by mpesa_receipt having count(*) > 1""")
    assert not dup, f"duplicate M-Pesa receipts (double-credited orders): {dup}"


@db
def test_no_expired_batch_allocated_to_an_open_order():
    from db import q
    from config import settings
    bad = q(
        """select l.order_id, b.batch_no, b.expiry_date
             from order_lines l
             join batches b on b.id = l.batch_id
             join orders o on o.id = l.order_id
            where o.pharmacy_id=%s
              and o.status in ('awaiting_payment','paid','packed')
              and b.expiry_date < current_date""",
        (settings.PHARMACY_ID,),
    )
    assert not bad, f"expired stock allocated to live orders: {bad[:5]}"
