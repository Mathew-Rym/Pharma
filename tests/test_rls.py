"""Tenant isolation enforced by the database (Phase 2).

These tests exist because the previous state was WORSE than no RLS: it was enabled on
most tables with zero policies and described as fail-closed, while the application
connected as `postgres`, which has rolbypassrls. It looked locked in the Supabase UI
and enforced nothing.

So the assertions here are deliberately about behaviour, not configuration. "RLS is
enabled" was true the whole time and meant nothing. What matters is: can pharmacy A
read pharmacy B's row, yes or no.
"""
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL")) and bool(os.getenv("PHARMACY_ID"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL/PHARMACY_ID not set")


@pytest.fixture
def two_pharmacies():
    """Two tenants, each with a product only they should see."""
    from db import ex, q1
    mark = secrets.token_hex(3).upper()
    made = {}
    for side in ("A", "B"):
        p = q1("insert into pharmacies (name) values (%s) returning id",
               (f"RLS-{side}-{mark}",))
        prod = q1("""insert into products (pharmacy_id, name, legacy_code, pack_size,
                            cost_price, sell_price)
                     values (%s,%s,%s,10,5,9) returning id""",
                  (p["id"], f"DRUG {side} {mark}", f"RLS{side}{mark}"))
        made[side] = {"pid": str(p["id"]), "product": str(prod["id"])}
    yield made
    for side in ("A", "B"):
        ex("delete from products where pharmacy_id=%s", (made[side]["pid"],))
        ex("delete from staff where pharmacy_id=%s", (made[side]["pid"],))
        ex("delete from pharmacies where id=%s", (made[side]["pid"],))


# ============================================================ the actual question
@db
def test_a_tenant_cannot_read_another_tenants_rows(two_pharmacies):
    """THE test. Everything else in this file is detail."""
    from db import pool
    a, b = two_pharmacies["A"], two_pharmacies["B"]

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("set local role pharmaos_app")
        cur.execute("select set_config('app.current_pharmacy', %s, true)", (a["pid"],))

        cur.execute("select count(*) as n from products where pharmacy_id = %s",
                    (b["pid"],))
        assert cur.fetchone()["n"] == 0, "pharmacy A read pharmacy B's products"

        # ...and can still see its own
        cur.execute("select count(*) as n from products")
        assert cur.fetchone()["n"] >= 1, "pharmacy A cannot see its own products"
        conn.rollback()


@db
def test_a_tenant_cannot_write_into_another_tenant(two_pharmacies):
    """Reading is half of it. WITH CHECK is what stops A creating rows owned by B."""
    import psycopg

    from db import pool
    a, b = two_pharmacies["A"], two_pharmacies["B"]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("set local role pharmaos_app")
            cur.execute("select set_config('app.current_pharmacy', %s, true)",
                        (a["pid"],))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("""insert into products (pharmacy_id, name, legacy_code,
                                    pack_size, cost_price, sell_price)
                               values (%s,'SMUGGLED','X',1,1,1)""", (b["pid"],))
        conn.rollback()


@db
def test_a_tenant_cannot_update_another_tenants_rows(two_pharmacies):
    from db import pool
    a, b = two_pharmacies["A"], two_pharmacies["B"]
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("set local role pharmaos_app")
        cur.execute("select set_config('app.current_pharmacy', %s, true)", (a["pid"],))
        cur.execute("update products set name='HACKED' where id=%s", (b["product"],))
        assert cur.rowcount == 0, "pharmacy A updated pharmacy B's product"
        conn.rollback()


@db
def test_unset_tenant_shows_nothing_rather_than_everything(two_pharmacies):
    """The failure direction that matters. A bug that forgets to set the tenant must
    show no rows, never somebody else's rows."""
    from db import pool
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("set local role pharmaos_app")
        # deliberately no set_config
        cur.execute("select count(*) as n from products")
        assert cur.fetchone()["n"] == 0
        conn.rollback()


@db
def test_child_rows_are_scoped_through_their_parent(two_pharmacies):
    """grn_lines, order_lines and po_lines have no pharmacy_id of their own. If the
    parent join were wrong they would be world-readable while the parent looked safe."""
    from db import ex, pool, q1
    a, b = two_pharmacies["A"], two_pharmacies["B"]
    mark = secrets.token_hex(3)
    g = q1("""insert into grns (pharmacy_id, invoice_no, status, images)
              values (%s,%s,'needs_review','[]') returning id""", (b["pid"], mark))
    ex("""insert into grn_lines (grn_id, line_no, raw_description,
                qty_invoiced_pieces) values (%s,1,'SECRET LINE',10)""", (g["id"],))
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("set local role pharmaos_app")
            cur.execute("select set_config('app.current_pharmacy', %s, true)",
                        (a["pid"],))
            cur.execute("select count(*) as n from grn_lines where grn_id=%s",
                        (g["id"],))
            assert cur.fetchone()["n"] == 0, "child rows leaked across tenants"
            conn.rollback()
    finally:
        ex("delete from grn_lines where grn_id=%s", (g["id"],))
        ex("delete from grns where id=%s", (g["id"],))


