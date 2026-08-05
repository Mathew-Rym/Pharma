"""FLOW A — Goods receiving. The wedge.

Two photos of a supplier invoice become a complete GRN with batch numbers and expiry
dates. Nothing enters stock without a named human approving it.
"""
import logging
import uuid
from datetime import date, timedelta

from config import settings
from db import apply_movement, download, ex, ex1, q, q1, tx
from llm import count_delivery, extract_invoice
from state import clear_state, get_state, set_state
from utils import from_pieces, kes, parse_date_loose, parse_expiry, parse_wp, to_pieces
from wa import reply_text

log = logging.getLogger(__name__)
from tenancy import pid          # tenant comes from the request, not from .env


# ------------------------------------------------------------ page collection
def add_page(phone: str, storage_path: str) -> None:
    st = get_state(phone)
    pages = st["context"].get("pages", []) if st["flow"] == "grn_collect" else []
    pages.append(storage_path)
    set_state(phone, "grn_collect", {"pages": pages})
    reply_text(
        phone,
        f"Page {len(pages)} received. Send more pages, or reply *DONE* to process.",
    )


# ------------------------------------------------------------ extraction
def process_pages(phone: str, staff: dict) -> None:
    st = get_state(phone)
    pages = st["context"].get("pages", [])
    if not pages:
        reply_text(phone, "No invoice pages yet. Send a photo of the supplier invoice first.")
        return

    reply_text(phone, f"Reading {len(pages)} page(s)... this takes about 30 seconds.")
    try:
        images = [download(settings.BUCKET_INVOICES, p) for p in pages]
        data = extract_invoice(images)
    except Exception as e:
        log.exception("invoice extraction failed")
        clear_state(phone)
        reply_text(phone, f"Could not read that invoice ({type(e).__name__}). "
                         "Try again with better light, or type HELP.")
        return

    grn_id = _persist(data, pages, staff)
    if not grn_id:
        clear_state(phone)
        reply_text(phone, "That did not look like a supplier invoice. Nothing was saved.")
        return

    # duplicate guard — two staff receiving the same delivery from two phones
    dup = q1(
        """select g.id, s.name as who, g.approved_at
             from grns g left join staff s on s.id = g.approved_by
            where g.pharmacy_id = %s and g.invoice_no = %s
              and g.status = 'approved' and g.id <> %s
            limit 1""",
        (pid(), data.get("invoice_no"), grn_id),
    )
    if dup:
        when = dup["approved_at"].strftime("%d %b %H:%M") if dup["approved_at"] else "earlier"
        reply_text(phone, f"Invoice {data.get('invoice_no')} was already received by "
                         f"{dup['who'] or 'someone'} at {when}. Nothing was changed.")
        clear_state(phone)
        return

    # ---- physical verification gate -------------------------------------------
    # The invoice tells us what the supplier BILLED. It does not tell us what arrived.
    # Ask for the goods before showing the approve prompt, because once staff see a
    # tidy summary with an OK button they will press it.
    ex1("update grns set status='awaiting_count' where id=%s returning id", (grn_id,))
    set_state(phone, "grn_goods", {"grn_id": grn_id, "goods": []})
    n = len(q("select 1 from grn_lines where grn_id=%s", (grn_id,)))
    reply_text(phone,
              f"Read {n} line(s) from invoice {data.get('invoice_no') or '—'}.\n\n"
              f"📦 *Now photograph the goods.*\n"
              f"Lay the packs out so they are all visible — flat on the counter beats a "
              f"stack, because I can only count what I can see. Send more than one photo "
              f"if it does not fit in the frame.\n\n"
              f"Reply *SKIP* to receive on the invoice quantities without counting.")


def add_goods_photo(phone: str, storage_path: str) -> None:
    """Collect photos of the delivered goods (state grn_goods)."""
    st = get_state(phone)
    goods = st["context"].get("goods", []) if st["flow"] == "grn_goods" else []
    goods.append(storage_path)
    set_state(phone, "grn_goods",
              {"grn_id": st["context"].get("grn_id"), "goods": goods})
    reply_text(phone, f"Goods photo {len(goods)} received. Send more, or reply *COUNT* "
                     f"to count them.")


