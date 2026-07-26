"""FLOW A — Goods receiving. The wedge.

Two photos of a supplier invoice become a complete GRN with batch numbers and expiry
dates. Nothing enters stock without a named human approving it.
"""
import logging
import uuid
from datetime import date, timedelta

from config import settings
from db import apply_movement, download, ex1, q, q1, tx
from llm import extract_invoice
from state import clear_state, get_state, set_state
from utils import from_pieces, kes, parse_date_loose, parse_expiry, parse_wp, to_pieces
from wa import send_text

log = logging.getLogger(__name__)
PID = settings.PHARMACY_ID


# ------------------------------------------------------------ page collection
def add_page(phone: str, storage_path: str) -> None:
    st = get_state(phone)
    pages = st["context"].get("pages", []) if st["flow"] == "grn_collect" else []
    pages.append(storage_path)
    set_state(phone, "grn_collect", {"pages": pages})
    send_text(
        phone,
        f"Page {len(pages)} received. Send more pages, or reply *DONE* to process.",
    )


# ------------------------------------------------------------ extraction
def process_pages(phone: str, staff: dict) -> None:
    st = get_state(phone)
    pages = st["context"].get("pages", [])
    if not pages:
        send_text(phone, "No invoice pages yet. Send a photo of the supplier invoice first.")
        return

    send_text(phone, f"Reading {len(pages)} page(s)... this takes about 30 seconds.")
    try:
        images = [download(settings.BUCKET_INVOICES, p) for p in pages]
        data = extract_invoice(images)
    except Exception as e:
        log.exception("invoice extraction failed")
        clear_state(phone)
        send_text(phone, f"Could not read that invoice ({type(e).__name__}). "
                         "Try again with better light, or type HELP.")
        return

    grn_id = _persist(data, pages, staff)
    if not grn_id:
        clear_state(phone)
        send_text(phone, "That did not look like a supplier invoice. Nothing was saved.")
        return

    # duplicate guard — two staff receiving the same delivery from two phones
    dup = q1(
        """select g.id, s.name as who, g.approved_at
             from grns g left join staff s on s.id = g.approved_by
            where g.pharmacy_id = %s and g.invoice_no = %s
              and g.status = 'approved' and g.id <> %s
            limit 1""",
        (PID, data.get("invoice_no"), grn_id),
    )
    if dup:
        when = dup["approved_at"].strftime("%d %b %H:%M") if dup["approved_at"] else "earlier"
        send_text(phone, f"Invoice {data.get('invoice_no')} was already received by "
                         f"{dup['who'] or 'someone'} at {when}. Nothing was changed.")
        clear_state(phone)
        return

    set_state(phone, "grn_review", {"grn_id": grn_id})
    send_text(phone, render_summary(grn_id))


def persist_from_paths(pages: list[str], staff: dict) -> str | None:
    """Extract + persist from already-uploaded pages, with no WhatsApp involved.

    The dashboard's manual-upload fallback needs the SAME pipeline as the photo path,
    not a parallel one — a second extraction path would drift and you would end up
    with invoices that receive correctly over WhatsApp but not from the desk. Returns
    the GRN id for review, or None if it did not look like an invoice.
    """
    if not pages:
        return None
    images = [download(settings.BUCKET_INVOICES, p) for p in pages]
    data = extract_invoice(images)
    return _persist(data, pages, staff)


