"""Conversation state. Keeps the bot from re-asking things mid-flow.

Deliberately short-lived: a stale flow is worse than no flow, because a staff member
typing "OK" an hour later should not silently approve a delivery.

TENANT-AWARE: keyed on (pharmacy_id, phone) so the same person can interact with
multiple pharmacies without state leaking between them. Before this change the key
was phone alone, so a customer messaging two pharmacies would see one pharmacy's
flow state from the other's conversation.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import tenancy
from db import ex, q1

log = logging.getLogger(__name__)

DEFAULT_TTL_MIN = 45


def get_state(phone: str, pharmacy_id: str | None = None) -> dict:
    """Read the conversation state for (pharmacy_id, phone).

    When pharmacy_id is not given, uses the currently bound tenant. This keeps
    all existing call sites working without changes — they call get_state(phone)
    from inside a pharmacy_scope block.
    """
    pid = pharmacy_id
    if pid is None:
        try:
            pid = tenancy.pid()
        except tenancy.NoTenant:
            # No tenant bound. Fall back to phone-only lookup for backward
            # compatibility (onboarding, where no tenant exists yet).
            row = q1(
                "select flow, context, expires_at from wa_state where phone = %s "
                "order by updated_at desc limit 1", (phone,)
            )
            if not row:
                return {"flow": "idle", "context": {}}
            if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
                clear_state(phone)
                return {"flow": "idle", "context": {}}
            ctx = row["context"] or {}
            if isinstance(ctx, str):
                ctx = json.loads(ctx)
            return {"flow": row["flow"] or "idle", "context": ctx}

    row = q1(
        "select flow, context, expires_at from wa_state "
        "where pharmacy_id = %s and phone = %s", (pid, phone)
    )
    if not row:
        return {"flow": "idle", "context": {}}
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        clear_state(phone, pharmacy_id=pid)
        return {"flow": "idle", "context": {}}
    ctx = row["context"] or {}
    if isinstance(ctx, str):
        ctx = json.loads(ctx)
    return {"flow": row["flow"] or "idle", "context": ctx}


def set_state(phone: str, flow: str, context: dict, ttl_min: int = DEFAULT_TTL_MIN,
              pharmacy_id: str | None = None) -> None:
    """Save the conversation's position for (pharmacy_id, phone).

    `pharmacy_id` defaults to whichever tenant is bound for this message.
    Falls back to settings.PHARMACY_ID only as a last resort, with a warning.
    """
    if pharmacy_id is None:
        try:
            pharmacy_id = tenancy.pid()
        except tenancy.NoTenant:
            from config import settings
            pharmacy_id = settings.PHARMACY_ID
            log.warning("set_state(%s, flow=%s) with no tenant bound; labelling the row "
                        "with the configured pharmacy. The flow itself is keyed on "
                        "(pharmacy_id, phone), so a caller is missing a pharmacy_scope.",
                        phone, flow)
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_min)
    ex(
        """insert into wa_state (phone, pharmacy_id, flow, context, expires_at, updated_at)
           values (%s,%s,%s,%s,%s, now())
           on conflict (pharmacy_id, phone) do update
             set flow = excluded.flow,
                 context = excluded.context,
                 expires_at = excluded.expires_at,
                 updated_at = now()""",
        (phone, pharmacy_id, flow, json.dumps(context), expires),
    )


def clear_state(phone: str, pharmacy_id: str | None = None) -> None:
    """Remove the conversation state for (pharmacy_id, phone).

    When pharmacy_id is not given, removes all state for this phone across
    all pharmacies — the safe default for onboarding and for callers inside
    a pharmacy_scope (where the old phone-only delete is the same thing).
    """
    if pharmacy_id:
        ex("delete from wa_state where pharmacy_id = %s and phone = %s",
           (pharmacy_id, phone))
    else:
        ex("delete from wa_state where phone = %s", (phone,))
