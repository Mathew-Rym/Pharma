"""Payment confirmation from a forwarded M-Pesa SMS.

WHY THIS EXISTS: Safaricom's Daraja go-live for a real Paybill takes days to weeks.
An STK push in sandbox only works against Safaricom's test MSISDN. Neither gets you a
believable payment demo on Friday against the pharmacy's real Paybill 4166919.

This path does: the customer pays to the existing Paybill, forwards the M-Pesa
confirmation SMS into the WhatsApp chat, and we parse it, match it to their open
order, and confirm — with no Safaricom integration at all.

⚠️ READ THIS BEFORE PRODUCTION ⚠️
A forwarded SMS is TEXT. Anyone can type a fake one. This is spoofable and must never
be the sole confirmation for money in production. Two safeguards are built in:
  1. every SMS-confirmed payment is marked `source='sms_forward'` and lands in a
     pharmacist confirmation queue rather than auto-releasing goods
  2. the receipt code is unique-indexed, so the same SMS cannot be replayed
In production, replace this with the Daraja C2B confirmation callback and demote this
to a fallback for when the callback is late. The pharmacy owner will check their own
M-Pesa statement anyway — do not pretend otherwise in the pitch.
"""
import logging
import re

from config import settings
from db import ex, ex1, q, q1
from state import clear_state, get_state
from utils import kes, norm_phone
from wa import send_text

log = logging.getLogger(__name__)
PID = settings.PHARMACY_ID

# Safaricom's confirmation SMS wording has varied over the years, so match loosely
# on the pieces that have stayed stable: a receipt code, an amount, and a keyword.
RECEIPT_RE = re.compile(r"\b([A-Z0-9]{10})\b")
AMOUNT_RE = re.compile(r"[Kk][Ss][Hh]?\s*([\d,]+(?:\.\d{1,2})?)", re.I)
ACCOUNT_RE = re.compile(r"\bfor account\s+([A-Z0-9\-]+)", re.I)
MPESA_HINTS = ("confirmed", "m-pesa", "mpesa", "paybill", "sent to", "paid to")


def looks_like_mpesa_sms(text: str) -> bool:
    t = (text or "").lower()
    if len(t) < 25:
        return False
    hits = sum(1 for h in MPESA_HINTS if h in t)
    return hits >= 1 and bool(AMOUNT_RE.search(text)) and bool(RECEIPT_RE.search(text.upper()))


def parse_mpesa_sms(text: str) -> dict:
    """Extract what we need without pretending to fully parse Safaricom's format."""
    up = (text or "").upper()
    receipt = None
    for candidate in RECEIPT_RE.findall(up):
        # a receipt code mixes letters and digits; a pure number is an amount or date
        if any(c.isalpha() for c in candidate) and any(c.isdigit() for c in candidate):
            receipt = candidate
            break
    amount = None
    m = AMOUNT_RE.search(text)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    acct = None
    m = ACCOUNT_RE.search(text)
    if m:
        acct = m.group(1).upper()
    return {"receipt": receipt, "amount": amount, "account": acct}


def handle_forwarded_sms(phone: str, text: str) -> bool:
    """Returns True if we consumed this message as a payment confirmation."""
    if not looks_like_mpesa_sms(text):
        return False

    parsed = parse_mpesa_sms(text)
    if not parsed["receipt"] or not parsed["amount"]:
        send_text(phone, "That looks like an M-Pesa message but I could not read the "
                         "code and amount clearly. Please forward it again, or reply "
                         "with the code and amount.")
        return True

    # replay guard
    dup = q1("select id, order_id from payments where mpesa_receipt=%s",
             (parsed["receipt"],))
    if dup:
        send_text(phone, f"We have already recorded receipt {parsed['receipt']}.")
        return True

    order = _match_order(phone, parsed)
    if not order:
        ex("""insert into payments (pharmacy_id, method, amount, phone, status,
                    mpesa_receipt, source, sms_text)
              values (%s,'mpesa_paybill',%s,%s,'success',%s,'sms_forward',%s)""",
           (PID, parsed["amount"], norm_phone(phone), parsed["receipt"], text[:1000]))
        send_text(phone,
                  f"Thank you — we have logged {kes(parsed['amount'])} "
                  f"({parsed['receipt']}). We could not match it to an open order "
                  f"automatically, so a member of staff will confirm shortly.")
        _alert_staff(f"💰 Unmatched payment {kes(parsed['amount'])} "
                     f"({parsed['receipt']}) from {phone}. Match it on the dashboard.")
        return True

    expected = float(order["total"] or 0)
    paid = float(parsed["amount"])

    ex1("""insert into payments (pharmacy_id, order_id, method, amount, phone,
                 status, mpesa_receipt, source, sms_text)
           values (%s,%s,'mpesa_paybill',%s,%s,'success',%s,'sms_forward',%s)
           returning id""",
        (PID, order["id"], paid, norm_phone(phone), parsed["receipt"], text[:1000]))

    if abs(paid - expected) > 1.0:
        send_text(phone,
                  f"We received {kes(paid)} but the order total is {kes(expected)}. "
                  f"A member of staff will call you to sort out the difference.")
        _alert_staff(f"⚠️ Payment mismatch on order {str(order['id'])[:8].upper()}: "
                     f"paid {kes(paid)}, expected {kes(expected)}. "
                     f"Receipt {parsed['receipt']}.")
        return True

    # Amount matches. Confirm to the customer, but goods are released by a human.
    from rx import on_payment_success
    try:
        on_payment_success(str(order["id"]), parsed["receipt"])
    except Exception:
        log.exception("post-payment processing failed")
        send_text(phone, f"Payment {parsed['receipt']} received, thank you. "
                         "We are preparing your order.")
    clear_state(phone)

    _alert_staff(f"💰 *Payment confirmed by forwarded SMS*\n"
                 f"Order {str(order['id'])[:8].upper()} · {kes(paid)} · "
                 f"receipt {parsed['receipt']}\n"
                 f"⚠️ SMS-confirmed — check it appears in the M-Pesa statement before "
                 f"handing over goods.")
    return True


def _match_order(phone: str, parsed: dict) -> dict | None:
    """Match on account reference first, then on this customer's open order."""
    if parsed.get("account"):
        row = q1("""select o.* from orders o
                     where o.pharmacy_id=%s
                       and upper(left(o.id::text, 8)) = %s
                       and o.status in ('awaiting_payment','quoted')
                     limit 1""", (PID, parsed["account"][:8]))
        if row:
            return row

    st = get_state(phone)
    if st["context"].get("order_id"):
        row = q1("""select * from orders where id=%s
                     and status in ('awaiting_payment','quoted')""",
                 (st["context"]["order_id"],))
        if row:
            return row

    return q1("""select o.* from orders o join customers c on c.id=o.customer_id
                  where c.pharmacy_id=%s and c.phone=%s
                    and o.status in ('awaiting_payment','quoted')
                  order by o.created_at desc limit 1""", (PID, norm_phone(phone)))


def _alert_staff(msg: str) -> None:
    for s in q("""select phone from staff where pharmacy_id=%s and is_active
                   and role in ('owner','manager','pharmacist')""", (PID,)):
        send_text(s["phone"], msg)


def unmatched_payments() -> list[dict]:
    """Shown on the dashboard so money never sits unexplained."""
    return q("""select id, amount, phone, mpesa_receipt, created_at, sms_text
                  from payments
                 where pharmacy_id=%s and order_id is null and status='success'
                 order by created_at desc limit 50""", (PID,))
