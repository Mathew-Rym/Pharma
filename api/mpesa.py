"""M-Pesa Daraja: STK Push (Lipa na M-Pesa Online) + C2B callback.

Idempotency is the whole game here. Safaricom will retry callbacks, and a duplicate
callback that double-credits an order is a bug your client will find before you do.
The unique index on payments.mpesa_receipt is the guard.
"""
import base64
import logging
from datetime import datetime, timedelta, timezone

import httpx

from config import settings
from db import ex, ex1, q1
from tenancy import NoTenant, pid
from utils import norm_phone

log = logging.getLogger(__name__)

BASE = ("https://sandbox.safaricom.co.ke" if settings.MPESA_ENV == "sandbox"
        else "https://api.safaricom.co.ke")

_token: str | None = None
_token_exp: datetime | None = None


def _access_token() -> str:
    global _token, _token_exp
    now = datetime.now(timezone.utc)
    if _token and _token_exp and _token_exp > now:
        return _token
    r = httpx.get(
        f"{BASE}/oauth/v1/generate?grant_type=client_credentials",
        auth=(settings.MPESA_KEY, settings.MPESA_SECRET),
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    _token = d["access_token"]
    _token_exp = now + timedelta(seconds=int(d.get("expires_in", 3599)) - 60)
    return _token


def stk_push(phone: str, amount: float, order_id: str,
             description: str = "Pharmacy order") -> dict:
    """Trigger the PIN prompt on the customer's phone.

    Daraja rejects decimals — amount must be a whole number of shillings.
    """
    phone = norm_phone(phone)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(
        f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{ts}".encode()
    ).decode()

    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": ts,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(round(float(amount))),
        "PartyA": phone,
        "PartyB": settings.MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": settings.MPESA_CALLBACK_URL,
        "AccountReference": str(order_id)[:12],
        "TransactionDesc": description[:60],
    }
    r = httpx.post(
        f"{BASE}/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers={"Authorization": f"Bearer {_access_token()}"},
        timeout=45,
    )
    data = r.json()
    log.info("stk_push order=%s resp=%s", order_id, data)

    # The pharmacy comes from the ORDER, not from .env and not from ambient context.
    #
    # It was settings.PHARMACY_ID, so a payment taken by pharmacy B was filed against
    # whichever pharmacy .env named: the money and the order pointed at different tenants,
    # and B's revenue appeared in someone else's books.
    #
    # pid() would fix that one case and leave the same weakness as db.apply_movement had --
    # a caller with the wrong tenant bound would file the payment somewhere plausible and
    # wrong. The order already knows which pharmacy it belongs to, and a payment that
    # disagreed with its own order would be worse than either bug. So it is derived, and a
    # mismatch with the bound tenant is refused rather than absorbed.
    o = q1("select pharmacy_id from orders where id = %s", (order_id,))
    if not o:
        raise ValueError(f"no such order {order_id}; refusing to record a payment")
    owner = str(o["pharmacy_id"])
    try:
        bound = pid()
    except NoTenant:
        bound = None
    if bound and bound != owner:
        raise ValueError(f"order {order_id} belongs to pharmacy {owner}, not the bound "
                         f"tenant {bound}; refusing to record the payment")

    ex(
        """insert into payments (pharmacy_id, order_id, method, amount, phone,
                                 checkout_request_id, status, raw_callback)
           values (%s,%s,'mpesa_stk',%s,%s,%s,%s,%s)""",
        (owner, order_id, amount, phone,
         data.get("CheckoutRequestID"),
         "pending" if data.get("ResponseCode") == "0" else "failed",
         __import__("json").dumps(data)),
    )
    return data


def handle_callback(body: dict) -> dict:
    """Process Daraja's STK callback. Safe to call twice with the same payload."""
    stk = (body.get("Body") or {}).get("stkCallback") or {}
    checkout_id = stk.get("CheckoutRequestID")
    result_code = stk.get("ResultCode")
    items = {i["Name"]: i.get("Value")
             for i in (stk.get("CallbackMetadata") or {}).get("Item", [])}
    receipt = items.get("MpesaReceiptNumber")
    amount = items.get("Amount")

    pay = q1("select * from payments where checkout_request_id = %s", (checkout_id,))
    if not pay:
        log.warning("callback for unknown checkout %s", checkout_id)
        return {"ok": False, "reason": "unknown_checkout"}

    if pay["status"] == "success":
        return {"ok": True, "reason": "already_processed"}

    if result_code != 0:
        ex("""update payments set status=%s, raw_callback=%s where id=%s""",
           ("failed", __import__("json").dumps(body), pay["id"]))
        _notify_failure(pay, stk.get("ResultDesc", "Payment not completed"))
        return {"ok": True, "status": "failed"}

    # success — unique index on mpesa_receipt makes a duplicate insert impossible
    updated = ex1(
        """update payments
              set status='success', mpesa_receipt=%s, amount=coalesce(%s, amount),
                  raw_callback=%s
            where id=%s and status <> 'success'
            returning id, order_id""",
        (receipt, amount, __import__("json").dumps(body), pay["id"]),
    )
    if not updated:
        return {"ok": True, "reason": "race_already_processed"}

    from rx import on_payment_success
    on_payment_success(updated["order_id"], receipt)
    return {"ok": True, "status": "success", "receipt": receipt}


def _notify_failure(pay: dict, reason: str) -> None:
    from wa import reply_text
    if pay.get("phone"):
        reply_text(pay["phone"],
                   f"Payment was not completed ({reason}). Reply *PAY* to try again, "
                   "or send the money to our Paybill and we will confirm manually.")
