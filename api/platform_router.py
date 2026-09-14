"""PharmaOS master-number router.

The platform WhatsApp number is for pharmacy onboarding, account linking and support.
It is intentionally separate from tenant pharmacy customer traffic.
"""
import logging

from db import ex, q1
from safety import record_inbound
from tenancy import pharmacy_scope
from wa import reply_text

log = logging.getLogger(__name__)


def handle_platform_inbound(msg: dict, platform_id: str) -> None:
    phone = str(msg.get("from") or "")
    if not phone:
        return

    with pharmacy_scope(platform_id):
        record_inbound(phone, platform_id)
        text = (msg.get("text") or "").strip()
        wa_id = msg.get("wa_id")

        if wa_id and q1("select 1 from wa_messages where wa_id=%s", (wa_id,)):
            return

        ex("""insert into wa_messages
              (pharmacy_id, wa_id, direction, from_phone, msg_type, body, media_path,
               gowa_device_id, status, handled)
              values (%s,%s,'in',%s,%s,%s,%s,%s,'processing',false)
              on conflict (wa_id) do nothing""",
           (platform_id, wa_id, phone, msg.get("type", "text"), text[:4000],
            msg.get("media_path"), msg.get("gowa_device_id")))

        up = text.upper()
        if up in ("HELP", "HI", "HELLO", "START", "ONBOARD", "ONBOARDING", "REGISTER"):
            reply_text(phone,
                "Welcome to PharmaOS.\n\n"
                "We help pharmacies connect their WhatsApp number and use PharmaOS.\n\n"
                "Reply *ONBOARD* to start, *LINK* if you already have an 8-digit code, or *HELP*.")
        elif up.startswith("LINK ") or up.startswith("CODE "):
            code = text.split(maxsplit=1)[1].strip()
            reply_text(phone, f"Received code {code}. Linking will continue with your pharmacy setup.")
        elif up.startswith("ONBOARD"):
            reply_text(phone,
                "To onboard your pharmacy, send:\n"
                "1. Pharmacy name\n2. Owner/manager name\n3. Pharmacy WhatsApp number\n\n"
                "A PharmaOS onboarding assistant will guide the remaining steps.")
        else:
            reply_text(phone,
                "Welcome to PharmaOS. This number is for pharmacies, onboarding and support.\n\n"
                "Reply *ONBOARD* to join, *LINK <8-digit code>* to link a WhatsApp number, or *HELP*.")

        ex("update wa_messages set handled=true, status='handled' where wa_id=%s", (wa_id,))