def handle_goods_reply(phone: str, staff: dict, text: str) -> bool:
    """Returns True if consumed. SKIP must always work, or receiving can be blocked."""
    st = get_state(phone)
    if st["flow"] != "grn_goods":
        return False
    grn_id = st["context"].get("grn_id")
    goods = st["context"].get("goods", [])
    up = text.strip().upper()

    if up in ("CANCEL", "STOP"):
        ex1("update grns set status='rejected' where id=%s returning id", (grn_id,))
        clear_state(phone)
        reply_text(phone, "Discarded. No stock was changed.")
        return True

    if up == "SKIP":
        # Deliberate escape hatch. A 40-line delivery at 6pm, a flat battery, or a
        # model outage must never stop stock being received — that is the one failure
        # the pharmacy cannot absorb. approve() then falls back to invoice quantities,
        # exactly as it did before this gate existed.
        _to_review(phone, grn_id,
                   "Receiving on invoice quantities — goods were not counted.")
        return True

    if up in ("COUNT", "DONE", "OK") and goods:
        reply_text(phone, f"Counting {len(goods)} photo(s)…")
        try:
            note, needs_more = apply_vision_count(grn_id, goods, return_flag=True)
        except Exception as e:
            log.exception("vision count failed")
            note, needs_more = (f"Could not count the photos ({type(e).__name__}). "
                                f"Check the quantities by hand below."), False

        # When something was cut off or hidden, one more photo is far cheaper than a
        # wrong count -- and much cheaper than the pharmacist hand-counting 40 boxes.
        # Offer it once, then stop asking: a loop that keeps demanding photos is the
        # blocking behaviour SKIP exists to prevent.
        if needs_more and not st["context"].get("recounted"):
            set_state(phone, "grn_goods",
                      {"grn_id": grn_id, "goods": goods, "recounted": True})
            reply_text(phone, note + "\n\n📷 *Send another photo* covering the items "
                                    "above and reply *COUNT* again, or reply *SKIP* to "
                                    "move on and check them by hand.")
            return True

        _to_review(phone, grn_id, note)
        return True

    if up in ("COUNT", "DONE", "OK"):
        reply_text(phone, "Send a photo of the goods first, or reply *SKIP* to receive "
                         "on the invoice quantities.")
        return True

    return False


def _to_review(phone: str, grn_id: str, note: str) -> None:
    ex1("update grns set status='needs_review' where id=%s returning id", (grn_id,))
    set_state(phone, "grn_review", {"grn_id": grn_id})
    reply_text(phone, note + "\n\n" + render_summary(grn_id))


