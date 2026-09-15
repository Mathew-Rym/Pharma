"""Price management + owner briefings.

The two features are tested together because they close one loop: stock arrives
unpriced -> the morning briefing names it -> the owner sets the price over
WhatsApp -> the customer can be quoted honestly. Each test pins the rule the
feature exists to enforce:

* a price change is staged, shown with margins, and applied ONLY on CONFIRM
* the product resolution never guesses between close names
* only owner/manager may manage prices; a customer never sees cost
* every change leaves a tenant-scoped audit row
* briefings are deterministic text from tenant-scoped SQL, idempotent per day,
  and quiet (not empty) when there is nothing to say
* a NULL sell_price is never quoted to a customer as KES 0
"""
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def shop():
    """A pharmacy with an owner, an attendant, and three products:
    priced, unpriced-with-cost, unpriced-without-cost.

    The pharmacy row carries a fake-but-unique wa_jid/gowa_device_id so it
    satisfies tenancy.LIVE_SQL and wa.compose() will build messages for it
    (same pattern as conftest's throwaway). Nothing is ever delivered -- no
    GOWA slot by these names exists -- but the composed row is what these
    tests assert on. The owner also gets an inbound_history row: safety Gate
    3 (chat established) blocks sends to a phone that never wrote in, which
    is correct behaviour, not an obstacle to route around in production.
    """
    import tenancy
    from db import ex, ex1
    from safety import record_inbound

    mark = secrets.token_hex(3)
    # Phones must be digits-only: mark is hex and hex contains letters, and a
    # lettered "phone" gets mangled by norm_phone and then refused by safety
    # Gate 2 as a stranger. Distinct prefixes keep the roles' numbers apart.
    dig = f"{int(mark, 16) % 10**7:07d}"
    owner_phone, att_phone = f"25471{dig}", f"25472{dig}"
    ph = ex1("""insert into pharmacies (name, kind, status, wa_jid, gowa_device_id)
                values (%s,'tenant','active',%s,%s) returning id""",
             (f"PRICE-{mark}", f"25479{mark[:6]}@s.whatsapp.net", f"price-{mark}"))
    pid = str(ph["id"])
    with tenancy.pharmacy_scope(pid):
        record_inbound(owner_phone, pid)
        owner = ex1("""insert into staff (pharmacy_id, phone, name, role)
                       values (%s,%s,'Owner','owner') returning id""",
                    (pid, owner_phone))
        attendant = ex1("""insert into staff (pharmacy_id, phone, name, role)
                           values (%s,%s,'Att','attendant') returning id""",
                        (pid, att_phone))
        p_priced = ex1("""insert into products (pharmacy_id, name, pack_size,
                                                 cost_price, sell_price)
                          values (%s,%s,10,100,150) returning id""",
                       (pid, f"PRICED MED {mark}"))
        p_costonly = ex1("""insert into products (pharmacy_id, name, pack_size,
                                                  cost_price)
                            values (%s,%s,10,180) returning id""",
                         (pid, f"COSTONLY MED {mark}"))
        p_bare = ex1("""insert into products (pharmacy_id, name, pack_size)
                        values (%s,%s,10) returning id""",
                     (pid, f"BARE MED {mark}"))
        # stock for the unpriced ones: they are "stocked but unsellable"
        for p in (p_costonly, p_bare):
            b = ex1("""insert into batches (pharmacy_id, product_id, batch_no,
                                            qty_pieces)
                       values (%s,%s,'B1',50) returning id""", (pid, p["id"]))
            ex("""insert into stock_movements (pharmacy_id, batch_id, delta_pieces,
                                               reason)
                  values (%s,%s,50,'grn')""", (pid, b["id"]))
        made = {"pid": pid, "mark": mark, "dig": dig,
                "owner": {"id": owner["id"], "phone": owner_phone},
                "attendant": {"id": attendant["id"], "phone": att_phone},
                "priced": str(p_priced["id"]),
                "costonly": str(p_costonly["id"]), "bare": str(p_bare["id"])}
    yield made
    ex("""delete from price_history where pharmacy_id=%s""", (pid,))
    ex("""delete from pharmacy_settings where pharmacy_id=%s""", (pid,))
    ex("""delete from stock_movements where pharmacy_id=%s""", (pid,))
    ex("""delete from order_lines where order_id in
           (select id from orders where pharmacy_id=%s)""", (pid,))
    for t in ("inbound_history", "job_runs", "wa_messages", "wa_state", "orders",
              "batches", "products", "staff", "customers"):
        ex(f"delete from {t} where pharmacy_id=%s", (pid,))
    ex("delete from pharmacies where id=%s", (pid,))


