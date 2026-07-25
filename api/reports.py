"""FLOW B — Owner asks anything, from anywhere.

The model chooses a tool and its arguments. OUR CODE owns every query. There is no
path from a WhatsApp message to arbitrary SQL against a live pharmacy database, and
there never should be.
"""
import logging
import uuid
from datetime import date, timedelta

from config import settings
from db import q, q1, upload, signed_url
from pdfgen import Doc, bar_chart, line_chart, qr_png
from utils import from_pieces, kes

log = logging.getLogger(__name__)
PID = settings.PHARMACY_ID


# ============================================================ tool schemas
TOOLS = [
    {
        "name": "get_stock",
        "description": "Current stock on hand for a product, or the lowest-stock products. "
                       "Use when asked 'do we have X', 'how much X is left', 'what is running out'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_query": {"type": "string",
                                  "description": "Drug name fragment. Omit to list low stock."},
                "low_stock_only": {"type": "boolean"},
                "limit": {"type": "integer", "default": 15},
            },
        },
    },
    {
        "name": "get_expiry_risk",
        "description": "Batches expiring within N days, with value at risk. Use for "
                       "'what is expiring', 'expiry report', 'dead stock'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 90},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "get_sales_summary",
        "description": "Sales figures for a period. Use for 'how did we do today', "
                       "'sales this week', 'revenue for july'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "yesterday", "week",
                                                      "month", "custom"]},
                "start": {"type": "string", "description": "YYYY-MM-DD if period=custom"},
                "end": {"type": "string", "description": "YYYY-MM-DD if period=custom"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "get_top_products",
        "description": "Best-selling products over the last N days by units or value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 30},
                "limit": {"type": "integer", "default": 10},
                "by": {"type": "string", "enum": ["units", "value"], "default": "value"},
            },
        },
    },
    {
        "name": "find_supplier",
        "description": "Look up a supplier's phone number and rep, by supplier name OR by "
                       "which supplier stocks a given drug. Use for 'who supplies X', "
                       "'MedTrack number'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_name": {"type": "string"},
                "product_query": {"type": "string"},
            },
        },
    },
    {
        "name": "get_reorder_suggestions",
        "description": "Products below reorder level, with days of cover based on recent "
                       "sales velocity. Use for 'what should I order'.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "generate_report_pdf",
        "description": "Build a full PDF report with charts and send it as a WhatsApp "
                       "document. Use ONLY when the user explicitly asks for a report, "
                       "a PDF, or a summary document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "week", "month"],
                           "default": "month"},
            },
        },
    },
]


# ============================================================ tool impls
def _period_bounds(period: str, start: str | None = None, end: str | None = None):
    today = date.today()
    if period == "today":
        return today, today
    if period == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if period == "week":
        return today - timedelta(days=7), today
    if period == "month":
        return today.replace(day=1), today
    if period == "custom" and start:
        return (date.fromisoformat(start),
                date.fromisoformat(end) if end else today)
    return today - timedelta(days=30), today


def get_stock(product_query: str | None = None, low_stock_only: bool = False,
              limit: int = 15) -> str:
    if product_query:
        rows = q(
            """select name, legacy_code, pack_size, qty_pieces, earliest_expiry, sell_price
                 from v_stock_on_hand
                where pharmacy_id = %s and (name ilike %s or similarity(name,%s) > 0.3)
                order by similarity(name,%s) desc limit %s""",
            (PID, f"%{product_query}%", product_query, product_query, limit),
        )
        if not rows:
            return f"No product matching '{product_query}'."
        out = []
        for r in rows:
            exp = f", earliest expiry {r['earliest_expiry']:%b %Y}" if r["earliest_expiry"] else ""
            out.append(f"• {r['name']} — {from_pieces(r['qty_pieces'], r['pack_size'])} "
                       f"({r['qty_pieces']} pcs) @ {kes(r['sell_price'])}{exp}")
        return "\n".join(out)

    rows = q(
        """select name, pack_size, qty_pieces, reorder_level_pieces
             from v_stock_on_hand
            where pharmacy_id = %s and qty_pieces <= greatest(reorder_level_pieces, 0)
            order by qty_pieces asc limit %s""",
        (PID, limit),
    )
    if not rows:
        return "Nothing is below its reorder level."
    return "\n".join(
        f"• {r['name']} — {from_pieces(r['qty_pieces'], r['pack_size'])} left "
        f"(reorder at {from_pieces(r['reorder_level_pieces'], r['pack_size'])})"
        for r in rows
    )


