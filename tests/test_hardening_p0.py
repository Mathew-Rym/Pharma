"""P0 hardening regressions: negative stock, agent command ownership, suspended
agents, fail-closed state, prescription capability, and PO dedup.

Each test here pins a defect found in the production-hardening audit that the
existing suite did not cover:

* apply_movement could take a batch below zero (quote-then-pay gap, concurrent POS
  and dispensing on the last units). Inventory must never go negative.
* /agent/commands/{id}/result finalised ANY command by id -- an agent token could
  mark another agent's command done and trigger its WhatsApp reply. Ownership and
  the queued -> taken -> done transition are now enforced.
* A suspended agent was still served commands and ingestion. Only heartbeat may
  answer, so the agent can learn it is suspended and back off.
* set_state fell back to settings.PHARMACY_ID "as a last resort" -- the last
  runtime place the .env pharmacy could own another tenant's conversation state.
* Manager/owner roles verified prescriptions on seniority alone. Verification is a
  capability: pharmacist role, or owner/manager WITH a PPB registration on file.
* The low-stock job, an ORDER reply and a stockout trigger could each mint their
  own PO for the same product inside an hour.
"""
import asyncio
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def stocked():
    """One throwaway pharmacy with a product, a batch of 5 pieces, a customer."""
    import tenancy
    from db import ex, ex1

    mark = secrets.token_hex(3)
    ph = ex1("""insert into pharmacies (name, kind, status) values (%s,'tenant','active')
                returning id""", (f"HARD-{mark}",))
    pid = str(ph["id"])
    with tenancy.pharmacy_scope(pid):
        prod = ex1("""insert into products (pharmacy_id, name, pack_size, cost_price,
                                            sell_price)
                      values (%s,%s,10,5,9) returning id""", (pid, f"DRUG {mark}"))
        batch = ex1("""insert into batches (pharmacy_id, product_id, batch_no, qty_pieces)
                       values (%s,%s,%s,5) returning id""",
                    (pid, prod["id"], f"B-{mark}"))
        cust = ex1("""insert into customers (pharmacy_id, phone, consent_given)
                      values (%s,%s,true) returning id""", (pid, f"25479{mark[:8]}"))
        made = {"pid": pid, "product": str(prod["id"]), "batch": str(batch["id"]),
                "customer": str(cust["id"]), "mark": mark}
    yield made
    # order_lines, po_lines and agent_commands have no pharmacy_id; reach them via
    # the parent. purchase_orders/agents cascade to their lines/commands.
    ex("""delete from order_lines where order_id in
           (select id from orders where pharmacy_id=%s)""", (pid,))
    ex("""delete from po_lines where po_id in
           (select id from purchase_orders where pharmacy_id=%s)""", (pid,))
    for t in ("stock_movements", "orders", "payments", "batches",
              "products", "customers", "staff", "purchase_orders",
              "suppliers", "alerts", "agents"):
        ex(f"delete from {t} where pharmacy_id=%s", (pid,))
    ex("delete from pharmacies where id=%s", (pid,))


# ------------------------------------------------- negative stock / oversell
@db
def test_a_movement_cannot_take_a_batch_below_zero(stocked):
    import tenancy
    from db import InsufficientStock, apply_movement, q1, tx

    with tenancy.pharmacy_scope(stocked["pid"]):
        with pytest.raises(InsufficientStock):
            with tx() as cur:
                apply_movement(cur, stocked["batch"], -10, "sale")
        # nothing moved, nothing was ledgered
        assert q1("select qty_pieces from batches where id=%s",
                  (stocked["batch"],))["qty_pieces"] == 5
        assert q1("select count(*) n from stock_movements where batch_id=%s",
                  (stocked["batch"],))["n"] == 0


@db
def test_concurrent_takers_of_the_last_units_exactly_one_wins(stocked):
    """Two real transactions race for the last 5 pieces on separate connections.
    T1 locks the row; T2 blocks on the UPDATE, then re-evaluates against T1's
    committed value, matches zero rows and raises. One winner, no negative stock,
    no double allocation -- the invariant the atomic WHERE clause guarantees."""
    import psycopg
    import tenancy
    from db import InsufficientStock, apply_movement, q1

    dsn = os.environ["DATABASE_URL"]
    tenancy.set_pharmacy(stocked["pid"])

    from psycopg.rows import dict_row
    c1 = psycopg.connect(dsn, row_factory=dict_row, prepare_threshold=None)
    c2 = psycopg.connect(dsn, row_factory=dict_row, prepare_threshold=None)
    try:
        with c1.cursor() as cur:
            cur.execute("begin")
            apply_movement(cur, stocked["batch"], -5, "sale")
        # T1 holds the row lock, uncommitted. A real race has T1 committing WHILE
        # T2 waits: commit c1 from a thread once T2 is (about to be) blocked, so
        # c2's UPDATE re-evaluates against the committed value instead of timing out.
        import threading
        import time

        def _commit_t1():
            time.sleep(0.5)
            c1.commit()

        committer = threading.Thread(target=_commit_t1)
        committer.start()

        with c2.cursor() as cur:
            cur.execute("begin")
            cur.execute("set local lock_timeout = '10s'")
            with pytest.raises(InsufficientStock):
                apply_movement(cur, stocked["batch"], -5, "sale")
            c2.rollback()
        committer.join()

        assert q1("select qty_pieces from batches where id=%s",
                  (stocked["batch"],))["qty_pieces"] == 0
        # exactly one sale ledgered
        assert q1("select count(*) n from stock_movements where batch_id=%s",
                  (stocked["batch"],))["n"] == 1
    finally:
        c1.close()
        c2.close()
        tenancy.clear_pharmacy()


