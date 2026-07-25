"""Outbound WhatsApp. The only place that knows how to reach the gateway.

Swap this file (and nothing else) when you migrate from Baileys to the official
Meta Cloud API.
"""
import logging

import httpx

from config import settings
from db import ex
from utils import norm_phone

log = logging.getLogger(__name__)
_HEADERS = {"x-dishii-secret": settings.SHARED_SECRET}


def send_text(phone: str, body: str) -> None:
    phone = norm_phone(phone)
    if not phone:
        return
    try:
        r = httpx.post(
            f"{settings.WA_GATEWAY_URL}/send",
            json={"to": phone, "text": body},
            headers=_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        _log_out(phone, "text", body, None)
    except Exception as e:
        log.exception("send_text failed to %s", phone)
        _log_out(phone, "text", body, str(e))


def send_document(phone: str, url: str, filename: str, caption: str = "") -> None:
    """Gateway fetches the URL itself and streams it — keeps big files off this process."""
    phone = norm_phone(phone)
    if not phone:
        return
    try:
        r = httpx.post(
            f"{settings.WA_GATEWAY_URL}/send-document",
            json={"to": phone, "url": url, "filename": filename, "caption": caption},
            headers=_HEADERS,
            timeout=120,
        )
        r.raise_for_status()
        _log_out(phone, "document", f"{filename} {caption}", None)
    except Exception as e:
        log.exception("send_document failed to %s", phone)
        _log_out(phone, "document", filename, str(e))


def broadcast(phones: list[str], body: str) -> None:
    """Deliberately sequential and unthrottled-by-design-choice.

    Baileys will get the number banned if you fan out concurrently. Keep pilot
    volumes low and let this be slow.
    """
    import time
    for p in phones:
        send_text(p, body)
        time.sleep(1.5)


def _log_out(phone: str, msg_type: str, body: str, error: str | None) -> None:
    ex(
        """insert into wa_messages
             (pharmacy_id, direction, to_phone, msg_type, body, error, handled)
           values (%s,'out',%s,%s,%s,%s,true)""",
        (settings.PHARMACY_ID, phone, msg_type, (body or "")[:4000], error),
    )
