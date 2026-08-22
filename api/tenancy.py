"""Inbound pharmacy/platform resolution and tenant context."""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from db import q, q1
from utils import norm_phone

log = logging.getLogger(__name__)
_current: ContextVar[str | None] = ContextVar("current_pharmacy", default=None)


class NoTenant(RuntimeError):
    pass


def pid() -> str:
    value = _current.get()
    if not value:
        raise NoTenant("no pharmacy in context; resolve one before touching tenant data")
    return value


def set_pharmacy(pharmacy_id: str):
    return _current.set(str(pharmacy_id))


def clear_pharmacy() -> None:
    _current.set(None)


@contextmanager
def pharmacy_scope(pharmacy_id: str):
    token = _current.set(str(pharmacy_id))
    try:
        yield str(pharmacy_id)
    finally:
        _current.reset(token)


@dataclass(frozen=True)
class Resolution:
    kind: str  # tenant | platform | unknown
    pharmacy_id: str | None = None
    name: str | None = None


UNKNOWN = Resolution("unknown")


def resolve(device_jid: str, sender_phone: str | None = None) -> Resolution:
    """Resolve an inbound GOWA device while preserving tenant/platform/unknown."""
    raw = (device_jid or "").strip()
    if not raw:
        log.warning("no device id on inbound from %s", sender_phone)
        return UNKNOWN

    row = q1("""select id, name, kind from pharmacies
                where wa_jid=%s or gowa_device_id=%s""", (raw, raw))
    if not row:
        phone = norm_phone(raw.split("@")[0])
        if phone:
            row = q1("select id, name, kind from pharmacies where wa_number=%s", (phone,))
    if not row:
        log.warning("unknown inbound device %s (sender %s)", raw, sender_phone)
        return UNKNOWN

    if row["kind"] == "platform":
        return Resolution("platform", str(row["id"]), row["name"])
    return Resolution("tenant", str(row["id"]), row["name"])


def resolve_by_sender(phone: str) -> list[str]:
    p = norm_phone(phone)
    if not p:
        return []
    rows = q("""select distinct pharmacy_id from (
                    select pharmacy_id from staff where phone=%s and is_active
                    union
                    select pharmacy_id from customers where phone=%s
                    union
                    select pharmacy_id from suppliers where phone=%s
                ) t""", (p, p, p))
    return [str(r["pharmacy_id"]) for r in rows]
