"""The WhatsApp confirmation layer.

Three approvals move out of the dashboard and onto WhatsApp:

  1. PATIENT picks items by number from their own prescription
  2. PHARMACIST verifies a prescription with a PIN
  3. OWNER approves a purchase order with a PIN

Why WhatsApp rather than the dashboard, for the clinical one especially:

The single most important control when reading a handwritten Kenyan prescription is
ZOOM. WhatsApp's native image viewer already has pinch-zoom, rotate, and fullscreen,
tuned by Meta for exactly this hardware. Anything we build in Streamlit is worse.
Forwarding the original image to the pharmacist's phone gives us the best image
viewer in the world for free, and it means the pharmacist can verify from the
dispensing bench instead of walking to the office PC.

Attribution: a dropdown that lets anyone past a shared password sign as
"Pharmacist · PPB-11908" is not attribution, it is theatre — and PPB attribution is
the legal core of this product. Here, approving requires a PIN that only that
pharmacist knows, sent from a phone number already whitelisted in `staff`. Two
factors, both weak alone, meaningfully better together. It is still not a substitute
for real auth; it is the cheapest thing that is not a lie.
"""
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from config import settings
from db import ex, ex1, q, q1, signed_url
from state import clear_state, get_state, set_state
from utils import from_pieces, kes
from wa import send_image, send_text

log = logging.getLogger(__name__)
PID = settings.PHARMACY_ID
PIN_MAX_FAILS = 4
PIN_LOCK_MINUTES = 15


