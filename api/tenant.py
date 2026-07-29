"""Compatibility shim. The implementation lives in tenancy.py.

This module and tenancy.py were written independently and did the same job differently,
which is two sources of truth for the single most security-sensitive question in the
system. Consolidated on tenancy.py because of one behavioural difference:

  * resolve_tenant() here used `limit 1` -- first-match-wins. Its own docstring admitted
    the gap. The spec makes a person at two pharmacies a DELIBERATE product decision
    (separate history, separate balances), so silently picking whichever row was created
    first is undetectable from outside and locks them in permanently.
  * resolve_pharmacy_by_device() here ignored `kind`, so the platform line resolved as if
    it were a tenant -- collapsing three outcomes back into two.

Kept as a shim so existing imports keep working; prefer tenancy directly in new code.
"""
import logging

import tenancy
from db import q1
from utils import norm_phone

log = logging.getLogger(__name__)


def resolve_tenant(sender_phone: str) -> str | None:
    """The pharmacy this phone belongs to, or None if unknown OR ambiguous.

    Returns None rather than guessing when a phone is known at more than one pharmacy.
    Callers that can ask should use tenancy.resolve_by_sender() and prompt.
    """
    found = tenancy.resolve_by_sender(sender_phone)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        log.warning("phone %s is known at %d pharmacies; refusing to guess",
                    norm_phone(sender_phone), len(found))
    return None


def resolve_pharmacy_by_device(device_id: str) -> str | None:
    """The TENANT that owns this GOWA device, or None.

    Returns None for the platform line: it is not a tenant and has no inventory. Callers
    that need to tell platform from unknown should use tenancy.resolve().
    """
    if not device_id:
        return None
    r = tenancy.resolve(device_jid=device_id)
    if r.kind == "tenant":
        return r.pharmacy_id
    if r.kind == "platform":
        return None
    # Fall back to the slot label and to wa_number, since GOWA reports a JID on the
    # webhook but the slot id elsewhere, and older rows may only have wa_number set.
    row = q1("select id, kind from pharmacies where gowa_device_id = %s", (device_id,))
    if not row:
        phone = norm_phone(device_id.split("@")[0])
        if phone:
            row = q1("select id, kind from pharmacies where wa_number = %s", (phone,))
    if row and row["kind"] == "tenant":
        return str(row["id"])
    return None
