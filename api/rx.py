"""FLOW C — Prescription to delivery.

The pharmacist gate at step 6 is not a feature you can trade away for a smoother demo.
Kenya's Pharmacy and Poisons Board requires a licensed pharmacist to verify a
prescription before dispensing. The AI prepares; a human with a registration number
approves. Every approval writes staff id + timestamp.
"""
import logging
import secrets
import uuid
from datetime import date, timedelta

from config import settings
from db import apply_movement, download, ex, ex1, q, q1, signed_url, tx
from llm import extract_prescription
from state import clear_state, get_state, set_state
from utils import from_pieces, kes, parse_date_loose
from wa import send_document, send_text

log = logging.getLogger(__name__)
PID = settings.PHARMACY_ID


# ------------------------------------------------------------ customer identity
def get_or_create_customer(phone: str) -> dict:
    row = q1("select * from customers where pharmacy_id=%s and phone=%s", (PID, phone))
    if row:
        return row
    return ex1(
        """insert into customers (pharmacy_id, phone) values (%s,%s)
           returning *""",
        (PID, phone),
    )


def ask_consent(phone: str) -> None:
    """Kenya Data Protection Act 2019 — explicit consent before we store health data."""
    set_state(phone, "awaiting_consent", {}, ttl_min=1440)
    send_text(
        phone,
        "Hello! This is the pharmacy's automated assistant.\n\n"
        "To take your order we need to save your phone number, your name and your "
        "prescription. We use it only to serve you, we never sell it, and you can ask "
        "us to delete it at any time by replying *DELETE*.\n\n"
        "Reply *YES* to continue.",
    )


def record_consent(phone: str) -> None:
    ex(
        """update customers set consent_given=true, consent_at=now()
            where pharmacy_id=%s and phone=%s""",
        (PID, phone),
    )
    clear_state(phone)
    send_text(phone,
              "Thank you. You can now:\n"
              "• Send a photo of your prescription\n"
              "• Ask if we have a medicine, e.g. *do you have amoxil*\n"
              "• Reply *STATUS* to check your order")


# ------------------------------------------------------------ prescription intake
def receive_prescription(phone: str, storage_path: str) -> None:
    cust = get_or_create_customer(phone)
    if not cust["consent_given"]:
        set_state(phone, "awaiting_consent", {"pending_rx": storage_path}, ttl_min=1440)
        ask_consent(phone)
        return

    send_text(phone, "Got your prescription. Reading it now, one moment...")
    try:
        img = download(settings.BUCKET_RX, storage_path)
        data = extract_prescription([img])
    except Exception as e:
        log.exception("rx extraction failed")
        send_text(phone, "Sorry, we could not read that image clearly. "
                         "Please retake the photo in good light, flat on a table.")
        return

    drugs = data.get("drugs") or []
    if not drugs:
        send_text(phone, "That does not look like a prescription. "
                         "Please send a clear photo of the doctor's prescription.")
        return

    flags = []
    issued = parse_date_loose(data.get("issued_date"))
    if issued and issued < date.today() - timedelta(days=settings.RX_MAX_AGE_DAYS):
        flags.append("expired_script")
    if not data.get("prescriber_reg"):
        flags.append("no_prescriber_reg")
    if any(not d.get("legible", True) for d in drugs):
        flags.append("illegible")
    if float(data.get("overall_confidence") or 0) < 0.7:
        flags.append("low_confidence")

    rx = ex1(
        """insert into prescriptions (pharmacy_id, customer_id, image_path, patient_name,
                                      prescriber_name, prescriber_reg, issued_date,
                                      extracted, confidence, flags, status)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending_verification')
           returning id""",
        (PID, cust["id"], storage_path, data.get("patient_name"),
         data.get("prescriber_name"), data.get("prescriber_reg"), issued,
         __import__("json").dumps(drugs), data.get("overall_confidence"), flags),
    )

    # v2: the PATIENT picks which items by number first, then a pharmacist verifies
    # the basket they actually chose. Ordering is deliberate — verifying a basket the
    # patient has already narrowed means the pharmacist checks one thing, not two.
    #
    # present_numbered_list -> approvals.handle_selection (creates the order, FEFO)
    #   -> approvals.notify_pharmacist (forwards the image + asks for APPROVE <PIN>)
    from approvals import present_numbered_list

    present_numbered_list(phone, str(rx["id"]), drugs)


