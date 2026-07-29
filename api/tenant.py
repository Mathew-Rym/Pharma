from __future__ import annotations

"""Multi-tenant resolution.

Takes a sender's phone number and returns the pharmacy_id they belong to.
When two pharmacies share a GOWA device (e.g. a platform device), this is
the only way to know which pharmacy a message is "for".

Priority order: staff > customer > supplier > wa_messages history.
Staff first because they drive the highest-privilege operations.
"""
import logging

from db import q1
from utils import norm_phone

log = logging.getLogger(__name__)


def resolve_tenant(sender_phone: str) -> str | None:
    """Return the pharmacy_id for this phone, or None if unknown.

    For the pilot, each phone belongs to exactly one pharmacy. If a phone
    appears in multiple pharmacies (e.g. the owner runs two), the first
    match wins. Phase 2 should prompt for disambiguation.
    """
    phone = norm_phone(sender_phone)
    if not phone:
        return None

    # 1. Staff — most common sender, highest priority
    row = q1("select pharmacy_id from staff where phone = %s and is_active limit 1",
             (phone,))
    if row:
        return str(row["pharmacy_id"])

    # 2. Customer — people who have ordered before
    row = q1("select pharmacy_id from customers where phone = %s limit 1",
             (phone,))
    if row:
        return str(row["pharmacy_id"])

    # 3. Supplier — rep replying to a PO
    row = q1("select pharmacy_id from suppliers where phone = %s limit 1",
             (phone,))
    if row:
        return str(row["pharmacy_id"])

    # 4. Unknown — no relationship found
    return None


def resolve_pharmacy_by_device(device_id: str) -> str | None:
    """Return the pharmacy_id that owns this GOWA device.

    This is used by the webhook to figure out which pharmacy received the
    message, since GOWA identifies messages by the WhatsApp device_id.
    """
    if not device_id:
        return None
    # Try exact match on gowa_device_id or wa_jid
    row = q1("select id from pharmacies where gowa_device_id = %s or wa_jid = %s",
             (device_id, device_id))
    if row:
        return str(row["id"])
    # Try normalised phone against wa_number
    jid = device_id.split("@")[0] if "@" in device_id else device_id
    phone = norm_phone(jid)
    if phone:
        row = q1("select id from pharmacies where wa_number = %s", (phone,))
        if row:
            return str(row["id"])
    return None
