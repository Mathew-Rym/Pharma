"""Outbound WhatsApp. The only place that knows how to reach the transport.

Two backends, chosen by WA_BACKEND. The three public functions below have identical
signatures either way, which is the whole point: business logic never learns which
transport is in use, so swapping is a config change rather than a refactor.

  baileys — the original wa-gateway/ node service. One device per server.
  gowa    — go-whatsapp-web-multidevice. Multi-device, so ONE server can hold a
            separate WhatsApp account per pharmacy, scoped by X-Device-Id. That is
            what makes this the right choice as soon as there is a second pharmacy.

GOWA notes learned from reading its source, not guessed:
  * ParseJID() accepts a bare number, so norm_phone() output works unchanged.
  * /send/image and /send/file tolerate a missing file upload when *_url is given
    (`if err == nil { request.Image = file }`), so we can keep passing signed URLs
    and never stream bytes through this process.
"""
import logging

import httpx

from config import settings
from db import ex, q1
from safety import GateBlocked, check_gates
from utils import norm_phone

log = logging.getLogger(__name__)
_HEADERS = {"x-pharmaos-secret": settings.SHARED_SECRET}
_GOWA = settings.WA_BACKEND == "gowa"


class UnroutableMessage(RuntimeError):
    """No device to send by. Deliberately fatal rather than falling back.

    A fallback device is how "Pharmacy B's customer got a reply from Pharmacy A"
    happens, so an unpaired or unknown pharmacy must fail loudly at compose time.
    """


def compose(pharmacy_id, phone: str, msg_type: str, body: str,
            media_path: str | None = None) -> int:
    """Record an outbound message and the device it must leave by. Returns its id.

    The device is resolved HERE, not at send time. Everything deferred -- cron alerts,
    SLA escalations, retries -- runs with no request context, so a sender that resolves
    the device itself has nothing correct to resolve from.

    Safety gates are checked HERE, before the row is written. A GateBlocked exception
    means the message is too risky for WhatsApp compliance and must not be sent.

    """
    phone = norm_phone(phone)
    if not phone:
        raise UnroutableMessage("no destination phone")

    ph = q1("""select wa_jid, gowa_device_id from pharmacies where id = %s""",
            (str(pharmacy_id),))
    if not ph:
        raise UnroutableMessage(f"unknown pharmacy {pharmacy_id}")
    if not ph["gowa_device_id"]:
        raise UnroutableMessage(
            f"pharmacy {pharmacy_id} has no paired device; refusing to guess one")

    # ---- safety gates ----
    check_gates(phone, str(pharmacy_id), ph["gowa_device_id"])

    row = q1("""insert into wa_messages
                  (pharmacy_id, direction, to_phone, msg_type, body, media_path,
                   gowa_device_id, expected_wa_jid, status, handled)
                values (%s,'out',%s,%s,%s,%s,%s,%s,'queued',true)
                returning id""",
             (str(pharmacy_id), phone, msg_type, (body or "")[:4000], media_path,
              ph["gowa_device_id"], ph["wa_jid"]))
    return row["id"]


def _gowa_kwargs(slot: str | None = None) -> dict:
    """Basic auth, plus device scoping when a slot is given.

    `slot` is passed in from the outbound record. There is deliberately no fallback to a
    configured default: with more than one paired number, a default device is precisely
    how one pharmacy's customer receives another pharmacy's reply.
    """
    kw: dict = {}
    if settings.GOWA_USER:
        kw["auth"] = (settings.GOWA_USER, settings.GOWA_PASS)
    if slot:
        kw["headers"] = {"X-Device-Id": slot}
    return kw


_slots_cache: dict = {"at": 0.0, "by_id": {}}
_SLOT_TTL = 60.0


def _slots(force: bool = False) -> dict:
    """GOWA's live slot table, {slot_id: jid}. Cached ~60s.

    Uncached, this would add a round trip to every single send. Cached without a forced
    refresh path, a stale entry would cause false refusals immediately after a legitimate
    re-pair -- hence `force`.

    Raises on transport failure. Callers must treat that as "unverified", never as "fine".
    """
    import time
    if not force and _slots_cache["by_id"] and (time.time() - _slots_cache["at"]) < _SLOT_TTL:
        return _slots_cache["by_id"]
    kw = _gowa_kwargs()
    kw.pop("headers", None)          # slot listing is not itself device-scoped
    r = httpx.get(f"{settings.GOWA_URL.rstrip('/')}/devices", timeout=15, **kw)
    r.raise_for_status()
    by_id = {s.get("id"): (s.get("jid") or "")
             for s in ((r.json() or {}).get("results") or [])}
    _slots_cache.update(at=time.time(), by_id=by_id)
    return by_id