def _build_quote(customer_id: str, rx_id: str, drugs: list[dict]):
    """FEFO allocation. Excludes batches too close to expiry to dispense."""
    order = ex1(
        """insert into orders (pharmacy_id, customer_id, prescription_id, status,
                               qr_token)
           values (%s,%s,%s,'awaiting_pharmacist',%s) returning id""",
        (PID, customer_id, rx_id, secrets.token_urlsafe(16)),
    )
    order_id = order["id"]
    avail, missing, subtotal = [], [], 0.0

    for d in drugs:
        name = " ".join(x for x in [d.get("drug"), d.get("strength")] if x)
        qty_wanted = int(d.get("qty") or 1)
        prod = q1(
            """select p.id, p.name, p.pack_size, p.sell_price, p.is_prescription_only,
                      s.qty_pieces
                 from products p
                 join v_stock_on_hand s on s.product_id = p.id
                where p.pharmacy_id=%s
                  and (p.name ilike %s or similarity(p.name,%s) > 0.35
                       or similarity(coalesce(p.generic_name,''), %s) > 0.35)
                order by similarity(p.name,%s) desc limit 1""",
            (PID, f"%{d.get('drug') or ''}%", name, d.get("drug") or "", name),
        )
        if not prod or (prod["qty_pieces"] or 0) <= 0:
            missing.append(name or (d.get("drug") or "unknown item"))
            continue

        batches = q(
            """select id, batch_no, expiry_date, qty_pieces from batches
                where pharmacy_id=%s and product_id=%s and qty_pieces > 0
                  and (expiry_date is null or expiry_date > current_date + %s)
                order by expiry_date nulls last""",
            (PID, prod["id"], timedelta(days=settings.MIN_SHELF_LIFE_DAYS)),
        )
        remaining, allocated = qty_wanted, []
        for b in batches:
            if remaining <= 0:
                break
            take = min(remaining, b["qty_pieces"])
            allocated.append((b["id"], take))
            remaining -= take
        if not allocated:
            missing.append(name)
            continue
        if remaining > 0:
            missing.append(f"{name} (only {qty_wanted - remaining} of {qty_wanted} available)")

        price = float(prod["sell_price"] or 0)
        for batch_id, take in allocated:
            total = price * take
            ex(
                """insert into order_lines (order_id, product_id, batch_id, qty_pieces,
                                            unit_price, line_total)
                   values (%s,%s,%s,%s,%s,%s)""",
                (order_id, prod["id"], batch_id, take, price, total),
            )
            subtotal += total
        got = qty_wanted - remaining
        avail.append({
            "name": prod["name"],
            "qty_text": from_pieces(got, prod["pack_size"]) + f" ({got} pcs)",
            "line_total": price * got,
        })

    total = subtotal + settings.DELIVERY_FEE
    ex("update orders set subtotal=%s, delivery_fee=%s, total=%s where id=%s",
       (subtotal, settings.DELIVERY_FEE, total, order_id))
    return order_id, avail, missing


def _notify_pharmacists(rx_id: str, cust: dict, drugs: list[dict], flags: list[str]) -> None:
    pharmacists = q(
        """select phone, name from staff
            where pharmacy_id=%s and role in ('pharmacist','owner','manager') and is_active""",
        (PID,),
    )
    names = ", ".join(d.get("drug", "?") for d in drugs[:5])
    warn = f"\n⚠️ Flags: {', '.join(flags)}" if flags else ""
    for p in pharmacists:
        send_text(p["phone"],
                  f"🩺 *Prescription awaiting your verification*\n"
                  f"Customer: {cust['phone']}\nItems: {names}{warn}\n\n"
                  f"Open the dashboard to review the image and approve or reject. "
                  f"No price is sent to the customer until you do.")