@db
def test_paid_order_with_vanished_stock_is_not_finalised(stocked):
    """The money moved but the shelf cannot honour the quote (stock sold between
    FEFO allocation and payment). The order must NOT be marked paid, the batch must
    not go negative, an alert must exist, and the customer gets an honest message
    instead of a delivery code for goods that do not exist."""
    import tenancy
    from db import ex1, q1
    from rx import on_payment_success

    with tenancy.pharmacy_scope(stocked["pid"]):
        order = ex1("""insert into orders (pharmacy_id, customer_id, status, total)
                       values (%s,%s,'awaiting_payment',45) returning id""",
                    (stocked["pid"], stocked["customer"]))
        # quote was for 5; only 2 remain on the batch by payment time is the race --
        # simpler: the line asks for more than the batch ever held
        ex1("""insert into order_lines (order_id, product_id, batch_id, qty_pieces,
                                        unit_price, line_total)
               values (%s,%s,%s,9,5,45) returning id""",
            (order["id"], stocked["product"], stocked["batch"]))

        on_payment_success(str(order["id"]), "RECEIPT1")

        o = q1("select status from orders where id=%s", (order["id"],))
        assert o["status"] == "awaiting_payment", (
            "an order that cannot be dispensed must not become 'paid'")
        assert q1("select qty_pieces from batches where id=%s",
                  (stocked["batch"],))["qty_pieces"] == 5, "batch must be untouched"
        alert = q1("""select * from alerts where pharmacy_id=%s
                       and kind='payment_stock_conflict'""", (stocked["pid"],))
        assert alert, "the conflict must be visible to staff, not just logged"


# ------------------------------------------------------- prescription capability
@db
def test_verification_is_a_capability_not_a_title(stocked):
    """Attendant: never. Manager/owner: only with a PPB registration on file.
    Pharmacist: yes. The operational title alone must not satisfy a clinical gate."""
    import tenancy
    from db import ex1, q1
    from rx import pharmacist_approve

    with tenancy.pharmacy_scope(stocked["pid"]):
        def _staff(role, ppb=None):
            return ex1("""insert into staff (pharmacy_id, phone, name, role, ppb_reg_no)
                          values (%s,%s,%s,%s,%s) returning id""",
                       (stocked["pid"], f"2547{secrets.token_hex(5)}",
                        f"{role}-{stocked['mark']}", role, ppb))

        def _rx():
            return ex1("""insert into prescriptions (pharmacy_id, customer_id,
                                                     image_path, status)
                          values (%s,%s,'x','pending_verification') returning id""",
                       (stocked["pid"], stocked["customer"]))

        attendant, manager, manager_ppb, pharmacist = (
            _staff("attendant"), _staff("manager"), _staff("manager", "PPB/1234"),
            _staff("pharmacist", "PPB/5678"))

        for who in (attendant, manager):
            rx = _rx()
            with pytest.raises(PermissionError):
                pharmacist_approve(str(rx["id"]), str(who["id"]))
            assert q1("select status from prescriptions where id=%s",
                      (rx["id"],))["status"] == "pending_verification"

        for who in (manager_ppb, pharmacist):
            rx = _rx()
            pharmacist_approve(str(rx["id"]), str(who["id"]))
            assert q1("select status from prescriptions where id=%s",
                      (rx["id"],))["status"] == "verified"


# ---------------------------------------------------------- agent hardening
@pytest.fixture
def two_agents():
    import tenancy
    from db import ex, ex1

    mark = secrets.token_hex(3)
    made = {}
    for side in ("a", "b"):
        ph = ex1("""insert into pharmacies (name, kind, status) values (%s,'tenant','active')
                    returning id""", (f"HARDAGENT-{side}-{mark}",))
        pid = str(ph["id"])
        tok = f"tok-{side}-{mark}"
        with tenancy.pharmacy_scope(pid):
            ex1("""insert into agents (pharmacy_id, agent_token, enrolment_token,
                                       machine_name)
                   values (%s,%s,%s,%s) returning id""",
                (pid, tok, f"enrol-{side}-{mark}", f"PC-{side}"))
        made[side] = {"pid": pid, "token": tok}
    yield made
    for side in ("a", "b"):
        d = made[side]
        # agent_commands has no pharmacy_id; deleting the agent cascades to them
        ex("delete from agents where pharmacy_id=%s", (d["pid"],))
        ex("delete from pharmacies where id=%s", (d["pid"],))