def deliver(row_id: int) -> bool:
    """Send a composed message using the device recorded ON THE ROW.

    Reads nothing from ambient context, so a cron job, a retry and a webhook reply all
    behave identically.
    """
    row = q1("""select id, to_phone, msg_type, body, media_path, gowa_device_id,
                       expected_wa_jid, attempts
                  from wa_messages where id = %s""", (row_id,))
    if not row:
        raise UnroutableMessage(f"no outbound row {row_id}")

    slot, expected = row["gowa_device_id"], row["expected_wa_jid"]

    if _GOWA:
        try:
            live = _slots().get(slot)
            if live != expected:
                live = _slots(force=True).get(slot)   # tolerate a stale cache after re-pair
        except Exception as e:
            # Unverified is not the same as wrong. Leave it queued for retry rather than
            # sending from a device we could not confirm -- misdelivery is unrecoverable,
            # a delayed message is not.
            ex("""update wa_messages set attempts = attempts + 1, last_error = %s,
                         status = 'queued' where id = %s""",
               (f"could not verify device: {e}"[:500], row_id))
            log.warning("deliver %s deferred: GOWA /devices unreachable", row_id)
            return False

        if live != expected:
            ex("""update wa_messages set status='refused', attempts = attempts + 1,
                         last_error = %s where id = %s""",
               (f"slot {slot} jid is {live or '(none)'}, expected {expected}"[:500],
                row_id))
            log.error("deliver %s REFUSED: slot %s holds %s, expected %s",
                      row_id, slot, live or "(none)", expected)
            return False

    try:
        _send_for(row, slot)
    except Exception as e:
        ex("""update wa_messages set status='failed', attempts = attempts + 1,
                     last_error = %s, error = %s where id = %s""",
           (str(e)[:500], str(e)[:500], row_id))
        log.exception("deliver %s failed", row_id)
        return False

    ex("""update wa_messages set status='sent', attempts = attempts + 1,
                 last_error = null where id = %s""", (row_id,))
    return True


def _send_for(row: dict, slot: str) -> None:
    """Dispatch one recorded row to the transport, scoped to `slot`."""
    phone, body = row["to_phone"], row["body"] or ""
    kind = row["msg_type"]
    if kind == "document":
        _post("/send/file", "/send-document",
              {"phone": phone, "file_url": row["media_path"], "caption": body},
              {"to": phone, "url": row["media_path"], "filename": body,
               "caption": body}, 120, slot)
    elif kind == "image":
        _post("/send/image", "/send-image",
              {"phone": phone, "image_url": row["media_path"], "caption": body},
              {"to": phone, "url": row["media_path"], "caption": body}, 120, slot)
    else:
        _post("/send/message", "/send",
              {"phone": phone, "message": body},
              {"to": phone, "text": body}, 30, slot)