def apply_vision_count(grn_id: str, goods_paths: list[str], return_flag: bool = False):
    """Count the goods photos and record the machine's opinion.

    Returns the pharmacist-facing summary, or (summary, needs_more_photos) when
    `return_flag` is set. `needs_more_photos` is True when at least one line could not
    be counted because something was hidden or out of frame — the one case where
    another photo actually fixes the problem.

    Writes vision_* columns only. It does NOT write qty_counted_pieces: that column
    means a human stands behind the number, and nothing here has been seen by a human
    yet. approve() therefore still receives invoice quantities unless someone confirms
    a different count — the machine can flag a discrepancy, never silently change what
    enters the ledger.
    """
    lines = q("""select l.line_no, l.raw_description, l.qty_invoiced_pieces,
                        coalesce(p.pack_size,1) as pack_size
                   from grn_lines l left join products p on p.id = l.product_id
                  where l.grn_id=%s order by l.line_no""", (grn_id,))
    if not lines:
        return "No lines to count."

    images = [download(settings.BUCKET_INVOICES, p) for p in goods_paths]
    result = count_delivery(images, [dict(l) for l in lines])

    ex("update grns set goods_images=%s where id=%s",
       (__import__("json").dumps(goods_paths), grn_id))

    by_line = {int(i["line_no"]): i for i in (result.get("items") or [])
               if i.get("line_no") is not None}
    agreed, mismatched, unsure = 0, [], []

    for l in lines:
        item = by_line.get(l["line_no"])
        if not item:
            continue
        packs = int(item.get("packs") or 0)
        loose = int(item.get("loose") or 0)
        conf = float(item.get("confidence") or 0)
        ex("""update grn_lines set vision_packs=%s, vision_loose=%s,
                  vision_confidence=%s, vision_note=%s
                where grn_id=%s and line_no=%s""",
           (packs, loose, round(conf, 3), (item.get("note") or "")[:300] or None,
            grn_id, l["line_no"]))

        seen = packs * int(l["pack_size"] or 1) + loose
        invoiced = int(l["qty_invoiced_pieces"] or 0)
        # `fully_visible` matters more than confidence, and they are different things.
        # A model can be perfectly confident it sees 2 packs while 3 more sit out of
        # frame. Reporting that as "invoice says 6, I count 2" is a false shortage --
        # and a few of those teach staff to ignore every count warning, which costs
        # more than never counting at all. Default to False when the key is absent so
        # an older/looser model response degrades to "check by hand".
        fully_visible = bool(item.get("fully_visible", False))
        if conf < 0.75 or not fully_visible:
            unsure.append((l, item))
        elif seen != invoiced:
            mismatched.append((l, item, seen, invoiced))
            _add_flag(grn_id, l["line_no"], "count_mismatch")
        else:
            agreed += 1

    parts = []
    if result.get("photo_quality"):
        parts.append(f"⚠️ Photo {result['photo_quality']} — counts below are unreliable.")
    if agreed:
        parts.append(f"✅ {agreed} line(s) match the invoice.")
    if mismatched:
        parts.append("⚠️ *Counts that do not match the invoice:*\n" + "\n".join(
            f"{l['line_no']}. {(l['raw_description'] or '')[:34]} — invoice "
            f"{from_pieces(inv, l['pack_size'])}, I count "
            f"{from_pieces(seen, l['pack_size'])}"
            + (f" ({item['note']})" if item.get("note") else "")
            for l, item, seen, inv in mismatched))
    if unsure:
        parts.append("🔍 *Could not count confidently* (check these by hand):\n"
                     + "\n".join(
                         f"{l['line_no']}. {(l['raw_description'] or '')[:34]}"
                         + (f" — {item['note']}" if item.get("note") else "")
                         for l, item in unsure))
    if result.get("unlisted_items_seen"):
        parts.append(f"❓ {result['unlisted_items_seen']} product(s) in the photo are "
                     f"not on this invoice.")
    if mismatched or unsure:
        parts.append("_Correct a count with_ *line:packs* _— e.g._ *5:2W* _for 2 packs, "
                     "or_ *5:2W5P* _for 2 packs and 5 loose. The invoice figure is used "
                     "for any line you do not correct._")
    summary = "\n\n".join(parts) or "Counted, nothing to flag."
    return (summary, bool(unsure)) if return_flag else summary


def _add_flag(grn_id: str, line_no: int, flag: str) -> None:
    ex("""update grn_lines
             set flags = (select array_agg(distinct f)
                            from unnest(flags || array[%s]::text[]) f)
           where grn_id=%s and line_no=%s""", (flag, grn_id, line_no))


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
        (pid(), supplier_id, data.get("invoice_no"),
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
            (pid(), code.strip()),
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
        (description, pid(), description, settings.MATCH_THRESHOLD),
    )
    return (row, float(row["score"])) if row else (None, None)