# ============================================================ PIN check
def check_pin(staff: dict, pin: str) -> tuple[bool, str]:
    """Returns (ok, message). Locks out after repeated failures."""
    if staff.get("pin_locked_until"):
        until = staff["pin_locked_until"]
        if until > datetime.now(until.tzinfo):
            mins = int((until - datetime.now(until.tzinfo)).total_seconds() // 60) + 1
            return False, f"Too many wrong PINs. Try again in {mins} minute(s)."

    if not staff.get("approval_pin"):
        return False, ("You have no approval PIN set. Ask the owner to set one on the "
                       "dashboard before you can approve.")

    if str(pin).strip() == str(staff["approval_pin"]).strip():
        ex("update staff set pin_failed_count=0, pin_locked_until=null where id=%s",
           (staff["id"],))
        return True, ""

    fails = (staff.get("pin_failed_count") or 0) + 1
    if fails >= PIN_MAX_FAILS:
        ex("""update staff set pin_failed_count=0,
                  pin_locked_until = now() + make_interval(mins => %s) where id=%s""",
           (PIN_LOCK_MINUTES, staff["id"]))
        log.warning("PIN lockout for staff %s", staff["id"])
        return False, f"Too many wrong PINs. Locked for {PIN_LOCK_MINUTES} minutes."
    ex("update staff set pin_failed_count=%s where id=%s", (fails, staff["id"]))
    return False, f"Wrong PIN. {PIN_MAX_FAILS - fails} attempt(s) left."


# ============================================================ 1. patient selection
def present_numbered_list(phone: str, rx_id: str, drugs: list[dict]) -> None:
    """Show the patient what we read off their own prescription, priced, numbered.

    We show what is NOT available too. A patient who learns item 3 is out of stock
    from a WhatsApp message did not need to travel to find out.
    """
    items = []
    for i, d in enumerate(drugs, start=1):
        name_q = " ".join(x for x in [d.get("drug"), d.get("strength")] if x)
        prod = q1("""select p.id, p.name, p.pack_size, p.sell_price,
                            coalesce(s.qty_pieces,0) as on_hand
                       from products p
                       left join v_stock_on_hand s on s.product_id = p.id
                      where p.pharmacy_id=%s
                        and (p.name ilike %s or similarity(p.name,%s) > 0.35)
                      order by similarity(p.name,%s) desc limit 1""",
                  (PID, f"%{d.get('drug') or ''}%", name_q, name_q))
        want = int(d.get("qty") or 1)
        if prod and prod["on_hand"] >= 1:
            price = float(prod["sell_price"] or 0)
            avail = min(want, prod["on_hand"])
            items.append({
                "n": i, "product_id": str(prod["id"]), "name": prod["name"],
                "pack_size": prod["pack_size"], "qty": avail, "want": want,
                "unit_price": price, "line_total": price * avail,
                "available": True, "partial": avail < want,
            })
        else:
            items.append({
                "n": i, "product_id": None,
                "name": name_q or (d.get("drug") or "unreadable item"),
                "qty": want, "available": False,
            })

    set_state(phone, "rx_select", {"rx_id": str(rx_id), "items": items}, ttl_min=120)

    lines = []
    for it in items:
        if it["available"]:
            note = f" (only {it['qty']} of {it['want']} available)" if it["partial"] else ""
            lines.append(f"*{it['n']}.* {it['name']} — {kes(it['line_total'])}{note}")
        else:
            lines.append(f"*{it['n']}.* {it['name']} — ❌ out of stock")

    have = [it for it in items if it["available"]]
    if not have:
        clear_state(phone)
        send_text(phone,
                  "We read your prescription but none of the items are in stock right "
                  "now. We will call you when they arrive, or you can reply *CALL* to "
                  "speak to a pharmacist.")
        return

    send_text(
        phone,
        "Here is what we read from your prescription:\n\n"
        + "\n".join(lines)
        + "\n\nReply with the numbers you need — for example *1,3* — or *ALL* for "
          "everything available.\n"
          "_A pharmacist checks every order before it is prepared._",
    )


def handle_selection(phone: str, text: str) -> None:
    st = get_state(phone)
    items = st["context"].get("items", [])
    rx_id = st["context"].get("rx_id")
    if not items:
        clear_state(phone)
        send_text(phone, "That session expired. Please send your prescription again.")
        return

    up = text.strip().upper()
    available = [it for it in items if it["available"]]
    if up in ("ALL", "EVERYTHING", "ZOTE"):
        chosen = available
    else:
        nums = {int(n) for n in re.findall(r"\d+", text)}
        chosen = [it for it in available if it["n"] in nums]

    if not chosen:
        send_text(phone,
                  "I did not catch which items you want. Reply with the numbers, "
                  "e.g. *1,3*, or *ALL*.")
        return

    order = ex1("""insert into orders (pharmacy_id, customer_id, prescription_id, status,
                        qr_token)
                   select %s, c.id, %s, 'awaiting_pharmacist', %s
                     from customers c where c.pharmacy_id=%s and c.phone=%s
                   returning id""",
                (PID, rx_id, secrets.token_urlsafe(16), PID, phone))
    if not order:
        send_text(phone, "Something went wrong creating your order. Please try again.")
        return

    subtotal = 0.0
    for it in chosen:
        batches = q("""select id, qty_pieces from batches
                        where pharmacy_id=%s and product_id=%s and qty_pieces > 0
                          and (expiry_date is null
                               or expiry_date > current_date + %s)
                        order by expiry_date nulls last""",
                    (PID, it["product_id"],
                     timedelta(days=settings.MIN_SHELF_LIFE_DAYS)))
        remaining = it["qty"]
        for b in batches:
            if remaining <= 0:
                break
            take = min(remaining, b["qty_pieces"])
            ex("""insert into order_lines (order_id, product_id, batch_id, qty_pieces,
                        unit_price, line_total)
                  values (%s,%s,%s,%s,%s,%s)""",
               (order["id"], it["product_id"], b["id"], take,
                it["unit_price"], it["unit_price"] * take))
            subtotal += it["unit_price"] * take
            remaining -= take

    total = subtotal + settings.DELIVERY_FEE
    ex("update orders set subtotal=%s, delivery_fee=%s, total=%s where id=%s",
       (subtotal, settings.DELIVERY_FEE, total, order["id"]))

    set_state(phone, "rx_waiting_pharmacist", {"order_id": str(order["id"])}, ttl_min=240)
    send_text(phone,
              "Thank you. Your order:\n\n"
              + "\n".join(f"• {it['name']} — {kes(it['line_total'])}" for it in chosen)
              + f"\n\nSubtotal {kes(subtotal)} + delivery {kes(settings.DELIVERY_FEE)}\n"
                f"*Total {kes(total)}*\n\n"
                f"A pharmacist is checking your prescription now. You will get a payment "
                f"request here once approved — usually a few minutes.")

    notify_pharmacist(rx_id, str(order["id"]))


# ============================================================ 2. pharmacist gate
def notify_pharmacist(rx_id: str, order_id: str) -> None:
    """Forward the ORIGINAL IMAGE plus the extraction to every pharmacist on duty.

    The image is the point. Native pinch-zoom on their own phone beats anything we
    would build, and it lets them verify from the bench.
    """
    rx = q1("""select p.*, c.phone as customer_phone, c.name as customer_name
                 from prescriptions p join customers c on c.id=p.customer_id
                where p.id=%s""", (rx_id,))
    order = q1("select * from orders where id=%s", (order_id,))
    if not rx or not order:
        return

    lines = q("""select pr.name, pr.pack_size, l.qty_pieces, l.line_total,
                        b.batch_no, b.expiry_date
                   from order_lines l
                   join products pr on pr.id=l.product_id
                   left join batches b on b.id=l.batch_id
                  where l.order_id=%s""", (order_id,))

    on_duty = _on_duty_pharmacists()
    if not on_duty:
        on_duty = q("""select * from staff where pharmacy_id=%s and is_active
                        and role in ('pharmacist','owner','manager')""", (PID,))

    body = [
        "🩺 *PRESCRIPTION FOR VERIFICATION*",
        f"Patient: {rx['patient_name'] or rx['customer_name'] or 'not stated'}",
        f"Phone: {rx['customer_phone']}",
        f"Prescriber: {rx['prescriber_name'] or '—'} (reg {rx['prescriber_reg'] or '—'})",
        f"Issued: {rx['issued_date'] or 'not stated'}",
    ]
    if rx["flags"]:
        body.append("⚠️ " + ", ".join(rx["flags"]))

    item_lines = []
    for line in lines:
        base = f"• {line['name']} — {from_pieces(line['qty_pieces'], line['pack_size'])}"
        if line["expiry_date"]:
            base += (f" — batch {line['batch_no'] or '?'} "
                     f"exp {line['expiry_date']:%m/%Y}")
        item_lines.append(base)
    body.append("\n*Patient selected:*\n" + "\n".join(item_lines))

    body.append(f"\nTotal {kes(order['total'])}")
    body.append(
        f"\n*Zoom into the image above and check every line.*\n\n"
        f"Reply *APPROVE <your PIN>* to release the payment request.\n"
        f"Reply *REJECT <reason>* to decline.\n"
        f"Ref: {str(order_id)[:8].upper()}"
    )
    text = "\n".join(body)

    url = signed_url(settings.BUCKET_RX, rx["image_path"], 7200)
    for s in on_duty:
        if url:
            send_image(s["phone"], url, caption="Prescription to verify")
        send_text(s["phone"], text)
        set_state(s["phone"], "pharmacist_review",
                  {"rx_id": str(rx_id), "order_id": str(order_id)}, ttl_min=240)


def _on_duty_pharmacists() -> list[dict]:
    """Roster-aware, so a prescription does not ping five phones at midnight."""
    today = datetime.now(timezone.utc).date()
    weekday = today.weekday()
    return q("""select distinct s.* from staff s
                  join duty_roster r on r.staff_id = s.id
                 where s.pharmacy_id=%s and s.is_active
                   and s.role in ('pharmacist','owner','manager')
                   and (r.on_date = %s or (r.on_date is null and r.weekday = %s))""",
             (PID, today, weekday))


def handle_pharmacist_reply(phone: str, staff: dict, text: str) -> bool:
    """Returns True if this message was an approval decision."""
    st = get_state(phone)
    if st["flow"] != "pharmacist_review":
        return False
    rx_id = st["context"].get("rx_id")
    order_id = st["context"].get("order_id")
    up = text.strip().upper()

    if up.startswith("APPROVE"):
        pin = re.sub(r"[^0-9]", "", up.replace("APPROVE", "", 1))
        ok, msg = check_pin(staff, pin)
        if not ok:
            send_text(phone, msg)
            return True
        _do_approve(rx_id, order_id, staff, phone)
        return True

    if up.startswith("REJECT"):
        reason = text.strip()[6:].strip(" :-") or "not specified"
        _do_reject(rx_id, order_id, staff, phone, reason)
        return True

    return False


def _do_approve(rx_id: str, order_id: str, staff: dict, phone: str) -> None:
    if staff["role"] not in ("pharmacist", "owner", "manager"):
        send_text(phone, "Your role cannot verify prescriptions.")
        return
    already = q1("select status, verified_by from prescriptions where id=%s", (rx_id,))
    if already and already["status"] == "verified":
        send_text(phone, "This prescription was already verified.")
        clear_state(phone)
        return

    ex("""update prescriptions set status='verified', verified_by=%s, verified_at=now()
           where id=%s""", (staff["id"], rx_id))
    ex("update orders set status='awaiting_payment' where id=%s", (order_id,))
    clear_state(phone)

    order = q1("select * from orders where id=%s", (order_id,))
    cust = q1("select * from customers where id=%s", (order["customer_id"],))
    ph = q1("select mpesa_paybill from pharmacies where id=%s", (PID,))

    send_text(phone, f"✅ Verified and released. Recorded against {staff['name']}"
                     + (f" (PPB {staff['ppb_reg_no']})" if staff.get("ppb_reg_no") else "")
                     + f" at {datetime.now():%H:%M}.")

    set_state(cust["phone"], "awaiting_payment", {"order_id": str(order_id)}, ttl_min=180)
    send_text(cust["phone"],
              f"✅ *Approved by our pharmacist.*\n\n"
              f"Amount due: *{kes(order['total'])}*\n\n"
              f"Reply *PAY* and we will send an M-Pesa request to this number.\n\n"
              f"Or pay directly:\nPaybill *{ph['mpesa_paybill'] or '—'}*\n"
              f"Account *{str(order_id)[:8].upper()}*\n"
              f"Amount *{int(float(order['total']))}*\n\n"
              f"After paying, forward the M-Pesa confirmation message here and we will "
              f"confirm automatically.")


def _do_reject(rx_id: str, order_id: str, staff: dict, phone: str, reason: str) -> None:
    ex("""update prescriptions set status='rejected', verified_by=%s, verified_at=now(),
              rejection_reason=%s where id=%s""", (staff["id"], reason, rx_id))
    ex("update orders set status='cancelled' where id=%s", (order_id,))
    clear_state(phone)
    order = q1("select customer_id from orders where id=%s", (order_id,))
    cust = q1("select phone from customers where id=%s", (order["customer_id"],))
    clear_state(cust["phone"])
    send_text(phone, f"Rejected and the patient has been told. Recorded against "
                     f"{staff['name']}.")
    send_text(cust["phone"],
              f"Our pharmacist could not dispense against this prescription.\n\n"
              f"Reason: {reason}\n\n"
              f"Please call us or visit the pharmacy so we can help properly.")


# ============================================================ 3. PO approval
def send_po_for_approval(po_id: str) -> None:
    po = q1("""select po.*, s.name as supplier, s.phone as sup_phone
                 from purchase_orders po join suppliers s on s.id=po.supplier_id
                where po.id=%s""", (po_id,))
    lines = q("""select p.name, p.pack_size, l.qty_pieces, l.unit_cost, l.rationale
                   from po_lines l join products p on p.id=l.product_id
                  where l.po_id=%s""", (po_id,))
    if not po or not lines:
        return

    body = (f"🧾 *Purchase order for approval*\n*{po['supplier']}*\n\n"
            + "\n".join(f"• {l['name']} — {from_pieces(l['qty_pieces'], l['pack_size'])}"
                        f" @ {kes(l['unit_cost'])}\n  _{l['rationale']}_"
                        for l in lines)
            + f"\n\nEstimated total: {kes(po['total_estimate'])}\n\n"
              f"Reply *OKPO <your PIN>* to send this to {po['supplier']} on WhatsApp.\n"
              f"Reply *EDITPO* to change quantities on the dashboard.\n"
              f"Ref: {str(po_id)[:8].upper()}")

    for s in q("""select phone from staff where pharmacy_id=%s and is_active
                   and role in ('owner','manager')""", (PID,)):
        set_state(s["phone"], "po_review", {"po_id": str(po_id)}, ttl_min=1440)
        send_text(s["phone"], body)


def handle_po_reply(phone: str, staff: dict, text: str) -> bool:
    st = get_state(phone)
    if st["flow"] != "po_review":
        return False
    po_id = st["context"].get("po_id")
    up = text.strip().upper()

    if up.startswith("OKPO"):
        pin = re.sub(r"[^0-9]", "", up.replace("OKPO", "", 1))
        ok, msg = check_pin(staff, pin)
        if not ok:
            send_text(phone, msg)
            return True

        po = q1("""select po.*, s.name as supplier, s.phone as sup_phone
                     from purchase_orders po join suppliers s on s.id=po.supplier_id
                    where po.id=%s""", (po_id,))
        lines = q("""select p.name, p.pack_size, l.qty_pieces
                       from po_lines l join products p on p.id=l.product_id
                      where l.po_id=%s""", (po_id,))
        if po["sup_phone"]:
            send_text(po["sup_phone"],
                      "Good day. Purchase order from our pharmacy:\n\n"
                      + "\n".join(f"• {l['name']} — "
                                  f"{from_pieces(l['qty_pieces'], l['pack_size'])}"
                                  for l in lines)
                      + "\n\nPlease confirm availability and delivery date. Thank you.")
        ex("""update purchase_orders set status='sent', approved_by=%s,
                  approved_at=now(), sent_at=now() where id=%s""", (staff["id"], po_id))
        clear_state(phone)
        send_text(phone, f"✅ Sent to {po['supplier']}"
                         + (f" on {po['sup_phone']}" if po["sup_phone"] else
                            " — no phone on file, please call them")
                         + f". Recorded against {staff['name']}.")
        return True

    if up == "EDITPO":
        clear_state(phone)
        send_text(phone, "Open the dashboard → Purchase orders to change quantities, "
                         "then approve there.")
        return True

    return False