def get_expiry_risk(days: int = 90, limit: int = 20) -> str:
    rows = q(
        """select name, batch_no, expiry_date, qty_pieces, value_at_risk, days_left
             from v_expiry_risk
            where pharmacy_id = %s and days_left <= %s
            order by expiry_date limit %s""",
        (PID, days, limit),
    )
    if not rows:
        return f"Nothing expiring in the next {days} days."
    total = sum(float(r["value_at_risk"] or 0) for r in rows)
    head = f"{len(rows)} batch(es) · {kes(total)} at risk within {days} days"
    body = "\n".join(
        f"• {r['name']} — batch {r['batch_no'] or '?'} — {r['expiry_date']:%b %Y} "
        f"({r['days_left']}d) — {r['qty_pieces']} pcs — {kes(r['value_at_risk'])}"
        for r in rows
    )
    return f"{head}\n{body}"


def get_sales_summary(period: str = "today", start: str | None = None,
                      end: str | None = None) -> str:
    s, e = _period_bounds(period, start, end)
    row = q1(
        """select count(distinct o.id) as orders,
                  coalesce(sum(o.total),0) as revenue,
                  count(distinct o.customer_id) as customers
             from orders o
            where o.pharmacy_id = %s and o.status in ('paid','packed','dispatched','delivered')
              and o.created_at::date between %s and %s""",
        (PID, s, e),
    )
    units = q1(
        """select coalesce(-sum(m.delta_pieces),0) as pieces
             from stock_movements m
            where m.pharmacy_id = %s and m.reason='sale'
              and m.created_at::date between %s and %s""",
        (PID, s, e),
    )
    return (f"{s:%d %b} – {e:%d %b %Y}\n"
            f"• Revenue: {kes(row['revenue'])}\n"
            f"• Orders: {row['orders']}\n"
            f"• Customers served: {row['customers']}\n"
            f"• Units dispensed: {units['pieces']}")


def get_top_products(days: int = 30, limit: int = 10, by: str = "value") -> str:
    order_col = "value" if by == "value" else "pieces"
    rows = q(
        f"""select p.name,
                   -sum(m.delta_pieces) as pieces,
                   -sum(m.delta_pieces) * coalesce(p.sell_price,0) as value
              from stock_movements m
              join batches b on b.id = m.batch_id
              join products p on p.id = b.product_id
             where m.pharmacy_id = %s and m.reason='sale'
               and m.created_at > now() - (%s || ' days')::interval
             group by p.id, p.name
             order by {order_col} desc limit %s""",
        (PID, str(days), limit),
    )
    if not rows:
        return f"No sales recorded in the last {days} days."
    return "\n".join(
        f"{i}. {r['name']} — {r['pieces']} pcs — {kes(r['value'])}"
        for i, r in enumerate(rows, 1)
    )