def _last_out(pid, phone):
    from db import q
    rows = q("""select body from wa_messages where pharmacy_id=%s
                 and to_phone=%s and direction='out'
                 order by created_at desc limit 1""", (pid, phone))
    return (rows[0]["body"] or "") if rows else ""


# ------------------------------------------------------------------ parsing
def test_parse_price_command_shapes():
    import prices
    assert prices.parse_price_command("price Panadol 500mg 250") == ("Panadol 500mg", "250")
    assert prices.parse_price_command("set price Panadol to 250") == ("Panadol", "250")
    assert prices.parse_price_command("change Panadol price to 280") == ("Panadol", "280")
    assert prices.parse_price_command("set Augmentin 625mg at 850") == ("Augmentin 625mg", "850")
    assert prices.parse_price_command("what is the price of Panadol?")[1] is None
    assert prices.parse_price_command("hello there") == (None, None)
    assert prices.parse_price_command("PRICES") == (None, None)   # keyword, not a parse


def test_invalid_amounts_are_rejected():
    import prices
    assert prices._parse_amount("0") is None
    assert prices._parse_amount("-5") is None
    assert prices._parse_amount("abc") is None
    assert prices._parse_amount("99999999") is None
    assert prices._parse_amount("250") is not None
    assert prices._parse_amount("249.50") is not None


# ------------------------------------------------------------------ capability
def test_only_owner_and_manager_may_manage_prices():
    from reports import may_use
    assert may_use("owner", "manage_prices")
    assert may_use("manager", "manage_prices")
    assert not may_use("pharmacist", "manage_prices")
    assert not may_use("attendant", "manage_prices")
    assert not may_use(None, "manage_prices")          # customers: fail closed
    assert not may_use("", "manage_prices")


def test_set_price_tool_is_not_in_customer_tools():
    """The customer tool list must never offer the price tools: cost prices and
    margins are procurement data."""
    from reports import CUSTOMER_TOOLS
    names = {t["name"] for t in CUSTOMER_TOOLS}
    assert "set_price" not in names
    assert "get_price" not in names
    assert "missing_prices" not in names


def test_agent_loop_refuses_unoffered_tools(shop):
    """A model naming a tool the role was never given must be refused, not run.
    This is the enforcement the filtered list always implied but never had."""
    import tenancy
    from router import _agent_reply
    from db import q as qdb
    import router

    # An attendant's model "calls" set_price anyway (simulated by monkeypatching
    # chat to return a tool_use the attendant was never offered).
    class _TU:
        type, id = "tool_use", "tu1"
        name, input = "set_price", {"product_query": "PRICED", "price": 1}

    class _Resp:
        content = [_TU()]

    import llm
    orig = llm.chat
    def _fake(system, messages, tools=None):
        return _Resp()
    llm.chat = _fake
    try:
        with tenancy.pharmacy_scope(shop["pid"]):
            attendant_tools = [t for t in _tools() if t["name"] != "set_price"]
            _agent_reply(shop["attendant"]["phone"], "set the price",
                         "sys", attendant_tools)
    finally:
        llm.chat = orig
    # nothing staged, nothing changed
    from state import get_state
    with tenancy.pharmacy_scope(shop["pid"]):
        assert get_state(shop["attendant"]["phone"])["flow"] == "idle"
    row = qdb("select sell_price from products where id=%s", (shop["priced"],))
    assert float(row[0]["sell_price"]) == 150.0


def _tools():
    from reports import tools_for
    return tools_for("attendant")


# ------------------------------------------------------------------ flows
@db
def test_missing_price_flow_with_suggestion_and_confirm(shop):
    """show_price on an unpriced product suggests from cost; CONFIRM applies it
    and writes the audit row."""
    import tenancy
    from db import q1
    import prices

    with tenancy.pharmacy_scope(shop["pid"]):
        prices.show_price(shop["owner"]["phone"], shop["owner"],
                          f"COSTONLY MED {shop['mark']}")
        out = _last_out(shop["pid"], shop["owner"]["phone"])
        assert "no selling price" in out
        assert "Suggested selling price" in out          # cost 180 -> 250 (1.4x, clean 10)
        assert "CONFIRM" in out

        assert prices.handle_confirm(shop["owner"]["phone"], shop["owner"], "CONFIRM")
        out = _last_out(shop["pid"], shop["owner"]["phone"])
        assert "now KES 250" in out

        prod = q1("select sell_price from products where id=%s", (shop["costonly"],))
        assert float(prod["sell_price"]) == 250.0
        hist = q1("select * from price_history where product_id=%s",
                  (shop["costonly"],))
        assert hist is not None
        assert hist["old_price"] is None
        assert float(hist["new_price"]) == 250.0
        assert str(hist["actor_staff"]) == str(shop["owner"]["id"])
        assert hist["source"] == "suggested"


