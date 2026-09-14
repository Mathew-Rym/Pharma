"""Final verification pass: regressions for defects found while RE-AUDITING the
previous hardening work (including its own claims).

The previous pass marked "PO dedup DONE" -- but jobs.low_stock_check minted draft
POs directly, unchecked, so the daily cron stacked one duplicate PO per day for
any product below its reorder level. And the fixes it did make were verified
singly, not under the concurrency this pass demands:

* apply_pos_sales read applied=false rows OUTSIDE its transaction, so two
  workers (agent retry + fresh batch, or dashboard manual ingest while the agent
  posts) both held the same row and both deducted the batch. The row is now
  claimed inside the transaction; the loser skips. One sale, one deduction, under
  ANY interleaving.
* on_payment_success replied via pid(), which is bound by the router and the SMS
  path but NOT by Safaricom's /mpesa/callback. The payment committed, then the
  customer receipt and staff dispatch notice died in the caller's except handler.
  The order's own pharmacy is now bound -- and wins even when another tenant is
  bound, which is what the mpesa.stk_push precedent established.
* pharmacist_approve had no already-verified guard, so a dashboard double-click
  set a PAID order back to 'awaiting_payment' -- regressing the order state
  machine on a second click of the same button.
* No test anywhere proved the wa_id insert-as-lock under concurrent duplicate
  delivery; it is correct by construction, and now it is correct by test.
"""
import os
import secrets
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def farm():
    """A throwaway pharmacy: product, batch, customer, supplier -- the pieces the
    flows under test need, nothing else."""
    import tenancy
    from db import ex, ex1

    mark = secrets.token_hex(3)
    ph = ex1("""insert into pharmacies (name, kind, status) values (%s,'tenant','active')
                returning id""", (f"VERIFY-{mark}",))
    pid = str(ph["id"])
    with tenancy.pharmacy_scope(pid):
        sup = ex1("""insert into suppliers (pharmacy_id, name, phone)
                     values (%s,'Verify Wholesaler','254700999999') returning id""",
                  (pid,))
        prod = ex1("""insert into products (pharmacy_id, name, legacy_code, pack_size,
                                            cost_price, sell_price, reorder_level_pieces,
                                            preferred_supplier_id)
                      values (%s,%s,%s,10,5,9,10,%s) returning id""",
                   (pid, f"DRUG {mark}", f"VF{mark}", sup["id"]))
        batch = ex1("""insert into batches (pharmacy_id, product_id, batch_no, qty_pieces)
                       values (%s,%s,%s,100) returning id""",
                    (pid, prod["id"], f"B-{mark}"))
        cust = ex1("""insert into customers (pharmacy_id, phone, consent_given)
                      values (%s,%s,true) returning id""", (pid, f"2547{mark[:8]}"))
        made = {"pid": pid, "mark": mark, "product": str(prod["id"]),
                "batch": str(batch["id"]), "customer": str(cust["id"]),
                "supplier": str(sup["id"])}
    yield made
    # order_lines / po_lines / loyalty_ledger / job_runs: no pharmacy_id column or
    # FK-ordered; reach them via their parents before the parent delete.
    ex("""delete from loyalty_ledger where customer_id in
           (select id from customers where pharmacy_id=%s)""", (pid,))
    ex("""delete from order_lines where order_id in
           (select id from orders where pharmacy_id=%s)""", (pid,))
    ex("""delete from po_lines where po_id in
           (select id from purchase_orders where pharmacy_id=%s)""", (pid,))
    for t in ("stock_movements", "orders", "payments", "pos_sales", "batches",
              "products", "prescriptions", "customers", "staff", "purchase_orders",
              "suppliers", "alerts", "agents", "stockout_log"):
        ex(f"delete from {t} where pharmacy_id=%s", (pid,))
    ex("delete from job_runs where pharmacy_id=%s", (pid,))
    ex("delete from wa_messages where pharmacy_id=%s", (pid,))
    ex("delete from wa_state where pharmacy_id=%s", (pid,))
    ex("delete from inbound_history where pharmacy_id=%s", (pid,))
    ex("delete from pharmacies where id=%s", (pid,))


