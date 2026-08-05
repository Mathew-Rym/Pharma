"""Conversation state. Keeps the bot from re-asking things mid-flow.

Deliberately short-lived: a stale flow is worse than no flow, because a staff member
typing "OK" an hour later should not silently approve a delivery.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import tenancy
from config import settings
from db import ex, q1

log = logging.getLogger(__name__)

DEFAULT_TTL_MIN = 45


def get_state(phone: str) -> dict:
    row = q1(
        "select flow, context, expires_at from wa_state where phone = %s", (phone,)
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


def set_state(phone: str, flow: str, context: dict, ttl_min: int = DEFAULT_TTL_MIN,
              pharmacy_id: str | None = None) -> None:
    """Save the conversation's position.

    `pharmacy_id` defaults to whichever tenant is bound for this message, and only falls
    back to the configured one when nothing is bound at all. It used to be
    settings.PHARMACY_ID unconditionally, which stamped every pharmacy's half-finished
    delivery with the .env pharmacy -- invisible until a second tenant existed, at which
    point wa_state said the wrong shop for everyone.

    The fallback to settings.PHARMACY_ID is KEPT, unlike the ones removed from
    db.apply_movement and mpesa.stk_push, and the difference is what the row is for.

    A stock movement and a payment are records of value: filing one under the wrong
    pharmacy corrupts an audit trail and money, so those refuse rather than guess. wa_state
    is conversational position, its primary key is `phone` alone, and pharmacy_id is
    informational -- get_state() looks up by phone and never filters on it. Raising here
    would abandon someone mid-delivery, losing a half-finished GRN over a field nothing
    reads, which is a worse outcome than a wrong label.

    So it falls back, and logs, because an unbound tenant still means a caller skipped a
    scope somewhere and that should be findable rather than silent.
    """
    if pharmacy_id is None:
        try:
            pharmacy_id = tenancy.pid()
        except tenancy.NoTenant:
            pharmacy_id = settings.PHARMACY_ID
            log.warning("set_state(%s, flow=%s) with no tenant bound; labelling the row "
                        "with the configured pharmacy. The flow itself is keyed on phone, "
                        "so it still works -- but a caller is missing a pharmacy_scope.",
                        phone, flow)
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_min)
    ex(
        """insert into wa_state (phone, pharmacy_id, flow, context, expires_at, updated_at)
           values (%s,%s,%s,%s,%s, now())
           on conflict (phone) do update
             set flow = excluded.flow,
                 context = excluded.context,
                 expires_at = excluded.expires_at,
                 updated_at = now()""",
        (phone, pharmacy_id, flow, json.dumps(context), expires),
    )


def clear_state(phone: str) -> None:
    ex("delete from wa_state where phone = %s", (phone,))
