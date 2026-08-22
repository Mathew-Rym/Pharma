"""Conversation state scoped by (pharmacy_id, phone)."""
import json
from datetime import datetime, timedelta, timezone

from db import ex, q1
from tenancy import pid

DEFAULT_TTL_MIN = 45


def get_state(phone: str) -> dict:
    pharmacy_id = pid()
    row = q1(
        """select flow, context, expires_at from wa_state
           where phone = %s and pharmacy_id = %s""",
        (phone, pharmacy_id),
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


def set_state(phone: str, flow: str, context: dict,
              ttl_min: int = DEFAULT_TTL_MIN) -> None:
    pharmacy_id = pid()
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


def clear_state(phone: str) -> None:
    ex("delete from wa_state where phone = %s and pharmacy_id = %s", (phone, pid()))
