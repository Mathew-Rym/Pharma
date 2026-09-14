"""The restock intelligence loop: demand -> log -> aggregate -> staff alert -> DRAFT
PO -> human approval -> (on delivery) notify the waiting customers.

Pinned behaviours, in order of how badly their silence would lie:

  * an out-of-stock inquiry is LOGGED even when no product matches — unmatched
    demand is still demand, and "not in your product list" is exactly the item a
    pharmacy most needs to know customers keep asking for
  * three requests in 7 days alert staff and draft a PO — but the PO is
    'awaiting_approval', never 'sent'. Auto-ordering is the one rule the whole
    system is built around breaking last.
  * the 48h re-trigger guard means requests 4..9 do not each re-alert. A
    notification system that repeats itself gets muted by the people it warns.
  * prescription-only inquiries get the pharmacist escalation and no availability
    discussion — POM is the closest thing the schema has to the controlled list,
    and PPB attribution is not negotiable.
  * the customer never sees "we don't have it" from the TOOL: the redirect rules
    live in the system prompt, the tool states facts and obligations only.
  * a delivery notifies exactly the customers who opted in (wants_notify), once
    (notified_at), capped per burst.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


@pytest.fixture()
def customer_phone():
    """A number that is NOT staff, so nothing in these tests can touch staff flows."""
    return "254701000299"


def _seed_supplier():
    """The throwaway pharmacy has staff but no supplier; a stockout draft PO needs one
    to route to (send_po_for_approval joins on suppliers). Scoped explicitly: the
    staff table holds every pharmacy's rows, so `limit 1` without a filter seeds the
    supplier into whichever pharmacy was inserted first."""
    import tenancy
    from config import settings
    from db import ex1
    with tenancy.pharmacy_scope(settings.PHARMACY_ID):
        return ex1("""insert into suppliers (pharmacy_id, name, phone)
                      values (%s,'Test Wholesaler','254700999999') returning id""",
                   (settings.PHARMACY_ID,))


def _stocked(name: str, qty: int = 100):
    """A product WITH stock, in the currently-scoped (throwaway) pharmacy."""
    import tenancy
    from config import settings
    from db import ex1
    with tenancy.pharmacy_scope(settings.PHARMACY_ID):
        p = ex1("""insert into products (pharmacy_id, name, generic_name, pack_size,
                                         cost_price, sell_price)
                   values (%s,%s,'gen',30,100,150) returning id""",
                (settings.PHARMACY_ID, name))
        b = ex1("""insert into batches (pharmacy_id, product_id, batch_no, qty_pieces,
                                        cost_price)
                   values (%s,%s,'T1',%s,100) returning id""",
                (settings.PHARMACY_ID, p["id"], qty))
        ex1("""insert into stock_movements (pharmacy_id, batch_id, delta_pieces, reason)
               values (%s,%s,%s,'opening') returning id""",
            (settings.PHARMACY_ID, b["id"], qty))
        return str(p["id"])


def _product(name):
    from db import q1
    return q1("select id, pack_size from products where name = %s", (name,))


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs a database")
class TestStockoutLogging:
    def test_out_of_stock_inquiry_is_logged_with_the_product(self, customer_phone):
        import tenancy
        from config import settings
        from db import q1
        import restock
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            # Amoxil exists in the seeded catalogue with stock; zero it by querying a
            # product that exists but has none. Brufen was created via NEW: 4 loose
            # pcs. Use a fresh product with zero stock instead.
            from db import ex1
            p = ex1("""insert into products (pharmacy_id, name, generic_name, pack_size,
                                             cost_price, sell_price, reorder_level_pieces)
                       values (%s,'ZeroStock Med 10mg','zerostock',30,100,150,10)
                       returning id""", (settings.PHARMACY_ID,))
            before = q1("select count(*) as n from stockout_log where product_id=%s",
                        (p["id"],))["n"]
            restock.record_stockout("ZeroStock Med 10mg", customer_phone)
            after = q1("""select count(*) as n, max(phone) as ph,
                                 bool_or(wants_notify) as w
                            from stockout_log where product_id=%s""", (p["id"],))
            assert after["n"] == before + 1
            assert after["ph"] == customer_phone
            assert after["w"] is False        # opt-in only, never assumed

    def test_unmatched_query_is_logged_with_null_product(self, customer_phone):
        import tenancy
        from config import settings
        from db import q1
        import restock
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            restock.record_stockout("Something We Do Not Stock", customer_phone)
            row = q1("""select * from stockout_log
                         where product_query = 'Something We Do Not Stock'""")
            assert row and row["product_id"] is None

    def test_check_stock_customer_says_in_stock_when_it_is(self, customer_phone):
        import tenancy
        from config import settings
        import restock
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            _stocked("Plentiful Med 20mg", 90)
            out = restock.check_stock_customer("Plentiful Med 20mg", customer_phone)
            assert out.startswith("IN STOCK")

    def test_check_stock_customer_out_of_stock_never_says_we_dont_have(
            self, customer_phone):
        import tenancy
        from config import settings
        from db import ex1
        import restock
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            p = ex1("""insert into products (pharmacy_id, name, generic_name, pack_size,
                                             cost_price, sell_price)
                       values (%s,'Empty Shelf 5mg','emptyshelf',30,100,150)
                       returning id""", (settings.PHARMACY_ID,))
            out = restock.check_stock_customer("Empty Shelf 5mg", customer_phone)
            assert out.startswith("OUT OF STOCK")
            assert "redirect" in out.lower()
            assert "NEVER open with 'we don't have it'" in out

    def test_prescription_only_gets_pharmacist_escalation_not_availability(
            self, customer_phone):
        import tenancy
        from config import settings
        from db import ex1
        import restock
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            ex1("""insert into products (pharmacy_id, name, generic_name, pack_size,
                                         cost_price, sell_price, is_prescription_only)
                   values (%s,'Strong scripted 2mg','scripted',30,100,150,true)
                   returning id""", (settings.PHARMACY_ID,))
            out = restock.check_stock_customer("Strong scripted 2mg", customer_phone)
            assert out.startswith("PRESCRIPTION-ONLY")
            assert "pharmacist" in out.lower()
            assert "do not discuss availability" in out.lower()


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs a database")
class TestTriggerAndApproval:
    def _three_requests(self, name, phone_base="2547010002"):
        import tenancy
        from config import settings
        from db import ex1
        import restock
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            _seed_supplier()
            p = ex1("""insert into products (pharmacy_id, name, generic_name, pack_size,
                                             cost_price, sell_price)
                       values (%s,%s,'trigtest',30,100,150) returning id""",
                    (settings.PHARMACY_ID, name))
            for i in range(3):
                restock.record_stockout(name, f"{phone_base}{10 + i}")
            return p["id"]

    def test_three_requests_create_an_AWAITING_APPROVAL_po(self):
        import tenancy
        from config import settings
        from db import q1
        pid_ = self._three_requests("Trigger Med A 10mg")
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            po = q1("""select * from purchase_orders
                        where reason->>'trigger' = 'stockout'
                          and id in (select po_id from po_lines where product_id = %s)""",
                    (pid_,))
            assert po, "no stockout-triggered draft PO was created"
            assert po["status"] == "awaiting_approval"    # NEVER 'sent' automatically

    def test_po_quantity_scales_with_demand(self):
        # 3 requests x 1.5 = 4.5 -> ceil 5 packs x pack_size 30 = 150 pcs
        import tenancy
        from config import settings
        from db import q1
        pid_ = self._three_requests("Trigger Med B 10mg")
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            line = q1("""select qty_pieces from po_lines where product_id = %s
                          order by qty_pieces desc limit 1""", (pid_,))
            assert line["qty_pieces"] == 150

    def test_retrigger_guard_blocks_a_second_po_within_48h(self):
        import tenancy
        from config import settings
        from db import q, q1
        import restock
        pid_ = self._three_requests("Trigger Med C 10mg")
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            # demand keeps arriving after the alert already fired
            restock.record_stockout("Trigger Med C 10mg", "254701000299")
            restock.record_stockout("Trigger Med C 10mg", "254701000298")
            pos = q("""select * from purchase_orders
                        where reason->>'trigger' = 'stockout'
                          and id in (select po_id from po_lines where product_id = %s)""",
                    (pid_,))
            assert len(pos) == 1, f"re-triggered {len(pos)} POs; alert spam"

    def test_two_requests_do_not_trigger_yet(self):
        import tenancy
        from config import settings
        from db import ex1, q1
        import restock
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            p = ex1("""insert into products (pharmacy_id, name, generic_name, pack_size,
                                             cost_price, sell_price)
                       values (%s,'Quiet Med D 10mg','quiet',30,100,150) returning id""",
                    (settings.PHARMACY_ID,))
            restock.record_stockout("Quiet Med D 10mg", "254701000290")
            restock.record_stockout("Quiet Med D 10mg", "254701000291")
            po = q1("""select * from purchase_orders
                        where id in (select po_id from po_lines where product_id = %s)""",
                    (p["id"],))
            assert not po, "triggered below the 3-request threshold"


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs a database")
class TestRestockNotification:
    def _setup(self, name, phone):
        """A customer who asked, got told it's out, said yes to the alert."""
        import tenancy
        from config import settings
        from db import ex1
        import restock
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            p = ex1("""insert into products (pharmacy_id, name, generic_name, pack_size,
                                             cost_price, sell_price)
                       values (%s,%s,'notifytest',30,100,150) returning id""",
                    (settings.PHARMACY_ID, name))
            restock.record_stockout(name, phone)
            restock.request_restock_alert(name, phone)
            return str(p["id"])

    def test_opt_in_is_recorded(self):
        import tenancy
        from config import settings
        from db import q1
        name = "Notify Med E 10mg"
        ph = "254701000280"
        self._setup(name, ph)
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            row = q1("""select wants_notify, notified_at from stockout_log
                         where phone = %s order by created_at desc limit 1""", (ph,))
            assert row["wants_notify"] is True
            assert row["notified_at"] is None

    def test_delivery_notifies_the_optin_customer_once(self):
        import tenancy
        from config import settings
        from db import ex, ex1, q1
        from restock import notify_restocked
        name = "Notify Med F 10mg"
        ph = "254701000270"
        prod = self._setup(name, ph)
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            # A GRN for this product, approved shape
            g = ex1("""insert into grns (pharmacy_id, status) values (%s,'approved')
                       returning id""", (settings.PHARMACY_ID,))
            ex("insert into grn_lines (grn_id, line_no, raw_description, product_id,"
               " qty_invoiced_pieces) values (%s,1,%s,%s,30)",
               (g["id"], name, prod))
            notify_restocked(str(g["id"]))
            row = q1("""select wants_notify, notified_at from stockout_log
                         where phone = %s order by created_at desc limit 1""", (ph,))
            assert row["notified_at"] is not None
            # Second delivery of the same product must NOT re-notify
            g2 = ex1("""insert into grns (pharmacy_id, status) values (%s,'approved')
                        returning id""", (settings.PHARMACY_ID,))
            ex("insert into grn_lines (grn_id, line_no, raw_description, product_id,"
               " qty_invoiced_pieces) values (%s,1,%s,%s,30)",
               (g2["id"], name, prod))
            notify_restocked(str(g2["id"]))
            row2 = q1("""select count(*) as n from stockout_log
                         where phone = %s and notified_at is not null""", (ph,))
            assert row2["n"] == 1


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs a database")
class TestToolBoundary:
    def test_customer_tools_are_not_in_the_staff_tool_list(self):
        """reports.TOOLS feeds tools_for(role); a customer tool in it would widen what
        staff models may pick and break the role-cap invariants."""
        from reports import CUSTOMER_TOOLS, TOOLS
        staff_names = {t["name"] for t in TOOLS}
        customer_names = {t["name"] for t in CUSTOMER_TOOLS}
        assert customer_names == {"check_stock", "notify_me_when_back"}
        assert not (customer_names & staff_names)

    def test_customer_tools_reach_run_tool(self):
        import tenancy
        from config import settings
        from reports import run_tool
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            _stocked("Runnable Med 20mg", 60)
            out = run_tool("check_stock", {"product_query": "Runnable Med 20mg"},
                           "254701000299")
            assert out.startswith("IN STOCK")