def _persist(data: dict, pages: list[str], staff: dict) -> str | None:
    lines = data.get("lines") or []
    if not lines:
        return None

    supplier_id = _match_supplier(data.get("supplier_name"))
    parsed_total = sum(float(l.get("line_total") or 0) for l in lines)

    grn = ex1(
        """insert into grns (pharmacy_id, supplier_id, invoice_no, invoice_date, po_ref,
                             subtotal, vat_total, net_total, parsed_total,
                             status, images, raw_extract, model)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'needs_review',%s,%s,%s)
           returning id""",
        (PID, supplier_id, data.get("invoice_no"),
         parse_date_loose(data.get("invoice_date")), data.get("po_ref"),
         data.get("printed_subtotal"), data.get("printed_vat"), data.get("printed_net"),
         parsed_total, __import__("json").dumps(pages),
         __import__("json").dumps(data), settings.MODEL_VISION),
    )
    grn_id = grn["id"]

    printed_net = data.get("printed_net")
    total_mismatch = (
        printed_net is not None and abs(float(printed_net) - parsed_total) > 1.0
    )

    for i, l in enumerate(lines, start=1):
        product, score = match_product(l.get("code"), l.get("description"))
        pack_size = (product or {}).get("pack_size", 1)
        pieces = to_pieces(l.get("qty_whole"), l.get("qty_pieces"), pack_size)
        expiry = parse_expiry(l.get("expiry_date") or l.get("expiry_raw"))

        flags = []
        if not product:
            flags.append("unmatched_product")
        if not expiry:
            flags.append("missing_expiry")
        if not l.get("batch_no"):
            flags.append("missing_batch")
        if float(l.get("confidence") or 0) < settings.LINE_CONF_THRESHOLD:
            flags.append("low_confidence")
        if expiry and expiry < date.today() + timedelta(days=180):
            flags.append("short_dated")
        if total_mismatch:
            flags.append("total_mismatch")

        ex1(
            """insert into grn_lines (grn_id, line_no, raw_code, raw_description,
                                      product_id, match_score, batch_no, expiry_date,
                                      qty_invoiced_pieces, unit_price, line_total,
                                      confidence, flags)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (grn_id, l.get("line_no") or i, l.get("code"), l.get("description"),
             (product or {}).get("id"), score, l.get("batch_no"), expiry, pieces,
             l.get("unit_price"), l.get("line_total"), l.get("confidence"), flags),
        )
    return grn_id


# ------------------------------------------------------------ matching
def match_product(code: str | None, description: str | None):
    """Exact legacy code first, then trigram similarity on the name."""
    if code:
        row = q1(
            "select id, name, pack_size from products where pharmacy_id=%s and legacy_code=%s",
            (PID, code.strip()),
        )
        if row:
            return row, 1.0
    if not description:
        return None, None
    row = q1(
        """select id, name, pack_size, similarity(name, %s) as score
             from products
            where pharmacy_id = %s and similarity(name, %s) > %s
            order by score desc limit 1""",
        (description, PID, description, settings.MATCH_THRESHOLD),
    )
    return (row, float(row["score"])) if row else (None, None)


def _match_supplier(name: str | None) -> str | None:
    if not name:
        return None
    row = q1(
        """select id from suppliers
            where pharmacy_id=%s and similarity(name, %s) > 0.4
            order by similarity(name, %s) desc limit 1""",
        (PID, name, name),
    )
    if row:
        return row["id"]
    row = ex1(
        "insert into suppliers (pharmacy_id, name) values (%s,%s) returning id",
        (PID, name.strip()[:120]),
    )
    return row["id"]


# ------------------------------------------------------------ summary
def render_summary(grn_id: str) -> str:
    g = q1(
        """select g.*, s.name as supplier
             from grns g left join suppliers s on s.id = g.supplier_id
            where g.id = %s""",
        (grn_id,),
    )
    lines = q(
        """select l.*, p.name as product_name, p.pack_size
             from grn_lines l left join products p on p.id = l.product_id
            where l.grn_id = %s order by l.line_no""",
        (grn_id,),
    )

    head = f"*{(g['supplier'] or 'Unknown supplier').upper()}*"
    if g["invoice_no"]:
        head += f" · {g['invoice_no']}"
    if g["invoice_date"]:
        head += f" · {g['invoice_date'].strftime('%d/%m/%Y')}"

    net = g["net_total"]
    parsed = g["parsed_total"] or 0
    if net is None:
        totals = f"{len(lines)} lines · {kes(parsed)} (no printed total found)"
    elif abs(float(net) - float(parsed)) <= 1.0:
        totals = f"{len(lines)} lines · {kes(net)} ✅ matches invoice total"
    else:
        totals = (f"{len(lines)} lines · our sum {kes(parsed)} vs printed {kes(net)} "
                  f"⚠️ difference {kes(abs(float(net) - float(parsed)))}")

    problems, short = [], []
    for l in lines:
        f = set(l["flags"] or [])
        if "unmatched_product" in f:
            problems.append(f"{l['line_no']}. {l['raw_description']} — not in your product "
                            f"list. Reply *{l['line_no']} NEW* to add it.")
        elif "missing_expiry" in f:
            problems.append(f"{l['line_no']}. {l['raw_description']} — expiry unreadable. "
                            f"Reply *{l['line_no']} EXP 06/2028*.")
        elif "missing_batch" in f:
            problems.append(f"{l['line_no']}. {l['raw_description']} — batch unreadable. "
                            f"Reply *{l['line_no']} BATCH ST26-0439*.")
        elif "low_confidence" in f:
            problems.append(f"{l['line_no']}. {l['raw_description']} — please check, "
                            f"reading was unclear.")
        if "short_dated" in f and l["expiry_date"]:
            short.append(f"{l['line_no']}. {l['raw_description']} — expires "
                         f"{l['expiry_date'].strftime('%b %Y')}")

    parts = [head, totals]
    if short:
        parts.append("🔶 *Short-dated — refuse or negotiate now:*\n" + "\n".join(short[:6]))
    if problems:
        parts.append(f"⚠️ *{len(problems)} line(s) need you:*\n" + "\n".join(problems[:8]))
    else:
        parts.append("✅ All lines matched with batch and expiry.")

    parts.append(
        "Physical count different anywhere? Reply like *5:2W* (line 5, you counted 2 packs).\n"
        "Reply *OK* to receive all lines into stock, or *CANCEL* to discard."
    )
    return "\n\n".join(parts)


# ------------------------------------------------------------ review replies
def handle_review(phone: str, staff: dict, text: str) -> None:
    st = get_state(phone)
    grn_id = st["context"].get("grn_id")
    if not grn_id:
        clear_state(phone)
        send_text(phone, "That review session expired. Please send the invoice again.")
        return

    t = text.strip()
    up = t.upper()

    if up in ("CANCEL", "STOP"):
        ex1("update grns set status='rejected' where id=%s returning id", (grn_id,))
        clear_state(phone)
        send_text(phone, "Discarded. No stock was changed.")
        return

    if up == "OK":
        approve(grn_id, staff, phone)
        return

    # "5:2W"  -> physical count correction
    if ":" in t:
        left, right = t.split(":", 1)
        if left.strip().isdigit():
            wp = parse_wp(right)
            if wp:
                _set_counted(grn_id, int(left.strip()), wp)
                send_text(phone, f"Line {left.strip()} counted as {right.strip()}. "
                                 "More corrections, or reply *OK*.")
                return

    # "<line> EXP 06/2028" | "<line> BATCH X" | "<line> NEW"
    parts = t.split(None, 2)
    if parts and parts[0].isdigit():
        line_no = int(parts[0])
        kind = parts[1].upper() if len(parts) > 1 else ""
        val = parts[2].strip() if len(parts) > 2 else ""

        if kind == "EXP" and val:
            d = parse_expiry(val)
            if not d:
                send_text(phone, "Could not read that date. Use format *06/2028*.")
                return
            _update_line(grn_id, line_no, "expiry_date", d, "missing_expiry")
            send_text(phone, f"Line {line_no} expiry set to {d.strftime('%b %Y')}. "
                             "More corrections, or reply *OK*.")
            return

        if kind == "BATCH" and val:
            _update_line(grn_id, line_no, "batch_no", val, "missing_batch")
            send_text(phone, f"Line {line_no} batch set to {val}. "
                             "More corrections, or reply *OK*.")
            return

        if kind == "NEW":
            name = _create_product_from_line(grn_id, line_no)
            if name:
                send_text(phone, f"Added *{name}* to your product list and linked line "
                                 f"{line_no}. More corrections, or reply *OK*.")
            else:
                send_text(phone, f"Could not find line {line_no}.")
            return

    send_text(phone,
              "I did not understand that. Options:\n"
              "• *OK* — receive into stock\n"
              "• *5:2W* — line 5, physical count 2 packs\n"
              "• *7 EXP 06/2028* — fix an expiry\n"
              "• *7 BATCH ST26-0439* — fix a batch number\n"
              "• *7 NEW* — add as a new product\n"
              "• *CANCEL* — discard")


def _set_counted(grn_id: str, line_no: int, wp: tuple[int, int]) -> None:
    row = q1(
        """select l.id, coalesce(p.pack_size,1) as pack_size
             from grn_lines l left join products p on p.id = l.product_id
            where l.grn_id=%s and l.line_no=%s""",
        (grn_id, line_no),
    )
    if not row:
        return
    pieces = to_pieces(wp[0], wp[1], row["pack_size"])
    ex1(
        """update grn_lines
              set qty_counted_pieces = %s,
                  flags = case when %s <> qty_invoiced_pieces
                               then array_append(array_remove(flags,'short_delivery'),'short_delivery')
                               else array_remove(flags,'short_delivery') end
            where id = %s returning id""",
        (pieces, pieces, row["id"]),
    )


def _update_line(grn_id: str, line_no: int, col: str, value, drop_flag: str) -> None:
    assert col in ("expiry_date", "batch_no")   # never interpolate user input as a column
    ex1(
        f"""update grn_lines
               set {col} = %s, flags = array_remove(flags, %s)
             where grn_id = %s and line_no = %s returning id""",
        (value, drop_flag, grn_id, line_no),
    )


def _create_product_from_line(grn_id: str, line_no: int) -> str | None:
    l = q1("select * from grn_lines where grn_id=%s and line_no=%s", (grn_id, line_no))
    if not l:
        return None
    name = (l["raw_description"] or "Unknown item").strip()[:200]
    pack = _guess_pack_size(name)
    p = ex1(
        """insert into products (pharmacy_id, legacy_code, name, pack_size, cost_price)
           values (%s,%s,%s,%s,%s)
           on conflict (pharmacy_id, legacy_code) do update set name = excluded.name
           returning id, pack_size""",
        (PID, l["raw_code"] or f"NEW-{uuid.uuid4().hex[:6]}", name, pack, l["unit_price"]),
    )
    pieces = to_pieces(None, l["qty_invoiced_pieces"], 1) or l["qty_invoiced_pieces"]
    ex1(
        """update grn_lines
              set product_id=%s, match_score=1.0,
                  qty_invoiced_pieces = %s,
                  flags = array_remove(flags,'unmatched_product')
            where id=%s returning id""",
        (p["id"], pieces, l["id"]),
    )
    return name


def _guess_pack_size(name: str) -> int:
    """'PRENOR 25/5MG TABS 30S' -> 30. Falls back to 1, which is always safe."""
    import re
    m = re.search(r"(\d{1,4})\s*[sS]\b", name)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 1000:
            return n
    return 1


# ------------------------------------------------------------ approval
def approve(grn_id: str, staff: dict, phone: str) -> None:
    lines = q(
        """select l.*, p.pack_size, p.name as product_name
             from grn_lines l left join products p on p.id = l.product_id
            where l.grn_id = %s order by l.line_no""",
        (grn_id,),
    )
    unmatched = [l for l in lines if not l["product_id"]]
    if unmatched:
        nums = ", ".join(str(l["line_no"]) for l in unmatched[:8])
        send_text(phone, f"Cannot receive yet — line(s) {nums} are not linked to a product. "
                         f"Reply *{unmatched[0]['line_no']} NEW* to add, for each.")
        return

    g = q1("select * from grns where id=%s", (grn_id,))
    if g["status"] == "approved":
        send_text(phone, "This invoice was already received.")
        clear_state(phone)
        return

    received, short_lines, no_expiry = 0, [], 0
    with tx() as cur:
        for l in lines:
            qty = l["qty_counted_pieces"] if l["qty_counted_pieces"] is not None \
                else l["qty_invoiced_pieces"]
            if not qty:
                continue
            if l["qty_counted_pieces"] is not None and \
               l["qty_counted_pieces"] != l["qty_invoiced_pieces"]:
                short_lines.append(
                    f"{l['line_no']}. {l['product_name']}: invoiced "
                    f"{from_pieces(l['qty_invoiced_pieces'], l['pack_size'])}, counted "
                    f"{from_pieces(l['qty_counted_pieces'], l['pack_size'])}"
                )
            if not l["expiry_date"]:
                no_expiry += 1

            cur.execute(
                """insert into batches (pharmacy_id, product_id, batch_no, expiry_date,
                                        qty_pieces, cost_price, grn_id, source_image,
                                        confidence, verified_by, verified_at)
                   values (%s,%s,%s,%s,0,%s,%s,%s,%s,%s, now())
                   on conflict (pharmacy_id, product_id, batch_no, expiry_date)
                     do update set verified_by = excluded.verified_by,
                                   verified_at = now()
                   returning id""",
                (PID, l["product_id"], l["batch_no"], l["expiry_date"],
                 l["unit_price"], grn_id,
                 (g["images"][0] if g["images"] else None),
                 l["confidence"], staff["id"]),
            )
            batch_id = cur.fetchone()["id"]
            apply_movement(cur, batch_id, int(qty), "grn",
                           actor_staff=staff["id"], ref_table="grns", ref_id=grn_id,
                           note=f"line {l['line_no']}")
            received += 1

            if l["unit_price"]:
                cur.execute(
                    "update products set cost_price=%s where id=%s",
                    (l["unit_price"], l["product_id"]),
                )

            # Learn who supplies this product from the invoice that delivered it.
            # `preferred_supplier_id` is read by forecast.reorder_list() and the low-stock
            # digest, and create_draft_pos() DROPS every row where it is null -- so
            # without this the reorder list says "No supplier set", `PO` creates nothing,
            # and no order ever reaches the distributor. The supplier is already known
            # here: _match_supplier() resolved it from the invoice at extraction time.
            # Last supplier who actually delivered it wins, which self-corrects when the
            # pharmacy switches wholesaler. If a deliberate override is ever needed, add
            # a locked flag rather than removing this.
            if g["supplier_id"]:
                cur.execute(
                    "update products set preferred_supplier_id=%s where id=%s",
                    (g["supplier_id"], l["product_id"]),
                )

        cur.execute(
            """update grns set status='approved', approved_by=%s, approved_at=now(),
                               discrepancy_note=%s
                where id=%s""",
            (staff["id"], "; ".join(short_lines) or None, grn_id),
        )

    clear_state(phone)
    msg = [f"✅ Received {received} line(s) into stock. Approved by {staff['name']}."]
    if short_lines:
        msg.append("📋 Discrepancies recorded (claim within 48 hours):\n" +
                   "\n".join(short_lines))
    if no_expiry:
        msg.append(f"⚠️ {no_expiry} line(s) saved without an expiry date. "
                   "Add them on the dashboard when you can.")
    send_text(phone, "\n\n".join(msg))

    owner = q1(
        "select phone from staff where pharmacy_id=%s and role='owner' and is_active limit 1",
        (PID,),
    )
    if owner and owner["phone"] != phone:
        send_text(owner["phone"],
                  f"📦 Stock received: {g['invoice_no'] or 'invoice'} · "
                  f"{kes(g['net_total'] or g['parsed_total'])} · {received} lines · "
                  f"by {staff['name']}"
                  + (f"\n⚠️ {len(short_lines)} count discrepancy(ies)" if short_lines else ""))
