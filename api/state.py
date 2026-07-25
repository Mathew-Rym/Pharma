"""Conversation state. Keeps the bot from re-asking things mid-flow.

Deliberately short-lived: a stale flow is worse than no flow, because a staff member
typing "OK" an hour later should not silently approve a delivery.
"""
import json
from datetime import datetime, timedelta, timezone

from config import settings
from db import ex, q1

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


def set_state(phone: str, flow: str, context: dict, ttl_min: int = DEFAULT_TTL_MIN) -> None:
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_min)
    ex(
        """insert into wa_state (phone, pharmacy_id, flow, context, expires_at, updated_at)
           values (%s,%s,%s,%s,%s, now())
           on conflict (phone) do update
             set flow = excluded.flow,
                 context = excluded.context,
                 expires_at = excluded.expires_at,
                 updated_at = now()""",
        (phone, settings.PHARMACY_ID, flow, json.dumps(context), expires),
    )


def clear_state(phone: str) -> None:
    ex("delete from wa_state where phone = %s", (phone,))