@db
def test_existing_price_change_shows_both_margins(shop):
    import tenancy
    import prices

    with tenancy.pharmacy_scope(shop["pid"]):
        prices.begin_price_change(shop["owner"]["phone"], shop["owner"],
                                  f"PRICED MED {shop['mark']}", "180")
        out = _last_out(shop["pid"], shop["owner"]["phone"])
        assert "Current price" in out and "New price" in out
        assert "Current margin" in out and "New margin" in out
        assert "CONFIRM" in out

        prices.handle_confirm(shop["owner"]["phone"], shop["owner"], "CONFIRM")
        out = _last_out(shop["pid"], shop["owner"]["phone"])
        assert "KES 180" in out


@db
def test_confirmation_is_per_tenant_and_per_person(shop):
    """A CONFIRM from pharmacy A's owner must never apply pharmacy B's staged
    change for the same phone number. State is (pharmacy_id, phone)."""
    import tenancy
    from db import ex1
    import prices

    other = ex1("""insert into pharmacies (name, kind, status) values (%s,'tenant','active')
                   returning id""", (f"OTHER-{shop['mark']}",))
    try:
        with tenancy.pharmacy_scope(shop["pid"]):
            prices.begin_price_change(shop["owner"]["phone"], shop["owner"],
                                      f"PRICED MED {shop['mark']}", "999")
        # same phone, other pharmacy: no staged change there
        with tenancy.pharmacy_scope(str(other["id"])):
            assert not prices.handle_confirm(shop["owner"]["phone"], shop["owner"],
                                             "CONFIRM")
        # and confirming from the right tenant works
        with tenancy.pharmacy_scope(shop["pid"]):
            assert prices.handle_confirm(shop["owner"]["phone"], shop["owner"],
                                         "CONFIRM")
    finally:
        from db import ex
        ex("delete from price_history where pharmacy_id=%s", (str(other["id"]),))
        ex("delete from pharmacies where id=%s", (str(other["id"]),))


@db
def test_double_confirm_is_a_noop(shop):
    import tenancy
    from db import q1
    import prices

    with tenancy.pharmacy_scope(shop["pid"]):
        prices.begin_price_change(shop["owner"]["phone"], shop["owner"],
                                  f"PRICED MED {shop['mark']}", "200")
        assert prices.handle_confirm(shop["owner"]["phone"], shop["owner"], "CONFIRM")
        # state cleared by the first confirm; a second CONFIRM is not consumed
        assert not prices.handle_confirm(shop["owner"]["phone"], shop["owner"],
                                         "CONFIRM")
        hist = q1("""select count(*) as n from price_history
                      where product_id=%s""", (shop["priced"],))
        assert hist["n"] == 1, "duplicate confirmation must not double-write"


@db
def test_ambiguous_product_asks_rather_than_guesses(shop):
    import tenancy
    from db import ex1
    import prices

    with tenancy.pharmacy_scope(shop["pid"]):
        # Two products equally close to the query: a trigram search cannot
        # separate them (similarity gap ~0.04 < AMBIGUITY_GAP), so the flow
        # must ask. Querying an EXACT name, by contrast, resolves at once --
        # that is covered by the other flows.
        for suffix in ("SYRUP", "TABS"):
            ex1("""insert into products (pharmacy_id, name, pack_size, cost_price, sell_price)
                    values (%s,%s,10,100,150) returning id""",
                (shop["pid"], f"AMBIG MED {shop['mark']} {suffix}"))
        prices.begin_price_change(shop["owner"]["phone"], shop["owner"],
                                  f"AMBIG MED {shop['mark']}", "200")
        out = _last_out(shop["pid"], shop["owner"]["phone"])
        assert "Which one?" in out

        # and nothing was staged while ambiguous
        from state import get_state
        assert get_state(shop["owner"]["phone"])["flow"] == "idle"


@db
def test_unpriced_product_is_never_quoted_as_zero(shop):
    """The customer quote flow must refuse an unpriced item rather than quote
    KES 0.00 for it."""
    import tenancy
    from rx import _build_quote
    from db import q1

    with tenancy.pharmacy_scope(shop["pid"]):
        cust = q1("""insert into customers (pharmacy_id, phone, consent_given)
                     values (%s,%s,true) returning id""",
                  (shop["pid"], f"25473{shop['dig']}"))
        order_id, avail, missing = _build_quote(
            str(cust["id"]), None,
            [{"drug": f"COSTONLY MED {shop['mark']}", "qty": 2}])
        assert not avail, "an unpriced product must not appear as an available line"
        assert any("price to be confirmed" in m for m in missing)
        # and no zero-priced order line was written
        lines = q1("""select count(*) as n from order_lines
                       where order_id=%s""", (order_id,))
        assert lines["n"] == 0


