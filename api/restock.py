"""FLOW B extension — restock intelligence from patient inquiries.

The reorder engine reads SALES (what moved). This module adds DEMAND (what people
asked for and could not get): every out-of-stock inquiry is logged, aggregated over
a 7-day window, and at threshold a staff alert plus a small-batch DRAFT purchase
order is prepared.

Money never moves without a human: the draft goes through the existing
approvals.send_po_for_approval() — OKPO + PIN — which is the only path an order
leaves by. That is the same rule the forecast's `PO` command obeys.

Thresholds (per 7-day window, per product):
  >= 3 requests  -> high priority: staff alert + draft PO
  >= 10 requests -> urgent: same, flagged 🚨 and escalated to owner/manager

Re-trigger guard: one alert per product per 48h, via triggered_at on the log rows.
Without it, request #4..#9 each re-alert and re-draft, which is how a notification
system gets muted by the people it exists to warn.
"""
import json
import logging
import math

from config import settings
from db import ex, ex1, q, q1
from tenancy import pid
from utils import from_pieces, kes

log = logging.getLogger(__name__)

HIGH_THRESHOLD = 3      # requests in 7d that justify a small-batch order
URGENT_THRESHOLD = 10
RETRIGGER_HOURS = 48
NOTIFY_CAP_PER_PRODUCT = 10   # per delivery, anti-burst; surplus waits for the next


def _match_product(query: str):
    """Same trigram match the rest of the system uses, so the customer's words and
    the catalogue agree on what product is being talked about."""
    if not query or not query.strip():
        return None
    row = q1(
        """select id, name, pack_size, is_prescription_only
             from products
            where pharmacy_id = %s and similarity(name, %s) > %s
            order by similarity(name, %s) desc limit 1""",
        (pid(), query, settings.MATCH_THRESHOLD, query),
    )
    return row


def record_stockout(product_query: str, phone: str, language: str = "en") -> None:
    """Log one out-of-stock inquiry and run the aggregation trigger.

    Never raises into the caller's reply path: a logging failure must not turn a
    customer's "do you have X" into an error message.
    """
    try:
        product = _match_product(product_query)
        product_id = (product or {}).get("id")
        ex(
            """insert into stockout_log (pharmacy_id, product_id, product_query, phone, language)
               values (%s,%s,%s,%s,%s)""",
            (pid(), product_id, (product_query or "")[:200], phone, (language or "en")[:8]),
        )
        if product_id:
            _maybe_trigger(product_id, product["name"], product["pack_size"])
    except Exception:
        log.exception("stockout logging failed for %r", product_query)


def _maybe_trigger(product_id, name: str, pack_size: int) -> None:
    """Aggregate the 7-day window for this product and act at threshold."""
    row = q1(
        """select count(*) as n,
                  max(triggered_at) as last_trigger
             from stockout_log
            where pharmacy_id = %s and product_id = %s
              and created_at > now() - interval '7 days'""",
        (pid(), product_id),
    )
    count = row["n"] or 0
    if count < HIGH_THRESHOLD:
        return

    # De-dup: within the retrigger window, keep counting but do not re-alert.
    if row["last_trigger"]:
        r = q1("select extract(epoch from now() - %s)/3600 as h", (row["last_trigger"],))
        if float(r["h"] or 0) < RETRIGGER_HOURS:
            return

    urgent = count >= URGENT_THRESHOLD
    po_id, po_note = _draft_stockout_po(product_id, name, pack_size, count, urgent)
    _alert_staff(name, count, po_note, urgent, po_id=po_id)
    ex("""update stockout_log set triggered_at = now()
           where pharmacy_id = %s and product_id = %s and triggered_at is null""",
       (pid(), product_id))


def _suggest_packs(count_7d: int, pack_size: int) -> int:
    """Each unmet request ≈ one customer wanting ~1 pack. 1.5x safety factor, minimum
    3 packs so a single delivery is worth the supplier's transport. No MOQ data
    exists in the schema, so the minimum acts as one."""
    ps = max(int(pack_size or 1), 1)
    return max(3, math.ceil(count_7d * 1.5))


