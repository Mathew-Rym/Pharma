"""GOWA transport tests.

The webhook is an unauthenticated-by-URL entry point that can drive stock movements
(a staff 'OK' approves a GRN). Its HMAC check is therefore load-bearing, and GOWA's
own default secret is the literal string "secret" -- so these tests exist mostly to
make sure the signature check cannot regress into a no-op.
"""
import hashlib
import hmac
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

TEXT_EVENT = {
    "event": "message",
    "device_id": "254712345678@s.whatsapp.net",
    "payload": {
        "id": "3EB0C127D7BACC83D6A1",
        "chat_id": "254700000001@s.whatsapp.net",
        "from": "254700000001@s.whatsapp.net",
        "from_name": "Vivian",
        "timestamp": "2026-07-27T10:30:00Z",
        "is_from_me": False,
        "body": "EXPIRY",
    },
}


def _client():
    from fastapi.testclient import TestClient

    import main
    return TestClient(main.app)


def _signed(body: dict, secret: str) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": f"sha256={sig}",
                 "content-type": "application/json"}


@pytest.fixture
def secret():
    from config import settings
    return settings.GOWA_WEBHOOK_SECRET


def test_correctly_signed_message_is_accepted(secret, monkeypatch):
    import router
    seen = []
    monkeypatch.setattr(router, "handle_inbound", lambda b: seen.append(b))
    import main
    monkeypatch.setattr(main, "handle_inbound", lambda b: seen.append(b))

    raw, headers = _signed(TEXT_EVENT, secret)
    r = _client().post("/webhook/gowa", content=raw, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert seen, "handle_inbound was never called"
    assert seen[0]["from"] == "254700000001"
    assert seen[0]["text"] == "EXPIRY"
    assert seen[0]["type"] == "text"


def test_unsigned_request_is_rejected():
    """Anyone who can reach the URL must not be able to inject a staff command."""
    r = _client().post("/webhook/gowa", json=TEXT_EVENT)
    assert r.status_code == 401


def test_wrong_signature_is_rejected(secret):
    raw, headers = _signed(TEXT_EVENT, secret + "-wrong")
    r = _client().post("/webhook/gowa", content=raw, headers=headers)
    assert r.status_code == 401


def test_tampered_body_is_rejected(secret):
    """Signature is over the raw body, so editing the message must invalidate it."""
    raw, headers = _signed(TEXT_EVENT, secret)
    tampered = raw.replace(b"EXPIRY", b"CANCEL")
    assert len(tampered) == len(raw)          # same length, only the content differs
    r = _client().post("/webhook/gowa", content=tampered, headers=headers)
    assert r.status_code == 401


def test_our_own_outbound_messages_are_ignored(secret, monkeypatch):
    """Without this the bot answers itself in a loop."""
    seen = []
    import main
    monkeypatch.setattr(main, "handle_inbound", lambda b: seen.append(b))
    event = json.loads(json.dumps(TEXT_EVENT))
    event["payload"]["is_from_me"] = True
    raw, headers = _signed(event, secret)
    r = _client().post("/webhook/gowa", content=raw, headers=headers)
    assert r.status_code == 200
    assert r.json()["ignored"] == "own message"
    assert not seen


def test_group_messages_are_ignored(secret, monkeypatch):
    """Otherwise any member of any group the number joins could drive the pharmacy."""
    seen = []
    import main
    monkeypatch.setattr(main, "handle_inbound", lambda b: seen.append(b))
    event = json.loads(json.dumps(TEXT_EVENT))
    event["payload"]["chat_id"] = "120363000000000000@g.us"
    raw, headers = _signed(event, secret)
    r = _client().post("/webhook/gowa", content=raw, headers=headers)
    assert r.status_code == 200
    assert r.json()["ignored"] == "group"
    assert not seen


def test_non_message_events_are_ignored(secret, monkeypatch):
    """Read receipts and typing indicators must not reach the router."""
    seen = []
    import main
    monkeypatch.setattr(main, "handle_inbound", lambda b: seen.append(b))
    for ev in ("message.ack", "chat_presence", "call.offer"):
        event = json.loads(json.dumps(TEXT_EVENT))
        event["event"] = ev
        raw, headers = _signed(event, secret)
        r = _client().post("/webhook/gowa", content=raw, headers=headers)
        assert r.status_code == 200
        assert r.json()["ignored"] == ev
    assert not seen


# ============================================================ media shapes
@pytest.mark.parametrize("payload,expected", [
    ({"image": "statics/media/1-a.jpeg"}, ("image", "statics/media/1-a.jpeg")),
    ({"image": {"path": "statics/media/2-b.jpeg", "caption": "invoice"}},
     ("image", "statics/media/2-b.jpeg")),
    ({"image": {"url": "https://x/y.jpeg"}}, ("image", "https://x/y.jpeg")),
    ({"document": {"path": "statics/media/3-c.pdf"}},
     ("document", "statics/media/3-c.pdf")),
    ({"body": "just text"}, None),
    ({}, None),
])
def test_every_documented_media_shape_is_understood(payload, expected):
    """GOWA changes this shape with auto-download and caption presence: bare string,
    {path,caption}, or {url}. Missing one silently downgrades an invoice photo to a
    text message, and Loop A never starts."""
    from main import _gowa_media_path
    assert _gowa_media_path(payload) == expected


# ============================================================ send adapter
def test_send_text_targets_the_gowa_endpoint(monkeypatch):
    """The two backends use different paths and body keys; a mix-up is a silent
    404 that only shows up as 'the pharmacist never got the message'."""
    import wa
    calls = []

    class _R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(wa, "_GOWA", True)
    monkeypatch.setattr(wa.httpx, "post",
                        lambda url, **kw: calls.append((url, kw)) or _R())
    monkeypatch.setattr(wa, "_log_out", lambda *a, **k: None)
    wa.send_text("0713755274", "hello")

    url, kw = calls[0]
    assert url.endswith("/send/message")
    assert kw["json"] == {"phone": "254713755274", "message": "hello"}


def test_send_image_uses_image_url_not_a_file_upload(monkeypatch):
    """GOWA tolerates a missing file when image_url is set, which lets signed URLs
    stay out of this process. Sending the wrong key silently drops the image."""
    import wa
    calls = []

    class _R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(wa, "_GOWA", True)
    monkeypatch.setattr(wa.httpx, "post",
                        lambda url, **kw: calls.append((url, kw)) or _R())
    monkeypatch.setattr(wa, "_log_out", lambda *a, **k: None)
    wa.send_image("254713755274", "https://signed/rx.jpg", "Prescription")

    url, kw = calls[0]
    assert url.endswith("/send/image")
    assert kw["json"]["image_url"] == "https://signed/rx.jpg"
    assert "image" not in kw["json"]


def test_baileys_backend_still_uses_the_old_shape(monkeypatch):
    """The pilot may run either transport; the old one must not break."""
    import wa
    calls = []

    class _R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(wa, "_GOWA", False)
    monkeypatch.setattr(wa.httpx, "post",
                        lambda url, **kw: calls.append((url, kw)) or _R())
    monkeypatch.setattr(wa, "_log_out", lambda *a, **k: None)
    wa.send_text("254713755274", "hello")

    url, kw = calls[0]
    assert url.endswith("/send")
    assert kw["json"] == {"to": "254713755274", "text": "hello"}