# ------------------------------------------------- POS concurrent double-apply
@db
def test_concurrent_pos_apply_deducts_stock_exactly_once(farm):
    """Two workers run apply_pos_sales at the same time over the same landed row.
    Whichever way the threads interleave, the batch moves once and the row is
    applied once -- the claim inside the transaction is the arbiter."""
    import tenancy
    from db import ex1, q1
    from agent_api import apply_pos_sales

    with tenancy.pharmacy_scope(farm["pid"]):
        row = ex1("""insert into pos_sales (pharmacy_id, source, external_id, sold_at,
                                            legacy_code, description, qty_pieces,
                                            unit_price, line_total, raw)
                     values (%s,'phamacore',%s,now(),%s,'DRUG',10,5,50,'{}'::jsonb)
                     returning id""",
                  (farm["pid"], f"T-{farm['mark']}", f"VF{farm['mark']}"))

    results = []
    barrier = threading.Barrier(2)

    def _worker():
        tenancy.set_pharmacy(farm["pid"])
        barrier.wait()
        results.append(apply_pos_sales())

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    tenancy.clear_pharmacy()

    assert sum(results) == 1, f"exactly one worker may apply the sale, got {results}"
    assert q1("select qty_pieces from batches where id=%s",
              (farm["batch"],))["qty_pieces"] == 90, "double deduction happened"
    assert q1("select count(*) n from stock_movements where batch_id=%s "
              "and reason='pos_sale'", (farm["batch"],))["n"] == 1
    assert q1("select applied from pos_sales where id=%s", (row["id"],))["applied"]


@db
def test_replayed_pos_batch_lands_once_but_applies_once(farm):
    """The agent re-POSTs a batch it already sent (network retry): the
    on-conflict clause lands nothing new, and nothing double-applies."""
    import tenancy
    from db import q1
    from agent_api import apply_pos_sales

    with tenancy.pharmacy_scope(farm["pid"]):
        from db import ex
        for _ in range(2):
            ex("""insert into pos_sales (pharmacy_id, source, external_id, sold_at,
                                         legacy_code, description, qty_pieces,
                                         unit_price, line_total, raw)
                  values (%s,'phamacore',%s,now(),%s,'DRUG',10,5,50,'{}'::jsonb)
                  on conflict (pharmacy_id, source, external_id) do nothing""",
               (farm["pid"], f"R-{farm['mark']}", f"VF{farm['mark']}"))
        apply_pos_sales()
        apply_pos_sales()

    assert q1("select count(*) n from pos_sales where pharmacy_id=%s and "
              "external_id=%s", (farm["pid"], f"R-{farm['mark']}"))["n"] == 1
    assert q1("select qty_pieces from batches where id=%s",
              (farm["batch"],))["qty_pieces"] == 90


# ------------------------------------------------- payment callback tenant path
@db
def test_payment_success_binds_the_orders_own_tenant(farm):
    """The Safaricom callback path runs with NO tenant bound. Before the fix the
    payment committed and then every reply raised NoTenant, swallowed by the
    caller -- customer never confirmed, staff never told to pack."""
    import tenancy
    from db import ex1, q1
    from rx import on_payment_success

    with tenancy.pharmacy_scope(farm["pid"]):
        order = ex1("""insert into orders (pharmacy_id, customer_id, status, total)
                       values (%s,%s,'awaiting_payment',50) returning id""",
                    (farm["pid"], farm["customer"]))
        ex1("""insert into order_lines (order_id, product_id, batch_id, qty_pieces,
                                        unit_price, line_total)
               values (%s,%s,%s,10,5,50) returning id""",
            (order["id"], farm["product"], farm["batch"]))

    tenancy.clear_pharmacy()                 # exactly what /mpesa/callback sees
    on_payment_success(str(order["id"]), "RCPT123")

    o = q1("select status from orders where id=%s", (order["id"],))
    assert o["status"] == "paid"
    assert q1("select qty_pieces from batches where id=%s",
              (farm["batch"],))["qty_pieces"] == 90


@db
def test_payment_success_wins_over_a_wrongly_bound_tenant(farm):
    """payments_sms calls this inside the SENDER's scope; a staff member of
    pharmacy B confirming a payment for pharmacy A's order must not file the
    dispensing under B. The order's own tenant wins, like stk_push."""
    import tenancy
    from db import ex1, q1
    from rx import on_payment_success

    other = ex1("""insert into pharmacies (name, kind, status) values (%s,'tenant','active')
                   returning id""", (f"OTHER-{farm['mark']}",))

    with tenancy.pharmacy_scope(farm["pid"]):
        order = ex1("""insert into orders (pharmacy_id, customer_id, status, total)
                       values (%s,%s,'awaiting_payment',50) returning id""",
                    (farm["pid"], farm["customer"]))
        ex1("""insert into order_lines (order_id, product_id, batch_id, qty_pieces,
                                        unit_price, line_total)
               values (%s,%s,%s,10,5,50) returning id""",
            (order["id"], farm["product"], farm["batch"]))

    with tenancy.pharmacy_scope(str(other["id"])):      # B bound, A's order
        on_payment_success(str(order["id"]), "RCPT456")

    o = q1("select status from orders where id=%s", (order["id"],))
    assert o["status"] == "paid"
    # stock moved under A, not refused by a cross-tenant guard nor filed under B
    assert q1("select qty_pieces from batches where id=%s",
              (farm["batch"],))["qty_pieces"] == 90
    assert q1("select count(*) n from stock_movements where pharmacy_id=%s "
              "and reason='sale'", (farm["pid"],))["n"] == 1
    from db import ex
    ex("delete from pharmacies where id=%s", (other["id"],))