def _draft_stockout_po(product_id, name: str, pack_size: int, count: int,
                       urgent: bool) -> tuple[str | None, str]:
    """Create a draft PO for this product only. Returns (po_id, human note for the
    alert).

    Mirrors forecast.create_draft_pos's insert shape but keyed to one product and
    one trigger, because reorder_list() reads sales velocity and a stockout product
    with no sales history would never appear there — which is precisely the gap this
    module exists to close.

    Dedup: the 07:00 low-stock job, an owner's ORDER reply and this stockout trigger
    can all fire for the same product inside an hour, and each used to mint its own
    PO. If an unsent PO for this product already awaits approval, say so instead of
    stacking another on top.
    """
    dup = q1(
        """select po.id from purchase_orders po
             join po_lines l on l.po_id = po.id
            where po.pharmacy_id = %s and l.product_id = %s
              and po.status in ('draft','awaiting_approval')
            order by po.created_at desc limit 1""",
        (pid(), product_id),
    )
    if dup:
        return str(dup["id"]), (f"a draft PO for {name} already awaits approval "
                                f"({str(dup['id'])[:8].upper()}) — reply *PO* to review it")

    p = q1(
        """select p.cost_price, s.id as supplier_id, s.name as supplier
             from products p left join suppliers s on s.id = p.preferred_supplier_id
            where p.id = %s and p.pharmacy_id = %s""",
        (product_id, pid()),
    )
    supplier_name = (p or {}).get("supplier")
    if not supplier_name or not p.get("supplier_id"):
        # preferred_supplier_id is NULL (e.g. product created via `n NEW` from a text
        # invoice). Fall back to any supplier on file; a PO with no supplier cannot be
        # routed to anyone and send_po_for_approval joins on suppliers.
        s = q1("""select id, name from suppliers where pharmacy_id = %s
                   order by name limit 1""", (pid(),))
        if not s:
            return None, "no supplier on file — add one on the dashboard, then reply *PO*"
        supplier_name, supplier_id = s["name"], s["id"]
    else:
        supplier_id = p["supplier_id"]

    ps = max(int(pack_size or 1), 1)
    packs = _suggest_packs(count, ps)
    qty_pieces = packs * ps
    unit_cost = float((p or {}).get("cost_price") or 0)
    est = round(qty_pieces * unit_cost, 2)

    po = ex1(
        """insert into purchase_orders (pharmacy_id, supplier_id, status, reason,
                           total_estimate)
           values (%s,%s,'awaiting_approval',%s,%s) returning id""",
        (pid(), supplier_id,
         json.dumps({"trigger": "stockout", "requests_7d": count,
                     "urgent": urgent, "requested_by": None}),
         est),
    )
    ex(
        """insert into po_lines (po_id, product_id, qty_pieces, unit_cost, rationale)
           values (%s,%s,%s,%s,%s)""",
        (po["id"], product_id, qty_pieces, unit_cost,
         f"{count} customer requests in 7 days, 0 in stock (x1.5 safety)"),
    )
    log.info("stockout draft PO %s for %s (%d requests)", po["id"], name, count)
    return str(po["id"]), (f"draft PO {str(po['id'])[:8].upper()} · {packs} packs "
                           f"({qty_pieces} pcs) from {supplier_name} · {kes(est)} "
                           f"— awaiting your approval")


def _alert_staff(name: str, count: int, po_note: str, urgent: bool,
                 po_id: str | None = None) -> None:
    """One alert to each owner/manager. Sends go through the usual safety gates; a
    blocked send is logged by wa.py, never fatal here.

    When a draft PO exists it is ALSO routed through send_po_for_approval(), which is
    the message carrying the OKPO+PIN instructions -- the alert without it promises
    an approval flow that never arrives."""
    from wa import reply_text
    head = "🚨 *URGENT RESTOCK NEEDED*" if urgent else "⚠️ *STOCKOUT ALERT*"
    body = (f"{head}\n"
            f"Medicine: *{name}*\n"
            f"Requests (7d): {count}\n"
            f"Current stock: 0\n"
            f"{po_note}")
    for s in q("""select phone from staff where pharmacy_id = %s and is_active
                   and role in ('owner','manager')""", (pid(),)):
        reply_text(s["phone"], body)
    if po_id:
        try:
            from approvals import send_po_for_approval
            send_po_for_approval(po_id)
        except Exception:
            # The alert itself went out; the approver can run `PO`/dashboard as the
            # fallback. Never let the notification path take the inquiry down.
            log.exception("stockout PO approval routing failed for %s", po_id)