def find_supplier(supplier_name: str | None = None,
                  product_query: str | None = None) -> str:
    if supplier_name:
        rows = q(
            """select name, phone, alt_phone, rep_name, email, mpesa_paybill
                 from suppliers
                where pharmacy_id=%s and (name ilike %s or similarity(name,%s) > 0.3)
                order by similarity(name,%s) desc limit 5""",
            (PID, f"%{supplier_name}%", supplier_name, supplier_name),
        )
    elif product_query:
        rows = q(
            """select distinct s.name, s.phone, s.alt_phone, s.rep_name, s.email,
                               s.mpesa_paybill
                 from grn_lines l
                 join grns g on g.id = l.grn_id
                 join suppliers s on s.id = g.supplier_id
                 join products p on p.id = l.product_id
                where g.pharmacy_id = %s
                  and (p.name ilike %s or similarity(p.name,%s) > 0.3)
                limit 5""",
            (PID, f"%{product_query}%", product_query),
        )
    else:
        rows = q("select name, phone, rep_name from suppliers where pharmacy_id=%s "
                 "order by name limit 25", (PID,))
    if not rows:
        return "No supplier found for that."
    out = []
    for r in rows:
        line = f"• *{r['name']}*"
        if r.get("phone"):
            line += f"\n  {r['phone']}"
        if r.get("alt_phone"):
            line += f" / {r['alt_phone']}"
        if r.get("rep_name"):
            line += f"\n  Rep: {r['rep_name']}"
        if r.get("mpesa_paybill"):
            line += f"\n  Paybill: {r['mpesa_paybill']}"
        out.append(line)
    return "\n".join(out)


def get_reorder_suggestions(limit: int = 20) -> str:
    rows = q(
        """select s.name, s.pack_size, s.qty_pieces, s.reorder_level_pieces,
                  coalesce(v.avg_daily, 0) as avg_daily,
                  sup.name as supplier, sup.phone as supplier_phone
             from v_stock_on_hand s
             left join v_velocity_90d v on v.product_id = s.product_id
             left join products p on p.id = s.product_id
             left join suppliers sup on sup.id = p.preferred_supplier_id
            where s.pharmacy_id = %s
              and s.qty_pieces <= greatest(s.reorder_level_pieces, coalesce(v.avg_daily,0) * 14)
            order by case when coalesce(v.avg_daily,0) > 0
                          then s.qty_pieces / v.avg_daily else 9999 end asc
            limit %s""",
        (PID, limit),
    )
    if not rows:
        return "Nothing needs reordering right now."
    out = []
    for r in rows:
        cover = (f"{r['qty_pieces'] / r['avg_daily']:.0f}d cover"
                 if r["avg_daily"] else "no recent sales")
        sup = f" · {r['supplier']}" if r["supplier"] else ""
        out.append(f"• {r['name']} — {from_pieces(r['qty_pieces'], r['pack_size'])} left, "
                   f"{cover}{sup}")
    return "\n".join(out)