# ------------------------------------------------------------ pharmacist decision
def pharmacist_approve(rx_id: str, staff_id: str) -> None:
    """Called from the Streamlit dashboard. Writes who approved and when."""
    rx = q1("select * from prescriptions where id=%s", (rx_id,))
    staff = q1("select * from staff where id=%s", (staff_id,))
    if not rx or not staff:
        raise ValueError("unknown prescription or staff")
    if staff["role"] not in ("pharmacist", "owner", "manager"):
        raise PermissionError("only a pharmacist may verify a prescription")

    ex("""update prescriptions set status='verified', verified_by=%s, verified_at=now()
           where id=%s""", (staff_id, rx_id))

    order = q1("select * from orders where prescription_id=%s order by created_at desc limit 1",
               (rx_id,))
    if not order:
        return
    ex("update orders set status='awaiting_payment' where id=%s", (order["id"],))

    cust = q1("select * from customers where id=%s", (order["customer_id"],))
    lines = q(
        """select p.name, p.pack_size, l.qty_pieces, l.line_total
             from order_lines l join products p on p.id=l.product_id
            where l.order_id=%s""",
        (order["id"],),
    )
    body = "\n".join(
        f"• {l['name']} — {from_pieces(l['qty_pieces'], l['pack_size'])} — {kes(l['line_total'])}"
        for l in lines
    )
    ph = q1("select mpesa_paybill from pharmacies where id=%s", (PID,))
    set_state(cust["phone"], "awaiting_confirm", {"order_id": str(order["id"])}, ttl_min=120)
    send_text(
        cust["phone"],
        f"✅ *Verified by {staff['name']}"
        + (f" (PPB {staff['ppb_reg_no']})" if staff.get("ppb_reg_no") else "")
        + "*\n\n"
        f"{body}\n\n"
        f"Delivery: {kes(order['delivery_fee'])}\n"
        f"*Total: {kes(order['total'])}*\n\n"
        f"Reply *CONFIRM* to pay by M-Pesa, or *CANCEL*.\n"
        f"(If the M-Pesa prompt does not arrive, pay to Paybill "
        f"{ph['mpesa_paybill'] or '—'}, account {str(order['id'])[:8].upper()}.)",
    )


def pharmacist_reject(rx_id: str, staff_id: str, reason: str) -> None:
    ex("""update prescriptions
             set status='rejected', verified_by=%s, verified_at=now(), rejection_reason=%s
           where id=%s""", (staff_id, reason, rx_id))
    order = q1("select * from orders where prescription_id=%s order by created_at desc limit 1",
               (rx_id,))
    if order:
        ex("update orders set status='cancelled' where id=%s", (order["id"],))
        cust = q1("select phone from customers where id=%s", (order["customer_id"],))
        clear_state(cust["phone"])
        send_text(cust["phone"],
                  f"Our pharmacist could not dispense against this prescription.\n\n"
                  f"Reason: {reason}\n\nPlease call the pharmacy or visit us in person.")


# ------------------------------------------------------------ payment
def customer_confirm(phone: str) -> None:
    st = get_state(phone)
    order_id = st["context"].get("order_id")
    if not order_id:
        send_text(phone, "There is no order waiting for confirmation. "
                         "Send your prescription to start a new one.")
        return
    order = q1("select * from orders where id=%s", (order_id,))
    if not order or order["status"] != "awaiting_payment":
        send_text(phone, "That order is no longer awaiting payment.")
        clear_state(phone)
        return
    from mpesa import stk_push
    try:
        res = stk_push(phone, float(order["total"]), order_id)
        if res.get("ResponseCode") == "0":
            send_text(phone, "📲 Check your phone and enter your M-Pesa PIN to pay "
                             f"{kes(order['total'])}. We will confirm here automatically.")
        else:
            ph = q1("select mpesa_paybill from pharmacies where id=%s", (PID,))
            send_text(phone, "The M-Pesa prompt failed to send. Please pay to Paybill "
                             f"{ph['mpesa_paybill'] or '—'}, account "
                             f"{str(order_id)[:8].upper()}, amount {kes(order['total'])}.")
    except Exception:
        log.exception("stk push failed")
        send_text(phone, "Payment system is briefly unavailable. Please try *CONFIRM* again "
                         "in a minute.")