def _post(path_gowa: str, path_baileys: str, gowa_json: dict, baileys_json: dict,
          timeout: int, slot: str | None = None) -> None:
    if _GOWA:
        r = httpx.post(f"{settings.GOWA_URL.rstrip('/')}{path_gowa}",
                       json=gowa_json, timeout=timeout, **_gowa_kwargs(slot))
    else:
        r = httpx.post(f"{settings.WA_GATEWAY_URL.rstrip('/')}{path_baileys}",
                       json=baileys_json, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()


def send_for(pharmacy_id, phone: str, msg_type: str, body: str,
             media_path: str | None = None) -> bool:
    """Compose then deliver, for callers that know their tenant. The target API."""
    return deliver(compose(pharmacy_id, phone, msg_type, body, media_path))


# --------------------------------------------------------- transitional wrappers
# These keep the pre-multitenant signature `send_text(phone, body)` working for the 105
# existing call sites across nine modules. They resolve the tenant from
# settings.PHARMACY_ID, which item #4 removes -- at which point every caller passes its
# own pharmacy_id and these wrappers are deleted.
#
# This is a transitional path, not a fallback: it is pinned to the single-tenant constant
# that still exists rather than picking a device when the answer is unknown. It is already
# strictly safer than what it replaces, because the device now comes from the pharmacy row
# and goes through the slot/JID guard instead of an env var nobody re-checked.
def send_text(phone: str, body: str) -> None:
    try:
        send_for(settings.PHARMACY_ID, phone, "text", body)
    except (UnroutableMessage, GateBlocked) as e:
        log.warning("send_text blocked to %s: %s", phone, e)


def send_document(phone: str, url: str, filename: str, caption: str = "") -> None:
    """Transport fetches the URL itself — keeps big files off this process."""
    # GOWA has no filename field for the *_url path; it derives one from the URL, so the
    # human-readable name rides along in the caption.
    try:
        send_for(settings.PHARMACY_ID, phone, "document", caption or filename, url)
    except (UnroutableMessage, GateBlocked) as e:
        log.warning("send_document blocked to %s: %s", phone, e)


def send_image(phone: str, url: str, caption: str = "") -> None:
    """Used to forward a prescription photo to the pharmacist's own phone, where
    native pinch-zoom beats anything we would build in a dashboard."""
    try:
        send_for(settings.PHARMACY_ID, phone, "image", caption, url)
    except (UnroutableMessage, GateBlocked) as e:
        log.warning("send_image blocked to %s: %s", phone, e)


# --------------------------------------------------------- reply wrappers
# Named for the caller's intent, and kept because router.py reads better with them. They
# do NOT bypass any gate: Gate 3 opens because handle_inbound recorded the inbound before
# dispatching, which is evidence rather than an assertion by the sender.
def reply_text(phone: str, body: str) -> None:
    try:
        send_for(settings.PHARMACY_ID, phone, "text", body)
    except (UnroutableMessage, GateBlocked) as e:
        log.warning("reply_text blocked to %s: %s", phone, e)


def reply_document(phone: str, url: str, filename: str, caption: str = "") -> None:
    try:
        send_for(settings.PHARMACY_ID, phone, "document", caption or filename, url)
    except (UnroutableMessage, GateBlocked) as e:
        log.warning("reply_document blocked to %s: %s", phone, e)


def reply_image(phone: str, url: str, caption: str = "") -> None:
    try:
        send_for(settings.PHARMACY_ID, phone, "image", caption, url)
    except (UnroutableMessage, GateBlocked) as e:
        log.warning("reply_image blocked to %s: %s", phone, e)


def _pause() -> None:
    """Randomised gap between bulk sends. Patched out in tests."""
    import random
    import time
    time.sleep(random.uniform(settings.WA_PACE_MIN_SECS, settings.WA_PACE_MAX_SECS))


def broadcast(pharmacy_id, phones: list[str], body: str) -> dict:
    """Send the same message to several recipients, from one pharmacy's device.

    This is the most ban-prone operation in the system, so it is deliberately
    inconvenient:

      * Every recipient goes through the same gates as a single send. Being on a list
        someone assembled is not consent.
      * A batch larger than WA_BROADCAST_MAX is REFUSED, not truncated. Truncation
        reads as success while the tail is silently never delivered.
      * The gap between sends is randomised. The previous version slept exactly 1.5s,
        which is both too fast and a bot signature in itself -- no human sends on a
        metronome.
      * A blocked recipient is skipped and counted, not fatal: one stranger in a list
        of managers must not stop the rest.

    Returns {"sent", "blocked", "failed"} so the caller can report honestly rather than
    assuming everything went out.
    """
    if len(phones) > settings.WA_BROADCAST_MAX:
        raise UnroutableMessage(
            f"{len(phones)} recipients exceeds WA_BROADCAST_MAX="
            f"{settings.WA_BROADCAST_MAX}; split it or raise the cap deliberately")

    out = {"sent": 0, "blocked": 0, "failed": 0}
    for i, p in enumerate(phones):
        if i:
            _pause()
        try:
            ok = deliver(compose(pharmacy_id, p, "text", body))
            out["sent" if ok else "failed"] += 1
        except GateBlocked as e:
            out["blocked"] += 1
            log.warning("broadcast skipped %s: %s", p, e)
        except UnroutableMessage as e:
            out["failed"] += 1
            log.warning("broadcast could not route %s: %s", p, e)
    log.info("broadcast done: %s", out)
    return out


def gowa_fetch_media(rel_path: str) -> bytes | None:
    """Pull an inbound media file off GOWA.

    With WHATSAPP_AUTO_DOWNLOAD_MEDIA on (its default) the webhook carries a path
    like 'statics/media/1752404751-uuid.jpeg' rather than bytes. GOWA serves that
    tree at /statics behind the same basic auth.
    """
    base = settings.GOWA_URL.rstrip("/")
    rel = str(rel_path or "").lstrip("/")
    if not rel:
        return None
    try:
        kw = _gowa_kwargs()
        kw.pop("headers", None)          # device header is meaningless for a static GET
        r = httpx.get(f"{base}/{rel}", timeout=120, follow_redirects=True, **kw)
        r.raise_for_status()
        return r.content
    except Exception:
        log.exception("could not fetch GOWA media %s", rel)
        return None