# ============================================================ report PDF
def build_report_pdf(period: str = "month") -> tuple[str, str]:
    """Returns (storage_path, filename)."""
    s, e = _period_bounds(period)
    ph = q1("select name from pharmacies where id=%s", (PID,))
    title = {"today": "Daily Report", "week": "Weekly Report",
             "month": "Monthly Report"}.get(period, "Report")
    doc = Doc(ph["name"] if ph else "Pharmacy", f"{title} · {s:%d %b} – {e:%d %b %Y}")
    doc.add_page()

    # --- KPIs
    fin = q1(
        """select count(distinct o.id) as orders, coalesce(sum(o.total),0) as revenue,
                  count(distinct o.customer_id) as customers
             from orders o
            where o.pharmacy_id=%s and o.status in ('paid','packed','dispatched','delivered')
              and o.created_at::date between %s and %s""",
        (PID, s, e),
    )
    exp = q1(
        """select count(*) as n, coalesce(sum(value_at_risk),0) as v
             from v_expiry_risk where pharmacy_id=%s and days_left <= 90""",
        (PID,),
    )
    stockval = q1(
        """select coalesce(sum(b.qty_pieces * coalesce(p.cost_price,0)),0) as v
             from batches b join products p on p.id=b.product_id
            where b.pharmacy_id=%s and b.qty_pieces > 0""",
        (PID,),
    )
    doc.kpis([
        ("Revenue", kes(fin["revenue"])),
        ("Orders", str(fin["orders"])),
        ("Stock value", kes(stockval["v"])),
        ("Expiry risk 90d", kes(exp["v"])),
    ])

    # --- revenue trend
    trend = q(
        """select to_char(d.day,'DD Mon') as label,
                  coalesce(sum(o.total),0) as revenue
             from generate_series(%s::date, %s::date, '1 day') d(day)
             left join orders o on o.created_at::date = d.day
                   and o.pharmacy_id = %s
                   and o.status in ('paid','packed','dispatched','delivered')
            group by d.day order by d.day""",
        (s, e, PID),
    )
    if trend and any(float(t["revenue"]) for t in trend):
        doc.h2("Revenue trend")
        doc.image_bytes(line_chart([t["label"] for t in trend],
                                   {"Revenue": [float(t["revenue"]) for t in trend]},
                                   "Daily revenue (KES)"))

    # --- top products
    top = q(
        """select p.name, -sum(m.delta_pieces) as pieces,
                  -sum(m.delta_pieces) * coalesce(p.sell_price,0) as value
             from stock_movements m
             join batches b on b.id=m.batch_id join products p on p.id=b.product_id
            where m.pharmacy_id=%s and m.reason='sale'
              and m.created_at::date between %s and %s
            group by p.id, p.name order by value desc limit 10""",
        (PID, s, e),
    )
    if top:
        doc.h2("Top 10 products")
        doc.image_bytes(bar_chart([t["name"][:18] for t in top],
                                  [float(t["value"]) for t in top],
                                  "Revenue by product (KES)"))
        doc.table(["Product", "Units", "Value"],
                  [[t["name"], t["pieces"], kes(t["value"])] for t in top],
                  [110, 30, 40], ["L", "R", "R"])

    # --- expiry
    exp_rows = q(
        """select name, batch_no, expiry_date, qty_pieces, value_at_risk, days_left
             from v_expiry_risk where pharmacy_id=%s and days_left <= 120
            order by expiry_date limit 25""",
        (PID,),
    )
    if exp_rows:
        doc.add_page()
        doc.h2("Expiry watchlist — act on these")
        doc.table(["Product", "Batch", "Expires", "Qty", "Value at risk"],
                  [[r["name"], r["batch_no"] or "-", f"{r['expiry_date']:%b %Y}",
                    r["qty_pieces"], kes(r["value_at_risk"])] for r in exp_rows],
                  [72, 32, 24, 20, 32], ["L", "L", "L", "R", "R"])

    # --- receiving activity
    grns = q(
        """select g.invoice_no, s.name as supplier, g.invoice_date, g.net_total,
                  g.discrepancy_note, st.name as approved_by
             from grns g
             left join suppliers s on s.id=g.supplier_id
             left join staff st on st.id=g.approved_by
            where g.pharmacy_id=%s and g.status='approved'
              and g.approved_at::date between %s and %s
            order by g.approved_at desc limit 20""",
        (PID, s, e),
    )
    if grns:
        doc.h2("Deliveries received")
        doc.table(["Invoice", "Supplier", "Date", "Value", "Received by", "Discrepancy"],
                  [[g["invoice_no"] or "-", g["supplier"] or "-",
                    f"{g['invoice_date']:%d/%m/%y}" if g["invoice_date"] else "-",
                    kes(g["net_total"]), g["approved_by"] or "-",
                    "YES" if g["discrepancy_note"] else "-"] for g in grns],
                  [30, 48, 20, 30, 30, 22], ["L", "L", "L", "R", "L", "C"])

    # --- dead stock
    dead = q(
        """select s.name, s.qty_pieces, s.pack_size
             from v_stock_on_hand s
             left join v_velocity_90d v on v.product_id = s.product_id
            where s.pharmacy_id=%s and s.qty_pieces > 0 and v.product_id is null
            order by s.qty_pieces desc limit 15""",
        (PID,),
    )
    if dead:
        doc.h2("No sales in 90 days — cash sitting on the shelf")
        doc.table(["Product", "On hand"],
                  [[d["name"], from_pieces(d["qty_pieces"], d["pack_size"])] for d in dead],
                  [140, 40], ["L", "R"])

    data = bytes(doc.output())
    fname = f"dishii-{period}-{date.today():%Y-%m-%d}.pdf"
    path = f"reports/{uuid.uuid4().hex[:8]}/{fname}"
    upload(settings.BUCKET_DOCS, path, data, "application/pdf")
    return path, fname