# ------------------------------------------------- pharmacist approval idempotency
@db
def test_dashboard_double_approval_does_not_regress_a_paid_order(farm):
    """pharmacist_approve had no already-verified guard: clicking Approve twice
    set a paid order back to awaiting_payment."""
    import tenancy
    from db import ex, ex1, q1
    from rx import pharmacist_approve

    with tenancy.pharmacy_scope(farm["pid"]):
        ph = ex1("""insert into staff (pharmacy_id, phone, name, role, ppb_reg_no)
                    values (%s,%s,'Verified Pharm','pharmacist','PPB/1') returning id""",
                 (farm["pid"], f"25478{farm['mark'][:7]}"))
        rx = ex1("""insert into prescriptions (pharmacy_id, customer_id, image_path,
                                               status)
                   values (%s,%s,'x','pending_verification') returning id""",
                 (farm["pid"], farm["customer"]))
        order = ex1("""insert into orders (pharmacy_id, customer_id, prescription_id,
                                           status, total)
                      values (%s,%s,%s,'awaiting_pharmacist',50) returning id""",
                    (farm["pid"], farm["customer"], rx["id"]))

        pharmacist_approve(str(rx["id"]), str(ph["id"]))
        assert q1("select status from orders where id=%s",
                  (order["id"],))["status"] == "awaiting_payment"

        ex("update orders set status='paid' where id=%s", (order["id"],))
        pharmacist_approve(str(rx["id"]), str(ph["id"]))       # the double-click

        assert q1("select status from orders where id=%s",
                  (order["id"],))["status"] == "paid", (
            "a second approval must never un-pay a paid order")


# ------------------------------------------------- low-stock job PO dedup
@db
def test_low_stock_job_does_not_stack_a_po_per_day(farm):
    """The daily cron created one fresh draft PO per run for any product below
    reorder -- five days low, five POs to hand-kill. Now: one, until it is sent."""
    import tenancy
    from db import ex, ex1, q1
    from jobs import low_stock_check

    with tenancy.pharmacy_scope(farm["pid"]):
        # velocity so the reorder query picks the product up: 900 sold in 90 days
        ex("""insert into stock_movements (pharmacy_id, batch_id, delta_pieces, reason)
              values (%s,%s,-900,'sale')""", (farm["pid"], farm["batch"]))
        ex("update batches set qty_pieces=5 where id=%s", (farm["batch"],))

        first = low_stock_check()
        second = low_stock_check()

        n = q1("""select count(*) n from purchase_orders
                   where pharmacy_id=%s""", (farm["pid"],))["n"]
        assert n == 1, f"daily cron stacked duplicate POs ({n})"
        assert q1("""select count(*) n from po_lines l
                      join purchase_orders po on po.id=l.po_id
                     where po.pharmacy_id=%s""", (farm["pid"],))["n"] == 1
        # the alert copy still fires every run -- only PO creation dedups
        assert first["items"] >= 1 and second["items"] >= 1


# ------------------------------------------------- concurrent duplicate webhook
@db
def test_concurrent_duplicate_webhook_runs_the_workflow_once(farm):
    """GOWA redelivers on reconnect; two redeliveries can arrive together. The
    wa_id insert-as-lock must make exactly one claim regardless of interleaving:
    one inbound row, one reply composed."""
    import tenancy
    from db import ex, ex1, q1
    from router import handle_inbound

    with tenancy.pharmacy_scope(farm["pid"]):
        ex1("""insert into staff (pharmacy_id, phone, name, role)
               values (%s,%s,'Owner','owner') returning id""",
            (farm["pid"], f"25477{farm['mark'][:7]}"))

    wa_id = f"dup-{farm['mark']}"
    msg = {"wa_id": wa_id, "from": f"25477{farm['mark'][:7]}", "type": "text",
           "text": "EXPIRY", "pharmacy_id": farm["pid"]}

    barrier = threading.Barrier(2)

    def _worker():
        barrier.wait()
        handle_inbound(dict(msg))

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    inbound = q1("select count(*) n from wa_messages where wa_id=%s", (wa_id,))["n"]
    assert inbound == 1, f"duplicate delivery created {inbound} inbound rows"