def on_payment_success(order_id: str, receipt: str) -> None:
    """Commit stock, generate receipt, award points, dispatch. Idempotent."""
    order = q1("select * from orders where id=%s", (order_id,))
    if not order or order["status"] not in ("awaiting_payment", "quoted"):
        return

    lines = q("select * from order_lines where order_id=%s", (order_id,))
    with tx() as cur:
        for l in lines:
            if l["batch_id"]:
                apply_movement(cur, l["batch_id"], -int(l["qty_pieces"]), "sale",
                               ref_table="orders", ref_id=order_id)
        cur.execute("update orders set status='paid' where id=%s", (order_id,))

    points = int(float(order["total"]) // settings.POINTS_PER_KES)
    if points:
        ex("insert into loyalty_ledger (customer_id, delta, reason, order_id) "
           "values (%s,%s,'purchase',%s)", (order["customer_id"], points, order_id))
        ex("update customers set loyalty_points = loyalty_points + %s where id=%s",
           (points, order["customer_id"]))

    cust = q1("select * from customers where id=%s", (order["customer_id"],))
    code = str(secrets.randbelow(9000) + 1000)   # 4-digit rider handover code
    ex("update orders set delivery_code=%s where id=%s", (code, order_id))

    from reports import build_receipt_pdf
    try:
        path, fname = build_receipt_pdf(order_id)
        ex("update orders set receipt_pdf=%s where id=%s", (path, order_id))
        url = signed_url(settings.BUCKET_DOCS, path, 604800)
        send_document(cust["phone"], url, fname,
                      f"Payment received ({receipt}). Thank you!")
    except Exception:
        log.exception("receipt pdf failed")

    clear_state(cust["phone"])
    send_text(
        cust["phone"],
        f"✅ *Payment confirmed* · {receipt}\n\n"
        f"Your order is being packed. Delivery code: *{code}* — give this to the rider.\n"
        f"You earned {points} loyalty point(s). Balance: "
        f"{(cust['loyalty_points'] or 0) + points}.\n\n"
        f"Reply *STATUS* any time.",
    )

    for s in q("""select phone from staff where pharmacy_id=%s and is_active
                   and role in ('owner','manager','attendant')""", (PID,)):
        send_text(s["phone"],
                  f"💰 Paid order {str(order_id)[:8].upper()} · {kes(order['total'])} · "
                  f"{cust['phone']} · code {code}. Pack and dispatch.")


# ------------------------------------------------------------ status / misc
def order_status(phone: str) -> None:
    o = q1(
        """select o.*, p.status as rx_status from orders o
             left join prescriptions p on p.id = o.prescription_id
             join customers c on c.id = o.customer_id
            where c.pharmacy_id=%s and c.phone=%s
            order by o.created_at desc limit 1""",
        (PID, phone),
    )
    if not o:
        send_text(phone, "You have no orders with us yet.")
        return
    human = {
        "awaiting_pharmacist": "Our pharmacist is verifying your prescription.",
        "awaiting_payment": f"Waiting for payment of {kes(o['total'])}. Reply *CONFIRM*.",
        "paid": f"Paid. Being packed. Delivery code {o['delivery_code']}.",
        "packed": f"Packed and waiting for a rider. Code {o['delivery_code']}.",
        "dispatched": f"On the way. Code {o['delivery_code']}.",
        "delivered": "Delivered. Thank you!",
        "cancelled": "This order was cancelled.",
        "quoted": "Quote prepared, awaiting pharmacist review.",
    }
    send_text(phone, f"Order {str(o['id'])[:8].upper()} · {kes(o['total'])}\n"
                     f"{human.get(o['status'], o['status'])}")


def delete_my_data(phone: str) -> None:
    """DPA 2019 right to erasure. Keeps financial records, drops personal data."""
    ex("""update customers set name=null, consent_given=false, marketing_opt_in=false
           where pharmacy_id=%s and phone=%s""", (PID, phone))
    ex("""update prescriptions set patient_name=null, extracted='[]'::jsonb
           where customer_id in (select id from customers where pharmacy_id=%s and phone=%s)""",
       (PID, phone))
    clear_state(phone)
    send_text(phone, "Your personal details and prescription contents have been removed. "
                     "Payment records are kept as the law requires.")
