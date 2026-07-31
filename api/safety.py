"""WhatsApp anti-ban safety gates.

Four progressive gates, checked at compose time before any message is queued:

  1. ALLOWLIST   — dev/test only. When WA_ALLOWLIST is set, only those numbers
                   can receive messages. Empty in production → gate skipped.
  2. RELATIONSHIP — recipient must exist in customers, staff, suppliers, be
                   the pharmacy itself, or be part-way through registering with
                   it. Blocks messages to total strangers.
  3. CHAT ESTABLISHED — recipient must have messaged this pharmacy at least once
                   (recorded in inbound_history). Blocks cold outreach.
  4. RATE LIMIT  — per-device caps on total messages and new-chat initiations
                   per hour. Prevents burst patterns that trigger WhatsApp bans.

Gates are called from wa.compose(). Failures raise GateBlocked, which is caught
and logged rather than silently dropped, so blocked messages are always visible.
"""
import logging
from typing import Set

from config import settings
from db import ex, q1
from utils import norm_phone

log = logging.getLogger(__name__)

# Keyed on the raw setting, not a bare set. Caching only the parsed set meant a changed
# WA_ALLOWLIST needed a process restart to take effect -- so widening the list before a
# demo would look like it had worked while the old list was still enforced.
_allowlist_cache: dict = {"raw": None, "set": set()}

# How long a registration in progress keeps Gate 2 open for that phone. Long enough that
# someone can walk to the shop's handset and come back; short enough that an abandoned
# registration does not leave a stranger permanently messageable.
ONBOARDING_WINDOW_HOURS = 24


class GateBlocked(RuntimeError):
    """A safety gate prevented this message from being sent.

    Distinct from UnroutableMessage (no device to send by). GateBlocked means
    the device exists but sending would be unsafe for WhatsApp compliance.
    """
    def __init__(self, gate: str, detail: str):
        self.gate = gate
        super().__init__(f"[Gate {gate}] {detail}")


# ------------------------------------------------------------------ Gate 1
def _get_allowlist() -> Set[str]:
    """Parse WA_ALLOWLIST env var into a set of normalised phone numbers.

    Production: WA_ALLOWLIST unset → empty set → gate skipped.
    Dev/Test: WA_ALLOWLIST="254777602338,254700111111" → gate active.
    """
    raw = settings.WA_ALLOWLIST or ""
    if _allowlist_cache["raw"] != raw:
        _allowlist_cache["raw"] = raw
        _allowlist_cache["set"] = {norm_phone(p.strip())
                                   for p in raw.split(",") if p.strip()}
    return _allowlist_cache["set"]


def check_allowlist(phone: str) -> None:
    """Gate 1: if an allowlist is configured, only those numbers may receive."""
    allowlist = _get_allowlist()
    if not allowlist:
        return  # gate disabled in production
    normalized = norm_phone(phone)
    if normalized not in allowlist:
        raise GateBlocked("allowlist",
                          f"{phone} (→{normalized}) not on allowlist "
                          f"({len(allowlist)} entries)")


# ------------------------------------------------------------------ Gate 2
def has_relationship(phone: str, pharmacy_id: str) -> bool:
    """Check if the recipient has an existing relationship with this pharmacy."""
    normalized = norm_phone(phone)
    if not normalized:
        return False

    # Check customer
    if q1("select id from customers where phone = %s and pharmacy_id = %s",
          (normalized, str(pharmacy_id))):
        return True

    # Check staff
    if q1("select id from staff where phone = %s and pharmacy_id = %s",
          (normalized, str(pharmacy_id))):
        return True

    # Check supplier
    if q1("select id from suppliers where phone = %s and pharmacy_id = %s",
          (normalized, str(pharmacy_id))):
        return True

    # Check pharmacy's own number
    if q1("select id from pharmacies where wa_number = %s and id = %s",
          (normalized, str(pharmacy_id))):
        return True

    # Someone this line is currently onboarding (see register._make_contactable).
    #
    # This is the narrow, deliberate exception the gate needs: registration is the one
    # flow where a phone with no prior relationship must receive a reply. It is scoped to
    # the pharmacy that is answering and expires, so an abandoned registration does not
    # leave a number permanently messageable. Gate 3 still applies on top -- the row is
    # only ever written for a phone that has just messaged us.
    # make_interval, not "interval '%s hours'": a placeholder inside a quoted literal is
    # not a placeholder at all, it is the two characters %s inside a string, and Postgres
    # rejects it at parse time.
    if q1("""select 1 from onboarding_contacts
              where phone = %s and pharmacy_id = %s
                and created_at > now() - make_interval(hours => %s)""",
          (normalized, str(pharmacy_id), ONBOARDING_WINDOW_HOURS)):
        return True

    return False