def notify_restocked(grn_id: str) -> None:
    """After a GRN is approved, tell the customers who asked to be told.

    Only rows with wants_notify (the customer said yes to the alert) are messaged,
    which is what makes the promise honest under DPA consent. Cap per product per
    delivery keeps the send under the rate gates; the rest go out on the next
    delivery of the same product.
    """
    from wa import reply_text
    ph = q1("select name from pharmacies where id = %s", (pid(),))
    rows = q(
        """select sl.id, sl.phone, p.name
             from stockout_log sl join grn_lines gl on gl.product_id = sl.product_id
             join products p on p.id = sl.product_id
            where gl.grn_id = %s and sl.pharmacy_id = %s
              and sl.wants_notify and sl.notified_at is null
            limit %s""",
        (grn_id, pid(), NOTIFY_CAP_PER_PRODUCT),
    )
    for r in rows:
        reply_text(r["phone"],
                   f"Good news — *{r['name']}*, which you asked about, is back in stock "
                   f"at {ph['name'] if ph else 'the pharmacy'}. Reply here or come by "
                   f"and we'll sort you out.")
        ex("update stockout_log set notified_at = now() where id = %s", (r["id"],))
    if rows:
        log.info("restock notices sent to %d waiting customer(s)", len(rows))


# ------------------------------------------------------------ customer tools
def check_stock_customer(product_query: str, phone: str) -> str:
    """The CUSTOMER-facing stock check: same SQL as reports.get_stock, plus the
    restock behaviours this module exists for.

    Returns tool text that STEERS the reply without writing it: the redirect-language
    rules live in the system prompt, and the facts + obligations live here. Never
    says "in stock" when it is not; never lets a POM inquiry turn into a discussion
    of availability.
    """
    product = _match_product(product_query)
    if not product:
        record_stockout(product_query, phone)
        return (f"NOT FOUND: no product matching '{product_query}'. A restock request "
                f"has been logged. Use redirect language: acknowledge, ask what they "
                f"are treating or how urgent it is, offer the pharmacist. Do NOT open "
                f"with 'we don't have it'.")

    if product["is_prescription_only"]:
        # POM ≈ the controlled-substance escalation: no availability discussion, no
        # restock-alert offer. The pharmacist is the answer, in one sentence.
        stock = q1(
            """select coalesce(sum(qty_pieces),0) as qty from v_stock_on_hand
                where pharmacy_id = %s and product_id = %s""",
            (pid(), product["id"]),
        )
        if not stock or stock["qty"] <= 0:
            record_stockout(product_query, phone)
        return (f"PRESCRIPTION-ONLY: {product['name']} requires a pharmacist's review. "
                f"Do not discuss availability or restock. Connect them to the "
                f"pharmacist: 'That item requires a pharmacist's review — let me "
                f"connect you with our pharmacist who can guide you properly.'")

    row = q1(
        """select qty_pieces, sell_price, earliest_expiry, pack_size
             from v_stock_on_hand
            where pharmacy_id = %s and product_id = %s""",
        (pid(), product["id"]),
    )
    qty = (row or {}).get("qty_pieces") or 0
    if qty > 0:
        price = kes(row["sell_price"]) if row["sell_price"] else "price at the counter"
        return (f"IN STOCK: {product['name']} — {from_pieces(qty, row['pack_size'])} "
                f"({qty} pcs) @ {price}. Quote it normally and warmly.")

    record_stockout(product_query, phone)
    return (f"OUT OF STOCK: {product['name']} — 0 pcs. A restock request has been "
            f"logged and staff will be alerted once demand adds up. Redirect language: "
            f"acknowledge first, ask what they are treating or how urgent it is, offer "
            f"a suitable in-stock alternative if one exists, offer the pharmacist. "
            f"Only if they want this exact item, offer to alert them when it's back "
            f"(then call notify_me_when_back). NEVER open with 'we don't have it' and "
            f"NEVER promise a restock date.")


def request_restock_alert(product_query: str, phone: str) -> str:
    """The customer said yes to 'alert me when it's back'. Marks their most recent
    un-notified stockout row for that product."""
    product = _match_product(product_query)
    product_id = (product or {}).get("id")
    row = q1(
        """select id from stockout_log
            where pharmacy_id = %s and phone = %s
              and (%s::uuid is null or product_id = %s::uuid)
              and wants_notify = false and notified_at is null
            order by created_at desc limit 1""",
        (pid(), phone, product_id, product_id),
    )
    if not row:
        # No stockout on file (maybe the product was never out). Record the intent
        # anyway so the promise has somewhere to land.
        ex("""insert into stockout_log (pharmacy_id, product_id, product_query, phone,
                                        wants_notify)
              values (%s,%s,%s,%s,true)""",
           (pid(), product_id, (product_query or "")[:200], phone))
        return ("Restock alert noted. Confirm warmly: they'll get a message the moment "
                "it's back. Do NOT give a date or timeframe.")

    ex("update stockout_log set wants_notify = true where id = %s", (row["id"],))
    return ("Restock alert noted. Confirm warmly: they'll get a message the moment "
            "it's back. Do NOT give a date or timeframe.")