@db
def test_a_pharmacy_cannot_see_other_pharmacies(two_pharmacies):
    from db import pool
    a, b = two_pharmacies["A"], two_pharmacies["B"]
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("set local role pharmaos_app")
        cur.execute("select set_config('app.current_pharmacy', %s, true)", (a["pid"],))
        cur.execute("select count(*) as n from pharmacies where id=%s", (b["pid"],))
        assert cur.fetchone()["n"] == 0
        conn.rollback()


# ============================================================ the trap that was set
@db
def test_the_app_role_cannot_bypass_rls():
    """The whole reason the previous setup enforced nothing. If anyone ever grants
    BYPASSRLS to pharmaos_app, every policy above silently stops applying and no other
    test would notice."""
    from db import q1
    row = q1("select rolbypassrls from pg_roles where rolname='pharmaos_app'")
    assert row is not None, "pharmaos_app role missing — run ./run.sh migrate"
    assert row["rolbypassrls"] is False, (
        "pharmaos_app can bypass RLS; every tenant_isolation policy is a no-op")


@db
def test_policies_exist_on_every_tenant_table():
    """Asserted against the database's own account of itself, not a hand-kept list,
    so adding a table without a policy fails here rather than in production."""
    from db import q
    gaps = q("select table_name from v_rls_coverage where policies = 0")
    assert not gaps, f"tables with no RLS policy: {[g['table_name'] for g in gaps]}"


def test_no_insert_omits_pharmacy_id_on_a_tenant_table():
    """The bug class RLS turns from silent into fatal.

    job_runs._run() inserted without pharmacy_id for months. Nothing complained --
    the dashboard just papered over it with `or pharmacy_id is null`. Under RLS that
    same insert fails the WITH CHECK outright, so every cron job would have started
    erroring the moment isolation was switched on. Catch it here instead.
    """
    import pathlib
    import re

    DIRECT = {"agents", "alerts", "batches", "customers", "demand_forecast",
              "duty_roster", "grns", "job_runs", "login_events", "orders", "payments",
              "pos_sales", "prescriptions", "products", "purchase_orders",
              "sales_history_monthly", "staff", "stock_movements",
              "stock_reconciliation", "suppliers", "wa_messages", "wa_state"}

    root = pathlib.Path(__file__).resolve().parent.parent
    pat = re.compile(r"insert\s+into\s+(\w+)\s*\(([^)]*)\)", re.I | re.S)
    offenders = []
    for f in list((root / "api").glob("*.py")) + list((root / "dashboard").glob("*.py")):
        src = f.read_text()
        for m in pat.finditer(src):
            table, cols = m.group(1).lower(), m.group(2).lower()
            if table in DIRECT and "pharmacy_id" not in cols:
                offenders.append(f"{f.name}:{src[:m.start()].count(chr(10)) + 1} "
                                 f"-> {table}")
    assert not offenders, (
        "these inserts omit pharmacy_id and will fail once DB_ENFORCE_RLS is on: "
        + ", ".join(offenders))


@db
def test_scope_does_not_leak_to_the_next_transaction():
    """SET LOCAL is the reason this is safe on a pooled connection. If it were SET
    (not LOCAL) the tenant would persist onto whoever borrowed the connection next."""
    from db import pool
    from config import settings
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("set local role pharmaos_app")
        cur.execute("select set_config('app.current_pharmacy', %s, true)",
                    (settings.PHARMACY_ID,))
        conn.rollback()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("select current_user as u, "
                    "coalesce(current_setting('app.current_pharmacy', true),'') as p")
        row = cur.fetchone()
        assert row["u"] != "pharmaos_app", "role leaked to the next transaction"
        assert row["p"] == "", "tenant setting leaked to the next transaction"


# ============================================================ fail-safe
def test_enforcement_is_off_by_default():
    """Creating the capability must not switch it on. Everything outside
    tenant_scope() still filters by hand and works today."""
    import importlib

    import config
    old = os.environ.pop("DB_ENFORCE_RLS", None)
    try:
        importlib.reload(config)
        assert config.get_settings.__wrapped__().DB_ENFORCE_RLS is False
    finally:
        if old is not None:
            os.environ["DB_ENFORCE_RLS"] = old
        importlib.reload(config)