def _match_supplier(name: str | None) -> str | None:
    if not name:
        return None
    row = q1(
        """select id from suppliers
            where pharmacy_id=%s and similarity(name, %s) > 0.4
            order by similarity(name, %s) desc limit 1""",
        (pid(), name, name),
    )
    if row:
        return row["id"]
    row = ex1(
        "insert into suppliers (pharmacy_id, name) values (%s,%s) returning id",
        (pid(), name.strip()[:120]),
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

    # Physical count disagreements survive a re-render of the summary, so they cannot
    # scroll out of the chat and be forgotten before someone presses OK.
    counts = []
    for l in lines:
        if l.get("qty_counted_pieces") is not None and \
           l["qty_counted_pieces"] != l["qty_invoiced_pieces"]:
            counts.append(
                f"{l['line_no']}. {l['product_name'] or l['raw_description']} — invoice "
                f"{from_pieces(l['qty_invoiced_pieces'] or 0, l['pack_size'])}, "
                f"receiving {from_pieces(l['qty_counted_pieces'], l['pack_size'])} "
                f"(your count)")
        elif "count_mismatch" in set(l["flags"] or []) and l.get("vision_packs") is not None:
            seen = (l["vision_packs"] * (l["pack_size"] or 1)
                    + (l["vision_loose"] or 0))
            counts.append(
                f"{l['line_no']}. {l['product_name'] or l['raw_description']} — invoice "
                f"{from_pieces(l['qty_invoiced_pieces'] or 0, l['pack_size'])}, "
                f"I counted {from_pieces(seen, l['pack_size'])} "
                f"— *unconfirmed*, receiving the invoice figure")

    parts = [head, totals]
    if counts:
        parts.append("📦 *Physical count:*\n" + "\n".join(counts[:8]))
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
        reply_text(phone, "That review session expired. Please send the invoice again.")
        return

    t = text.strip()
    up = t.upper()

    if up in ("CANCEL", "STOP"):
        ex1("update grns set status='rejected' where id=%s returning id", (grn_id,))
        clear_state(phone)
        reply_text(phone, "Discarded. No stock was changed.")
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
                reply_text(phone, f"Line {left.strip()} counted as {right.strip()}. "
                                 "More corrections, or reply *OK*.")
                return

    # Checked before the per-line parser, since "ALL NEW" does not start with a digit and
    # would otherwise fall through to the "I did not understand that" branch.
    if _is_all_new(t):
        created = _link_all_unmatched(grn_id)
        still = q("""select line_no from grn_lines where grn_id=%s and product_id is null
                      order by line_no""", (grn_id,))
        if not created and not still:
            reply_text(phone, "Every line is already linked to a product. Reply *OK* to "
                             "receive into stock.")
            return
        msg = (f"Added {len(created)} product(s) to your list and linked them:\n"
               + "\n".join(f"• {n}" for n in created[:12]))
        if len(created) > 12:
            msg += f"\n…and {len(created) - 12} more"
        if still:
            nums = ", ".join(str(r["line_no"]) for r in still[:8])
            msg += (f"\n\n⚠️ Line(s) {nums} had no readable description, so I did not "
                    f"invent a product for them. Fix with *{still[0]['line_no']} NEW* "
                    f"after checking the invoice.")
        else:
            msg += "\n\nReply *OK* to receive into stock."
        reply_text(phone, msg)
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
                reply_text(phone, "Could not read that date. Use format *06/2028*.")
                return
            _update_line(grn_id, line_no, "expiry_date", d, "missing_expiry")
            reply_text(phone, f"Line {line_no} expiry set to {d.strftime('%b %Y')}. "
                             "More corrections, or reply *OK*.")
            return

        if kind == "BATCH" and val:
            _update_line(grn_id, line_no, "batch_no", val, "missing_batch")
            reply_text(phone, f"Line {line_no} batch set to {val}. "
                             "More corrections, or reply *OK*.")
            return

        if kind == "NEW":
            name = _create_product_from_line(grn_id, line_no)
            if name:
                reply_text(phone, f"Added *{name}* to your product list and linked line "
                                 f"{line_no}. More corrections, or reply *OK*.")
            else:
                reply_text(phone, f"Could not find line {line_no}.")
            return

    reply_text(phone,
              "I did not understand that. Options:\n"
              "• *OK* — receive into stock\n"
              "• *5:2W* — line 5, physical count 2 packs\n"
              "• *7 EXP 06/2028* — fix an expiry\n"
              "• *7 BATCH ST26-0439* — fix a batch number\n"
              "• *7 NEW* — add as a new product\n"
              "• *ALL NEW* — add every unlinked line as a new product\n"
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


def _is_all_new(text: str) -> bool:
    """True for `ALL NEW` in any casing or spacing, and nothing else.

    Deliberately not a prefix or substring match. `7 NEW` must keep working -- on an
    established pharmacy, a few unfamiliar items among fifty known ones deserve a decision
    each, and a greedy match here would take that away.
    """
    return (text or "").strip().upper().replace(" ", "") == "ALLNEW"


def _link_all_unmatched(grn_id: str) -> list[str]:
    """Create a product for every unmatched line on this GRN. Returns the names created.

    This exists because the per-line remedy is a dead end on a NEW pharmacy. approve()
    refuses while anything is unmatched and says to reply `<n> NEW` for each -- which is
    right when three lines out of fifty are new, and impossible when all fifty are, because
    there are no products to match against yet.

    A real pharmacy demonstrated it: 232 extracted invoice lines, and products, batches and
    stock_movements all zero. Gemini had read the invoices correctly. Finishing would have
    meant ~58 separate WhatsApp messages, so nobody did, and a customer asking for a
    medicine got "No product matching".

    Scope is deliberately narrow: this LINKS lines to catalogue entries. It does not
    approve, does not write batches or stock_movements, and does not touch the physical
    count or the POM gate. Creating a catalogue entry and moving stock are different
    decisions and stay that way -- the user still replies OK afterwards, and still counts
    the goods.

    Idempotent: a second call finds nothing unmatched and returns []. Someone will send it
    twice.
    """
    rows = q("""select line_no, raw_description from grn_lines
                 where grn_id = %s and product_id is null
                 order by line_no""", (grn_id,))
    created, skipped = [], []
    for r in rows:
        # A line with no description would become a product called "Unknown item" that a
        # pharmacist could later dispense from. Skip it and name it, so a human decides.
        if not (r["raw_description"] or "").strip():
            skipped.append(r["line_no"])
            continue
        name = _create_product_from_line(grn_id, r["line_no"])
        if name:
            created.append(name)
        else:
            skipped.append(r["line_no"])
    if skipped:
        log.info("grn %s: lines %s left unmatched (no usable description)", grn_id, skipped)
    return created


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
        (pid(), l["raw_code"] or f"NEW-{uuid.uuid4().hex[:6]}", name, pack, l["unit_price"]),
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
        # Offer the bulk form FIRST when most of the invoice is unmatched. On a new
        # pharmacy that is every line, and naming only the per-line command is what left
        # 232 extracted lines stranded: correct advice nobody could act on 58 times.
        bulk = len(unmatched) >= 3 or len(unmatched) == len(lines)
        how = (f"Reply *ALL NEW* to add all {len(unmatched)} as new products, "
               f"or *{unmatched[0]['line_no']} NEW* one at a time."
               if bulk else
               f"Reply *{unmatched[0]['line_no']} NEW* to add, for each.")
        reply_text(phone, f"Cannot receive yet — line(s) {nums} are not linked to a "
                         f"product. {how}")
        return

    g = q1("select * from grns where id=%s", (grn_id,))
    if g["status"] == "approved":
        reply_text(phone, "This invoice was already received.")
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
                (pid(), l["product_id"], l["batch_no"], l["expiry_date"],
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

        # Lines where the machine disagreed with the invoice and NOBODY answered.
        # Receiving still proceeds on the invoice quantity -- blocking is worse -- but
        # the disagreement must survive approval. Otherwise the sequence "vision says
        # 3 packs, invoice says 6, pharmacist just replies OK" books 6 as though the
        # question was never raised, and the ledger is knowingly wrong with no trace.
        unresolved = [
            f"{l['line_no']}. {l['product_name'] or l['raw_description']}: invoice "
            f"{from_pieces(l['qty_invoiced_pieces'] or 0, l['pack_size'])}, counted "
            f"{from_pieces((l['vision_packs'] or 0) * (l['pack_size'] or 1) + (l['vision_loose'] or 0), l['pack_size'])} "
            f"by photo, never confirmed"
            for l in lines
            if l.get("vision_packs") is not None
            and l.get("qty_counted_pieces") is None
            and "count_mismatch" in set(l["flags"] or [])
        ]

        cur.execute(
            """update grns set status='approved', approved_by=%s, approved_at=now(),
                               discrepancy_note=%s, unresolved_count_note=%s
                where id=%s""",
            (staff["id"], "; ".join(short_lines) or None,
             "; ".join(unresolved) or None, grn_id),
        )

    clear_state(phone)
    msg = [f"✅ Received {received} line(s) into stock. Approved by {staff['name']}."]
    if short_lines:
        msg.append("📋 Discrepancies recorded (claim within 48 hours):\n" +
                   "\n".join(short_lines))
    if unresolved:
        msg.append("⚠️ *Received on invoice quantities, count never confirmed:*\n"
                   + "\n".join(unresolved)
                   + "\n_Left open on the dashboard so it can still be claimed._")
    if no_expiry:
        msg.append(f"⚠️ {no_expiry} line(s) saved without an expiry date. "
                   "Add them on the dashboard when you can.")
    reply_text(phone, "\n\n".join(msg))

    owner = q1(
        "select phone from staff where pharmacy_id=%s and role='owner' and is_active limit 1",
        (pid(),),
    )
    if owner and owner["phone"] != phone:
        reply_text(owner["phone"],
                  f"📦 Stock received: {g['invoice_no'] or 'invoice'} · "
                  f"{kes(g['net_total'] or g['parsed_total'])} · {received} lines · "
                  f"by {staff['name']}"
                  + (f"\n⚠️ {len(short_lines)} count discrepancy(ies)" if short_lines else ""))
