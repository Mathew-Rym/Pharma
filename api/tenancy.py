"""Which pharmacy does this message belong to?

Replaces nine module-level `PID = settings.PHARMACY_ID` constants, each of which pinned
the whole process to one tenant at import time. Nothing here reads that setting: a
resolver that falls back to a configured default reinstates all nine behind one function
call, and the failure is invisible -- messages land in the wrong pharmacy's data and every
log line looks correct.

Two signals, ranked:

  device JID    which of OUR numbers received the message. Correct once each pharmacy has
                its own paired SIM: it cannot be influenced by the sender. GOWA already
                puts this in every webhook (`device_id`), so it costs nothing to use.

  sender phone  who is texting. The only signal available while one number serves several
                pharmacies. Weaker -- a person may legitimately belong to two tenants --
                so it returns a LIST and the caller decides, rather than guessing.
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from db import q, q1
from utils import norm_phone

log = logging.getLogger(__name__)

_current: ContextVar[str | None] = ContextVar("current_pharmacy", default=None)


class NoTenant(RuntimeError):
    """Code that needs a tenant ran without one being resolved.

    Deliberately fatal. The nine constants this replaces were nine implicit defaults, and
    an accessor that falls back to one reinstates all of them behind a single function
    call -- with the symptom being rows written to the wrong pharmacy while every log line
    looks correct. Loud failure is the only safe behaviour.
    """


def pid() -> str:
    """The pharmacy this unit of work belongs to."""
    v = _current.get()
    if not v:
        raise NoTenant("no pharmacy in context; resolve one before touching tenant data")
    return v


def set_pharmacy(pharmacy_id: str):
    """Bind the tenant. Returns the token needed to restore the previous value."""
    return _current.set(str(pharmacy_id))


def clear_pharmacy() -> None:
    _current.set(None)


@contextmanager
def pharmacy_scope(pharmacy_id: str):
    """Bind a tenant for the duration of a block, then restore what was there before.

    Restoring rather than clearing matters for the jobs loop, which sets a tenant per
    iteration: an inner scope must not leave the outer one unset. Reset in a finally so an
    exception cannot leave a tenant bound to a worker that is about to serve someone else.
    """
    token = _current.set(str(pharmacy_id))
    try:
        yield pharmacy_id
    finally:
        _current.reset(token)


@dataclass(frozen=True)
class Resolution:
    """Three outcomes, never two.

    `platform` must be distinguishable from `unknown`: the platform line carries real
    traffic, and collapsing the two makes a genuine routing failure look identical to
    normal platform behaviour exactly when you are trying to debug it.
    """
    kind: str                      # 'tenant' | 'platform' | 'unknown'
    pharmacy_id: str | None = None
    name: str | None = None


UNKNOWN = Resolution("unknown")


def resolve(device_jid: str, sender_phone: str | None = None) -> Resolution:
    """Resolve the tenant for an inbound message.

    `sender_phone` is accepted for logging and for callers that want to fall back to
    resolve_by_sender(); it deliberately does NOT override the device binding. The device
    is ours and the sender is not, so letting the sender win would let anyone choose which
    pharmacy's data they are talking to.
    """
    jid = (device_jid or "").strip()
    if not jid:
        log.warning("no device jid on inbound from %s", sender_phone)
        return UNKNOWN

    row = q1("""select id, name, kind from pharmacies where wa_jid = %s""", (jid,))
    if not row:
        # Deliberately not a fallback. An unrecognised device means we do not know whose
        # customer this is, and inventing an answer is worse than refusing.
        log.warning("unknown device jid %s (sender %s)", jid, sender_phone)
        return UNKNOWN

    if row["kind"] == "platform":
        return Resolution("platform", None, row["name"])
    return Resolution("tenant", str(row["id"]), row["name"])


def resolve_by_sender(phone: str) -> list[str]:
    """Every pharmacy this phone is known at, as staff, customer or supplier.

    Returns a LIST on purpose. The spec makes a person at two pharmacies a deliberate
    product decision -- separate history, separate balances -- so first-match-wins would
    silently pin them to whichever tenant happened to be created first, with no way to
    detect it from outside. One result: use it. Several: the caller must ask. None: greet
    them as new.

    Only ACTIVE staff count. Deactivating someone has to revoke access rather than merely
    hide them from a list.

    Platform rows are excluded. Onboarding gives a registering owner a contact row on the
    platform pharmacy so it is allowed to reply to them (see register._make_contactable),
    and without this filter that row would come back here forever: every message they ever
    send afterwards would resolve to two pharmacies and be answered with "which one?".
    """
    p = norm_phone(phone)
    if not p:
        return []
    rows = q("""select distinct t.pharmacy_id from (
                    select pharmacy_id from staff     where phone = %s and is_active
                    union
                    select pharmacy_id from customers where phone = %s
                    union
                    select pharmacy_id from suppliers where phone = %s
                ) t
                join pharmacies ph on ph.id = t.pharmacy_id
               where ph.kind = 'tenant'""", (p, p, p))
    return [str(r["pharmacy_id"]) for r in rows]
