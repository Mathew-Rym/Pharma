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
from db import ex
from utils import norm_phone

log = logging.getLogger(__name__)
_HEADERS = {"x-dishii-secret": settings.SHARED_SECRET}
_GOWA = settings.WA_BACKEND == "gowa"


def _gowa_kwargs() -> dict:
    """Basic auth + device scoping for every GOWA call."""
    kw: dict = {}
    if settings.GOWA_USER:
        kw["auth"] = (settings.GOWA_USER, settings.GOWA_PASS)
    if settings.GOWA_DEVICE_ID:
        kw["headers"] = {"X-Device-Id": settings.GOWA_DEVICE_ID}
    return kw


def _post(path_gowa: str, path_baileys: str, gowa_json: dict, baileys_json: dict,
          timeout: int) -> None:
    if _GOWA:
        r = httpx.post(f"{settings.GOWA_URL.rstrip('/')}{path_gowa}",
                       json=gowa_json, timeout=timeout, **_gowa_kwargs())
    else:
        r = httpx.post(f"{settings.WA_GATEWAY_URL.rstrip('/')}{path_baileys}",
                       json=baileys_json, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()


def send_text(phone: str, body: str) -> None:
    phone = norm_phone(phone)
    if not phone:
        return
    try:
        _post("/send/message", "/send",
              {"phone": phone, "message": body},
              {"to": phone, "text": body}, 30)
        _log_out(phone, "text", body, None)
    except Exception as e:
        log.exception("send_text failed to %s", phone)
        _log_out(phone, "text", body, str(e))


def send_document(phone: str, url: str, filename: str, caption: str = "") -> None:
    """Transport fetches the URL itself — keeps big files off this process."""
    phone = norm_phone(phone)
    if not phone:
        return
    try:
        # GOWA has no filename field for the *_url path; it derives one from the URL,
        # so the human-readable name rides along in the caption.
        _post("/send/file", "/send-document",
              {"phone": phone, "file_url": url,
               "caption": caption or filename},
              {"to": phone, "url": url, "filename": filename, "caption": caption}, 120)
        _log_out(phone, "document", f"{filename} {caption}", None)
    except Exception as e:
        log.exception("send_document failed to %s", phone)
        _log_out(phone, "document", filename, str(e))


def send_image(phone: str, url: str, caption: str = "") -> None:
    """Used to forward a prescription photo to the pharmacist's own phone, where
    native pinch-zoom beats anything we would build in a dashboard."""
    phone = norm_phone(phone)
    if not phone:
        return
    try:
        _post("/send/image", "/send-image",
              {"phone": phone, "image_url": url, "caption": caption},
              {"to": phone, "url": url, "caption": caption}, 120)
        _log_out(phone, "image", caption, None)
    except Exception as e:
        log.exception("send_image failed to %s", phone)
        _log_out(phone, "image", caption, str(e))


def broadcast(phones: list[str], body: str) -> None:
    """Deliberately sequential and unthrottled-by-design-choice.

    Both backends drive a real WhatsApp Web session, and fanning out concurrently is
    how the number gets banned. Keep pilot volumes low and let this be slow.
    """
    import time
    for p in phones:
        send_text(p, body)
        time.sleep(1.5)


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


def _log_out(phone: str, msg_type: str, body: str, error: str | None) -> None:
    ex(
        """insert into wa_messages
             (pharmacy_id, direction, to_phone, msg_type, body, error, handled)
           values (%s,'out',%s,%s,%s,%s,true)""",
        (settings.PHARMACY_ID, phone, msg_type, (body or "")[:4000], error),
    )
