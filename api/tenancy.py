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


# ------------------------------------------------------------------ "is it live?"
#
# ONE definition, because three callers used to disagree and the disagreement cost six
# messages in a row on the night of the first real registration:
#
#   wa.compose()          required gowa_device_id       -> passed, wa_jid was NULL
#   wa.deliver()          compares live jid == expected -> refused, expected was NULL
#   jobs.for_every_tenant required gowa_device_id       -> selected a pharmacy that could
#                                                          never receive anything
#
# So compose accepted messages that were doomed at delivery, and the scheduler kept
# generating them. All three now read the definition below.
#
# All three conditions matter: gowa_device_id names the slot to send through, wa_jid is what
# deliver() checks that slot against, and status='active' is what activation_sweep sets once
# the handset has genuinely linked. Any one of them missing means "not reachable".
LIVE_SQL = "wa_jid is not null and gowa_device_id is not null and status = 'active'"


def why_not_live(pharmacy_id: str) -> str | None:
    """None when the pharmacy can send and receive; otherwise the human-readable reason.

    Returns a reason rather than a bool so the caller can say WHICH part is missing --
    "registered but the handset has not linked" and "no such pharmacy" are different
    operational problems and were previously indistinguishable.
    """
    row = q1("""select name, kind, status, wa_jid, gowa_device_id
                  from pharmacies where id = %s""", (str(pharmacy_id),))
    if not row:
        return f"unknown pharmacy {pharmacy_id}"
    if not row["gowa_device_id"]:
        return f"{row['name']} has no device slot; refusing to guess one"
    if not row["wa_jid"]:
        return (f"{row['name']} has a device slot but no linked handset yet "
                f"(status={row['status']}); run ./run.sh activate once it links")
    if row["status"] != "active":
        return f"{row['name']} is status={row['status']}, not active"
    return None


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
    """Every pharmacy this phone ACTS FOR: staff (active) or supplier. Not customers.

    Returns a LIST on purpose. The spec makes a person at two pharmacies a deliberate
    product decision -- separate history, separate balances -- so first-match-wins would
    silently pin them to whichever tenant happened to be created first, with no way to
    detect it from outside. One result: use it. Several: the caller must ask. None: greet
    them as new.

    Only ACTIVE staff count. Deactivating someone has to revoke access rather than merely
    hide them from a list.

    Platform rows are excluded: a platform pharmacy is not a tenant and has no inventory.

    ---- why `customers` is deliberately NOT here ----

    This resolver answers "which pharmacy does this number act FOR?". Shopping somewhere
    is not acting for it, and it is many-to-many by nature: a person who buys from three
    chemists is ordinary, not ambiguous. Including customers was a category error, and it
    had a concrete cost, found by driving the real flow rather than by reading the code:

      A stranger's FIRST message auto-creates a customers row -- router._handle_customer
      calls rx.get_or_create_customer() before the consent gate. So a pharmacy owner who
      registered through a host line and later sent that same line any ordinary message
      resolved to TWO pharmacies, and every message of theirs from then on was answered
      "you're registered at more than one pharmacy, which one?" instead of an answer.

    Filtering on `consent_given` does not fix it: the collision merely moves from "texted
    once" to "consented at two", and consenting at two pharmacies is normal behaviour.

    Customer traffic does not need this function. The device the message arrived on names
    the tenant (see resolve() and main.webhook_gowa, which sets pharmacy_id before the
    router ever consults the sender), so customers are routed by OUR number, not by their
    own identity -- which is the stronger signal anyway, because they cannot spoof it.

    CONSEQUENCE, stated rather than discovered later: a customer-only number whose
    device_id fails to resolve now falls through to router._greet_unknown and receives no
    reply. That is fail-closed and intended. Today it affects nobody, because every
    inbound arrives on a bound tenant device.

    THIS IS ARMED, NOT DORMANT. While every device resolves to a tenant, this function
    rarely runs at all. The moment a dedicated platform SIM is paired and
    `./run.sh platform` is applied, that device resolves to kind='platform' -- not a
    tenant -- so pharmacy_id is left unset and sender resolution becomes the PRIMARY path
    for every host-line message. The fix belongs before that SIM, not after.

    `suppliers` keeps the same many-to-many shape and is knowingly left in: one
    distributor across twenty pharmacies is twenty rows, so a distributor texting an
    unbound line resolves to twenty candidates and the caller asks. Asking is the correct
    degraded behaviour; guessing is not. Not fixed here, recorded so it is not
    rediscovered at scale.

    Gate 2 (safety.has_relationship) still reads customers, and must. It answers a
    different question -- "may we reply to this number?" -- and a customer we hold a row
    for is exactly who we may reply to. Only the resolver changed.
    """
    p = norm_phone(phone)
    if not p:
        return []
    rows = q("""select distinct t.pharmacy_id from (
                    select pharmacy_id from staff     where phone = %s and is_active
                    union
                    select pharmacy_id from suppliers where phone = %s
                ) t
                join pharmacies ph on ph.id = t.pharmacy_id
               where ph.kind = 'tenant'""", (p, p))
    return [str(r["pharmacy_id"]) for r in rows]
