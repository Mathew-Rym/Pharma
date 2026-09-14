"""Talking to GOWA about SESSIONS rather than messages.

wa.py owns sending. This owns the other half of the relationship with the gateway: making
a slot exist, asking WhatsApp for a link code, and reading back which number a slot ended
up holding. Kept separate because the failure modes are different -- a send that fails is
one lost message, a pairing that goes to the wrong handset hands someone else a session on
our infrastructure.

Everything here is a thin, honest wrapper. No retries, no caching, no swallowing: the
caller is a conversation with a human waiting for an answer, and it needs to know the
difference between "WhatsApp said no" and "the gateway is down".
"""
import logging
import re

import httpx

from config import settings

log = logging.getLogger(__name__)

# A slot id becomes an HTTP header value and a directory name inside the container, so it
# has to survive both. Anything outside this set is rejected rather than sanitised --
# silently rewriting a slot id would produce a pharmacy whose gowa_device_id does not name
# the slot it actually paired.
_SLOT_OK = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


class ProvisionError(RuntimeError):
    """The gateway refused, or could not be reached. Always carries a human-readable why:
    it is relayed to a pharmacy owner mid-registration, not just logged."""


def _base() -> str:
    return settings.GOWA_URL.rstrip("/")


def _auth() -> tuple[str, str] | None:
    return (settings.GOWA_USER, settings.GOWA_PASS) if settings.GOWA_USER else None


def slot_for(wa_number: str) -> str:
    """The GOWA slot id for a pharmacy handset.

    Derived from the number rather than the pharmacy id, for two reasons: it is readable
    in `./run.sh safety` and in the container's storage directory, and it is stable if the
    pharmacy row is ever recreated. Uniqueness comes free -- one handset, one session.
    """
    digits = re.sub(r"\D", "", wa_number or "")
    if not digits:
        raise ProvisionError("cannot derive a device slot without a phone number")
    return f"ph-{digits}"


def slots() -> dict[str, str]:
    """{slot_id: jid}. An empty jid means the slot exists but nothing is linked to it."""
    try:
        r = httpx.get(f"{_base()}/devices", auth=_auth(), timeout=15)
        r.raise_for_status()
    except Exception as e:
        raise ProvisionError(f"WhatsApp gateway unreachable: {e}") from e
    return {s.get("id"): (s.get("jid") or "")
            for s in ((r.json() or {}).get("results") or [])}


def ensure_slot(slot: str) -> None:
    """Create the slot if GOWA does not already have it. Idempotent."""
    if not _SLOT_OK.match(slot):
        raise ProvisionError(f"invalid device slot {slot!r}")
    if slot in slots():
        return
    try:
        r = httpx.post(f"{_base()}/devices", auth=_auth(), timeout=20,
                       json={"device_id": slot})
    except Exception as e:
        raise ProvisionError(f"WhatsApp gateway unreachable: {e}") from e
    if r.status_code not in (200, 201):
        raise ProvisionError(f"gateway refused to create device {slot}: "
                             f"{r.status_code} {r.text[:200]}")
    log.info("created GOWA slot %s", slot)


def pair_code(slot: str, phone: str) -> str:
    """Ask WhatsApp for the 8-character link code for `phone`, on `slot`.

    The query parameter is `phone`, NOT `phone_number` -- despite GOWA's own validation
    error reading "phone_number(): cannot be blank" when it is missing. That message names
    an internal field and sends you to the wrong parameter; every pairing attempt then
    fails with what looks like a WhatsApp problem. Verified against the running container.
    """
    ensure_slot(slot)
    live = slots().get(slot)
    if live:
        raise ProvisionError(f"device slot {slot} is already linked to {live}")
    try:
        r = httpx.get(f"{_base()}/app/login-with-code", auth=_auth(),
                      headers={"X-Device-Id": slot}, params={"phone": phone},
                      timeout=90)
    except Exception as e:
        raise ProvisionError(f"WhatsApp gateway unreachable: {e}") from e
    res = (r.json() or {}).get("results") or {}
    code = res.get("pair_code") or res.get("code") or res.get("pairing_code")
    if not code:
        raise ProvisionError(f"WhatsApp did not return a link code "
                             f"({r.status_code}): {str(res)[:200]}")
    return str(code)


def linked_jid(slot: str) -> str | None:
    """The JID currently linked to `slot`, or None.

    This -- not the pairing request -- is what proves a handset finished linking. The
    request only proves WhatsApp issued a code.
    """
    return slots().get(slot) or None