# ------------------------------------------------------------------ briefings
@db
def test_morning_briefing_names_missing_prices_and_sends_once(shop):
    import tenancy
    from jobs import morning_briefing
    from db import q as qdb

    with tenancy.pharmacy_scope(shop["pid"]):
        r1 = morning_briefing()
        assert r1["status"] == "ok" and r1["sent"]
        assert r1["missing_prices"] == 2          # costonly + bare are stocked, unpriced
        out = _last_out(shop["pid"], shop["owner"]["phone"])
        assert "Good morning" in out
        assert "Missing prices" in out

        # idempotent: a second run the same day (restart, double cron) sends nothing
        r2 = morning_briefing()
        assert r2.get("reason") == "already_sent_today"


@db
def test_afternoon_briefing_is_quiet_when_nothing_changed(shop):
    import tenancy
    from jobs import afternoon_briefing

    with tenancy.pharmacy_scope(shop["pid"]):
        r = afternoon_briefing()
        assert r["status"] == "ok"
        out = _last_out(shop["pid"], shop["owner"]["phone"])
        assert "on track" in out


@db
def test_evening_briefing_reports_and_replaces_daily_digest(shop):
    import tenancy
    from jobs import daily_digest, evening_briefing

    with tenancy.pharmacy_scope(shop["pid"]):
        r = evening_briefing()
        assert r["status"] == "ok" and r["sent"]
        out = _last_out(shop["pid"], shop["owner"]["phone"])
        assert "End of day" in out and "Tomorrow" in out

        # daily_digest is the same job now: the guard sees evening_briefing's run
        r2 = daily_digest()
        assert r2.get("reason") == "already_sent_today"


@db
def test_briefing_respects_tenant_settings(shop):
    import tenancy
    from jobs import morning_briefing
    import briefings

    with tenancy.pharmacy_scope(shop["pid"]):
        briefings.set_briefing("morning", False)
        r = morning_briefing()
        assert r.get("reason") == "disabled"


@db
def test_briefing_numbers_are_tenant_scoped(shop):
    """Pharmacy B's missing prices must not appear in pharmacy A's briefing."""
    import tenancy
    from db import ex, ex1
    from jobs import morning_briefing

    other = ex1("""insert into pharmacies (name, kind, status, wa_jid, gowa_device_id)
                   values (%s,'tenant','active',%s,%s) returning id""",
                (f"BRIEF-B-{shop['mark']}",
                 f"25478{shop['mark'][:6]}@s.whatsapp.net", f"briefb-{shop['mark']}"))
    try:
        with tenancy.pharmacy_scope(str(other["id"])):
            # B has an owner (recipient) and one unpriced stocked product
            ex1("""insert into staff (pharmacy_id, phone, name, role)
                   values (%s,%s,'B Owner','owner') returning id""",
                (str(other["id"]), f"25474{shop['dig']}"))
            from safety import record_inbound
            record_inbound(f"25474{shop['dig']}", str(other["id"]))
            p = ex1("""insert into products (pharmacy_id, name, pack_size)
                       values (%s,%s,10) returning id""",
                    (str(other["id"]), f"B UNPRICED {shop['mark']}"))
            b = ex1("""insert into batches (pharmacy_id, product_id, batch_no,
                                            qty_pieces)
                       values (%s,%s,'B',5) returning id""",
                    (str(other["id"]), p["id"]))
            ex("""insert into stock_movements (pharmacy_id, batch_id, delta_pieces,
                                               reason)
                  values (%s,%s,5,'grn')""", (str(other["id"]), b["id"]))
            rB = morning_briefing()
            assert rB["missing_prices"] == 1
            outB = _last_out(str(other["id"]), f"25474{shop['dig']}")
            # B's briefing carries B's own numbers: 1 missing price, not A's 2
            assert "Good morning" in outB
            assert "Missing prices" in outB and "1 medicine" in outB
            # A's briefing still counts A's products, not B's
        with tenancy.pharmacy_scope(shop["pid"]):
            rA = morning_briefing()
            if rA.get("sent"):
                outA = _last_out(shop["pid"], shop["owner"]["phone"])
                assert f"B UNPRICED {shop['mark']}" not in outA
    finally:
        from db import ex as _ex
        for t in ("inbound_history", "stock_movements", "wa_messages", "batches",
                  "products", "staff", "job_runs", "price_history",
                  "pharmacy_settings"):
            _ex(f"delete from {t} where pharmacy_id=%s", (str(other["id"]),))
        _ex("delete from pharmacies where id=%s", (str(other["id"]),))
