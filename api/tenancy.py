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
from dataclasses import dataclass

from db import q, q1
from utils import norm_phone

log = logging.getLogger(__name__)


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
    """
    p = norm_phone(phone)
    if not p:
        return []
    rows = q("""select distinct pharmacy_id from (
                    select pharmacy_id from staff     where phone = %s and is_active
                    union
                    select pharmacy_id from customers where phone = %s
                    union
                    select pharmacy_id from suppliers where phone = %s
                ) t""", (p, p, p))
    return [str(r["pharmacy_id"]) for r in rows]