def check_relationship(phone: str, pharmacy_id: str) -> None:
    """Gate 2: recipient must be known to this pharmacy."""
    if not has_relationship(phone, pharmacy_id):
        raise GateBlocked("relationship",
                          f"{phone} has no relationship to pharmacy {pharmacy_id}")


# ------------------------------------------------------------------ Gate 3
def record_inbound(phone: str, pharmacy_id: str) -> None:
    """Record that a phone has messaged this pharmacy.

    Called on every inbound message (router.handle_inbound). This is the single
    place that opens Gate 3 for a phone number.
    """
    normalized = norm_phone(phone)
    if not normalized:
        return
    try:
        ex("""
            insert into inbound_history (pharmacy_id, phone, first_seen_at, last_seen_at)
            values (%s, %s, now(), now())
            on conflict (pharmacy_id, phone)
            do update set last_seen_at = now(),
                         message_count = inbound_history.message_count + 1
        """, (str(pharmacy_id), normalized))
    except Exception:
        # Never let a tracking failure block message processing
        log.exception("failed to record inbound from %s", normalized)


def has_inbound_history(phone: str, pharmacy_id: str) -> bool:
    """Check if this phone has ever messaged this pharmacy."""
    normalized = norm_phone(phone)
    if not normalized:
        return False
    return bool(q1("""
        select id from inbound_history
        where pharmacy_id = %s and phone = %s
    """, (str(pharmacy_id), normalized)))


def check_chat_established(phone: str, pharmacy_id: str) -> None:
    """Gate 3: no cold messages — recipient must have messaged us first."""
    if not has_inbound_history(phone, pharmacy_id):
        raise GateBlocked("chat_established",
                          f"Cannot cold-message {phone} — they have never "
                          f"messaged pharmacy {pharmacy_id}. Have them message "
                          f"first, or use an onboarding flow.")


# ------------------------------------------------------------------ Gate 4
def is_rate_limited(device_id: str) -> bool:
    """Check if this device has exceeded hourly rate limits."""
    if not device_id:
        return False

    # Total messages in last hour. Only ones that actually reached WhatsApp: a message
    # the slot/JID guard refused never left, and counting it would let the guard doing
    # its job exhaust the pharmacy's own quota and silence real replies.
    row = q1("""
        select count(*) as n from wa_messages
        where gowa_device_id = %s
          and direction = 'out'
          and coalesce(status, 'sent') not in ('refused', 'failed')
          and created_at > now() - interval '1 hour'
    """, (device_id,))
    if row and (row["n"] or 0) >= settings.WA_RATE_LIMIT_HOUR:
        return True

    # New chats in last hour (sends to numbers with no inbound history)
    row = q1("""
        select count(*) as n from wa_messages wm
        where wm.gowa_device_id = %s
          and wm.direction = 'out'
          and wm.created_at > now() - interval '1 hour'
          and not exists (
              select 1 from inbound_history ih
              where ih.phone = wm.to_phone
                and ih.pharmacy_id = wm.pharmacy_id
          )
    """, (device_id,))
    if row and (row["n"] or 0) >= settings.WA_NEW_CHAT_LIMIT_HOUR:
        return True

    return False


def check_rate_limit(device_id: str) -> None:
    """Gate 4: per-device hourly rate caps."""
    if is_rate_limited(device_id):
        raise GateBlocked("rate_limit",
                          f"Rate limit exceeded for device {device_id}")


# ------------------------------------------------------------------ combined
def check_gates(phone: str, pharmacy_id: str, device_id: str) -> None:
    """Run all safety gates. Raises GateBlocked on failure.

    There is deliberately NO is_reply escape hatch. A bypass argument is
    indistinguishable from no gate at all -- every proactive sender that wants to send
    will pass it, and the one caller who genuinely needs it is the one who least needs
    it. Replies are already legal without a flag: router.handle_inbound records every
    inbound message before dispatching, so by the time any reply is composed the
    recipient has inbound history and Gate 3 opens on the evidence rather than on the
    caller's word.

    The consequence, which is the correct one: if recording the inbound failed, the
    reply is blocked. Fail closed.
    """
    check_allowlist(phone)
    check_relationship(phone, pharmacy_id)
    check_chat_established(phone, pharmacy_id)
    check_rate_limit(device_id)
