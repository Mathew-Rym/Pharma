"""apply_movement is the only sanctioned way stock changes, so its tenant must be provable.

It wrote `settings.PHARMACY_ID` -- whichever pharmacy .env named -- so every pharmacy's
movements were filed against that one: the batch under tenant A, the ledger row under the
.env pharmacy. Invisible while a single pharmacy existed. It became a hard failure only when
the .env pharmacy was deleted:

    ForeignKeyViolation: Key (pharmacy_id)=(cee0072c-…) is not present in table "pharmacies"

-- receiving broken for every pharmacy, at the last step before the ledger.

The first fix swapped it for `pid()`. Better, but still wrong in kind: a low-level ledger
writer should not depend on ambient context it cannot verify. If a caller binds the wrong
tenant, or forgets, pid() cheerfully supplies a plausible answer.

The pharmacy is DERIVED FROM THE BATCH, which is the only source that cannot disagree with
the row being written -- and cross-tenant access is then a loud error rather than a silent
mis-file. These tests pin both halves.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


def test_the_ledger_no_longer_reads_the_env_pharmacy():
    """Structural. The defect was one identifier; this is the guard against it returning."""
    import ast
    import inspect

    import db as dbmod
    tree = ast.parse(inspect.getsource(dbmod.apply_movement).lstrip())
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]                      # the docstring explains the bug
    code = ast.unparse(fn)
    assert "settings.PHARMACY_ID" not in code, (
        "apply_movement must never take the pharmacy from .env")


@pytest.fixture
def two_pharmacies():
    """Two tenants, each with one product and one batch."""
    import secrets

    from db import ex, q1
    mark = secrets.token_hex(3)
    made = {}
    for side in ("a", "b"):
        p = q1("""insert into pharmacies (name, kind, status) values (%s,'tenant','active')
                  returning id""", (f"LEDGER-{side}-{mark}",))
        pid_ = str(p["id"])
        prod = q1("""insert into products (pharmacy_id, name, legacy_code, pack_size,
                            cost_price, sell_price)
                     values (%s,%s,%s,10,5,9) returning id""",
                  (pid_, f"DRUG {side} {mark}", f"LG{side}{mark}"))
        b = q1("""insert into batches (pharmacy_id, product_id, batch_no, qty_pieces)
                  values (%s,%s,%s,100) returning id""",
               (pid_, prod["id"], f"B-{side}-{mark}"))
        made[side] = {"pid": pid_, "batch": str(b["id"]), "product": str(prod["id"])}
    yield made
    for side in ("a", "b"):
        d = made[side]
        ex("delete from stock_movements where pharmacy_id=%s", (d["pid"],))
        ex("delete from batches where pharmacy_id=%s", (d["pid"],))
        ex("delete from products where pharmacy_id=%s", (d["pid"],))
        ex("delete from pharmacies where id=%s", (d["pid"],))


@db
def test_the_movement_is_filed_against_the_batchs_own_pharmacy(two_pharmacies):
    """THE test. Whatever the caller believes, the ledger row follows the batch."""
    import tenancy
    from db import q1, tx
    from db import apply_movement

    a = two_pharmacies["a"]
    with tenancy.pharmacy_scope(a["pid"]):
        with tx() as cur:
            apply_movement(cur, a["batch"], -10, "sale")
    row = q1("""select pharmacy_id, delta_pieces from stock_movements
                 where batch_id=%s""", (a["batch"],))
    assert str(row["pharmacy_id"]) == a["pid"]
    assert row["delta_pieces"] == -10


@db
def test_moving_another_tenants_batch_is_refused(two_pharmacies):
    """The failure that matters most. Bound to A, given B's batch: previously this would
    have written a row under whichever pharmacy .env named, and B's stock would have moved
    with no trace under B. It must raise."""
    import tenancy
    from db import apply_movement, q1, tx

    a, b = two_pharmacies["a"], two_pharmacies["b"]
    with tenancy.pharmacy_scope(a["pid"]):
        with pytest.raises(Exception) as e:
            with tx() as cur:
                apply_movement(cur, b["batch"], -5, "sale")
    assert "belong" in str(e.value).lower() or "tenant" in str(e.value).lower(), (
        f"the error must name the cross-tenant problem, got: {e.value}")

    # and nothing moved
    assert q1("select count(*) n from stock_movements where batch_id=%s",
              (b["batch"],))["n"] == 0
    assert q1("select qty_pieces from batches where id=%s", (b["batch"],))["qty_pieces"] == 100


@db
def test_a_missing_batch_is_refused_rather_than_written(two_pharmacies):
    """A movement against a batch that does not exist previously inserted a ledger row with
    a dangling batch_id, or failed on the FK with a message about pharmacies. Fail on the
    real cause."""
    import tenancy
    from db import apply_movement, tx

    with tenancy.pharmacy_scope(two_pharmacies["a"]["pid"]):
        with pytest.raises(Exception) as e:
            with tx() as cur:
                apply_movement(cur, "11111111-1111-1111-1111-111111111111", -1, "sale")
    assert "batch" in str(e.value).lower()


@db
def test_the_batch_quantity_and_the_ledger_move_together(two_pharmacies):
    """They are written in one statement pair inside the caller's transaction. If the ledger
    row is refused, the quantity must not have moved -- otherwise stock drifts from its own
    audit trail."""
    import tenancy
    from db import apply_movement, q1, tx

    a = two_pharmacies["a"]
    with tenancy.pharmacy_scope(a["pid"]):
        with tx() as cur:
            apply_movement(cur, a["batch"], -30, "sale")
    assert q1("select qty_pieces from batches where id=%s", (a["batch"],))["qty_pieces"] == 70
    assert q1("""select sum(delta_pieces) s from stock_movements where batch_id=%s""",
              (a["batch"],))["s"] == -30