def build_receipt_pdf(order_id: str) -> tuple[str, str]:
    o = q1(
        """select o.*, c.name as customer_name, c.phone as customer_phone
             from orders o join customers c on c.id=o.customer_id where o.id=%s""",
        (order_id,),
    )
    lines = q(
        """select p.name, p.pack_size, l.qty_pieces, l.unit_price, l.line_total,
                  b.batch_no, b.expiry_date
             from order_lines l
             join products p on p.id=l.product_id
             left join batches b on b.id=l.batch_id
            where l.order_id=%s""",
        (order_id,),
    )
    ph = q1("select name, mpesa_paybill from pharmacies where id=%s", (PID,))

    doc = Doc(ph["name"] if ph else "Pharmacy", f"Receipt · Order {str(order_id)[:8].upper()}")
    doc.add_page()
    doc.set_font("Helvetica", "", 10)
    doc.cell(0, 6, f"Customer: {o['customer_name'] or o['customer_phone']}",
             new_x="LMARGIN", new_y="NEXT")
    doc.cell(0, 6, f"Date: {o['created_at']:%d %b %Y %H:%M}", new_x="LMARGIN", new_y="NEXT")
    doc.ln(3)

    doc.table(["Item", "Batch", "Expiry", "Qty", "Price", "Total"],
              [[l["name"], l["batch_no"] or "-",
                f"{l['expiry_date']:%m/%Y}" if l["expiry_date"] else "-",
                from_pieces(l["qty_pieces"], l["pack_size"]),
                kes(l["unit_price"]), kes(l["line_total"])] for l in lines],
              [62, 28, 20, 22, 24, 24], ["L", "L", "L", "R", "R", "R"])

    doc.set_font("Helvetica", "B", 11)
    doc.cell(0, 7, f"TOTAL  {kes(o['total'])}", align="R", new_x="LMARGIN", new_y="NEXT")

    # QR -> traceability page showing batch numbers dispensed
    if o.get("qr_token"):
        doc.ln(4)
        doc.set_font("Helvetica", "", 8)
        doc.cell(0, 5, "Scan to verify this order and its batch numbers:",
                 new_x="LMARGIN", new_y="NEXT")
        doc.image_bytes(qr_png(f"{settings.PUBLIC_BASE_URL}/verify/{o['qr_token']}"), w=30)

    data = bytes(doc.output())
    fname = f"receipt-{str(order_id)[:8]}.pdf"
    path = f"receipts/{order_id}/{fname}"
    upload(settings.BUCKET_DOCS, path, data, "application/pdf")
    return path, fname


# ============================================================ dispatcher
TOOL_IMPLS = {
    "get_stock": get_stock,
    "get_expiry_risk": get_expiry_risk,
    "get_sales_summary": get_sales_summary,
    "get_top_products": get_top_products,
    "find_supplier": find_supplier,
    "get_reorder_suggestions": get_reorder_suggestions,
}


def run_tool(name: str, args: dict, phone: str) -> str:
    """Execute a tool. generate_report_pdf has a side effect (sends a document)."""
    if name == "generate_report_pdf":
        from wa import send_document
        path, fname = build_report_pdf(args.get("period", "month"))
        url = signed_url(settings.BUCKET_DOCS, path, 86400)
        send_document(phone, url, fname, "Your Dishii report")
        return "Report PDF generated and sent to the user as a WhatsApp document."
    fn = TOOL_IMPLS.get(name)
    if not fn:
        return f"Unknown tool {name}"
    try:
        return fn(**args)
    except Exception as e:
        log.exception("tool %s failed", name)
        return f"Tool error: {type(e).__name__}: {e}"