class _Req:
    def __init__(self, payload):
        self._p = payload

    async def json(self):
        return self._p


@db
def test_agent_cannot_finalise_another_agents_command(two_agents):
    """THE ownership bug: any agent token could finalise any command id, mark it
    done, and have its reply_text sent wherever that command pointed."""
    from fastapi import HTTPException

    from db import ex1
    import agent_api

    a, b = two_agents["a"], two_agents["b"]
    cmd = ex1("""insert into agent_commands (agent_id, command, reply_to)
                 values ((select id from agents where agent_token=%s),
                         'ping', '254700000001') returning id""",
              (b["token"],))
    # B takes it (the legit transition), then A tries to finalise it
    agent_api.take_commands(b["token"])
    with pytest.raises(HTTPException) as e:
        asyncio.run(agent_api.command_result(str(cmd["id"]), _Req({"ok": True}),
                                             a["token"]))
    assert e.value.status_code == 404
    from db import q1
    assert q1("select status from agent_commands where id=%s",
              (cmd["id"],))["status"] == "taken"


@db
def test_command_result_replay_has_no_side_effects(two_agents):
    """done -> done is not a valid transition. A network retry of a result POST
    must be acknowledged without re-sending the WhatsApp reply."""
    from db import ex1, q1
    import agent_api

    b = two_agents["b"]
    cmd = ex1("""insert into agent_commands (agent_id, command, reply_to)
                 values ((select id from agents where agent_token=%s),
                         'ping', '254700000001') returning id""",
              (b["token"],))
    agent_api.take_commands(b["token"])
    res = asyncio.run(agent_api.command_result(str(cmd["id"]), _Req({"ok": True}),
                                               b["token"]))
    assert res == {"ok": True}
    replay = asyncio.run(agent_api.command_result(str(cmd["id"]), _Req({"ok": True}),
                                                  b["token"]))
    assert replay.get("reason", "").startswith("not_taken")
    assert q1("select status from agent_commands where id=%s",
              (cmd["id"],))["status"] == "done"


@db
def test_suspended_agent_is_refused_everywhere_but_heartbeat(two_agents):
    from fastapi import HTTPException

    from db import ex
    import agent_api

    a = two_agents["a"]
    ex("update agents set suspended=true where agent_token=%s", (a["token"],))

    with pytest.raises(HTTPException) as e:
        agent_api.take_commands(a["token"])
    assert e.value.status_code == 403

    with pytest.raises(HTTPException) as e:
        asyncio.run(agent_api.pos_sales(_Req({"rows": []}), a["token"]))
    assert e.value.status_code == 403

    # heartbeat still answers, so the agent learns it is suspended and backs off
    hb = asyncio.run(agent_api.heartbeat(_Req({"agent_version": "1"}), a["token"]))
    assert hb["suspended"] is True


@db
def test_take_commands_leaves_no_tenant_bound(two_agents):
    """take_commands is a sync endpoint on a threadpool worker; its set_pharmacy
    binding must not outlive the call."""
    import tenancy
    import agent_api

    tenancy.clear_pharmacy()
    agent_api.take_commands(two_agents["a"]["token"])
    with pytest.raises(tenancy.NoTenant):
        tenancy.pid()


# ------------------------------------------------------------- state fail-closed
def test_set_state_without_a_tenant_fails_closed():
    """The last runtime fallback to settings.PHARMACY_ID, removed. A caller with no
    scope is a bug; filing its state under the .env pharmacy hides it."""
    import tenancy
    from state import set_state

    tenancy.clear_pharmacy()
    with pytest.raises(tenancy.NoTenant):
        set_state("254700000002", "grn_collect", {})


# ------------------------------------------------------------------ PO dedup
@db
def test_stockout_does_not_stack_a_second_po_on_an_open_one(stocked):
    """07:00 job, owner's ORDER reply, stockout trigger -- one PO, not three."""
    import tenancy
    from db import ex1, q1
    import restock

    with tenancy.pharmacy_scope(stocked["pid"]):
        ex1("""insert into suppliers (pharmacy_id, name, phone)
               values (%s,'Test Wholesaler','254700999999') returning id""",
            (stocked["pid"],))
        first_id, first_note = restock._draft_stockout_po(
            stocked["product"], "DRUG", 10, 4, urgent=False)
        assert first_id
        n = q1("select count(*) n from purchase_orders where pharmacy_id=%s",
               (stocked["pid"],))["n"]
        assert n == 1

        second_id, second_note = restock._draft_stockout_po(
            stocked["product"], "DRUG", 10, 6, urgent=True)
        assert second_id == first_id, "must reuse the open PO, not mint another"
        assert "already awaits approval" in second_note
        assert q1("select count(*) n from purchase_orders where pharmacy_id=%s",
                  (stocked["pid"],))["n"] == 1
        assert q1("select count(*) n from po_lines where product_id=%s",
                  (stocked["product"],))["n"] == 1, "one line, not stacked lines"
