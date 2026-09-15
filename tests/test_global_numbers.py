"""Global phone numbers: a customer writing from ANY country must be routable
and replyable exactly as WhatsApp delivered them.

The system was born Kenyan and norm_phone() showed it: everything that did not
look like a Safaricom number was passed through unvalidated -- fine -- but the
"starts with 0" branch rewrote 00-prefixed international numbers into phantom
Kenyan ones, and nothing anywhere proved that an Indian/American/European
sender actually gets a reply. A friend of the owner texting from three
continents and hearing silence is the failure this file pins.

Three layers, each cheap:

* norm_phone()/pretty_phone()/is_valid_phone() -- pure functions, no database
* the GOWA webhook entry point -- the from-field for a foreign JID survives
  into handle_inbound untouched
* the full router pipeline -- a foreign customer on a tenant device receives
  the consent message, can consent, and gets a deterministic answer (POINTS),
  with the reply addressed to their unmangled E.164 number

The LLM is deliberately never called: keywords only, so these tests fail on
phone handling, never on a model's mood.
"""
import hashlib
import hmac
import json
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


# ------------------------------------------------------------------ norm_phone
@pytest.mark.parametrize("raw,expected", [
    # Kenya -- the home formats, all must collapse to one identity
    ("0713755274", "254713755274"),
    ("713755274", "254713755274"),
    ("+254 713 755 274", "254713755274"),
    ("254713755274", "254713755274"),
    ("254713755274@s.whatsapp.net", "254713755274"),
    ("+254254713755274", "254713755274"),          # double country code
    # India
    ("+91 98765 43210", "919876543210"),
    ("919876543210", "919876543210"),
    ("919876543210@s.whatsapp.net", "919876543210"),
    ("0091 98765 43210", "919876543210"),           # 00 international prefix
    # USA / Canada
    ("+1 415 555 2671", "14155552671"),
    ("14155552671", "14155552671"),
    ("001 415 555 2671", "14155552671"),
    # Europe
    ("+44 7700 900123", "447700900123"),            # UK
    ("+49 151 2345 6789", "4915123456789"),         # Germany
    ("+33 6 12 34 56 78", "33612345678"),           # France
    ("+27 71 234 5678", "27712345678"),             # South Africa
    ("+254 20 345678", "25420345678"),              # KE landline, country-coded
    # Garbage stays garbage, visibly
    ("", ""),
    ("not-a-phone", ""),
])
def test_norm_phone_global(raw, expected):
    from utils import norm_phone
    assert norm_phone(raw) == expected, (
        f"{raw!r} normalised to {norm_phone(raw)!r}, expected {expected!r} -- "
        "a foreign number must never be rewritten into another country's")


def test_norm_phone_never_invents_a_kenyan_from_foreign():
    """The 00-prefix bug: '0044…' used to become '254044…', a VALID-LOOKING
    Kenyan number. The reply would have gone to a stranger in Britain and the
    real sender to silence."""
    from utils import norm_phone
    assert norm_phone("00447700900123") == "447700900123"
    assert not norm_phone("00447700900123").startswith("254")


def test_is_valid_phone():
    from utils import is_valid_phone
    assert is_valid_phone("+919876543210")
    assert is_valid_phone("+14155552671")
    assert is_valid_phone("+447700900123")
    assert is_valid_phone("0713755274")
    assert not is_valid_phone("not-a-phone")
    assert not is_valid_phone("")


def test_pretty_phone_does_not_misgroup_foreign():
    """An Indian number is 12 digits, exactly like a Kenyan one -- grouping it
    with the Kenyan template prints +919 876 543 210, which reads as a
    misformatted number to the person who owns it."""
    from utils import pretty_phone
    assert pretty_phone("0713755274") == "+254 713 755 274"
    assert pretty_phone("+919876543210") == "+919876543210"
    assert pretty_phone("+14155552671") == "+14155552671"


# ------------------------------------------------------------------ webhook entry
def _client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def _signed(body: dict, secret: str):
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": f"sha256={sig}",
                 "content-type": "application/json"}


@pytest.mark.parametrize("jid", [
    "919876543210@s.whatsapp.net",   # India
    "14155552671@s.whatsapp.net",    # USA
    "447700900123@s.whatsapp.net",   # UK
])
def test_gowa_webhook_passes_foreign_sender_through(jid, monkeypatch):
    """The production entry point for foreign traffic: whatever GOWA put in
    `from` must arrive at handle_inbound as bare E.164 digits, unchanged."""
    event = {
        "event": "message",
        "device_id": "254712345678@s.whatsapp.net",
        "payload": {
            "id": f"FOREIGN-{secrets.token_hex(4)}",
            "chat_id": jid,
            "from": jid,
            "from_name": "Abroad",
            "is_from_me": False,
            "body": "do you have panadol?",
        },
    }
    seen = []
    import router
    monkeypatch.setattr(router, "handle_inbound", lambda b: seen.append(b))
    import main
    monkeypatch.setattr(main, "handle_inbound", lambda b: seen.append(b))
    import tenancy
    from tenancy import Resolution
    monkeypatch.setattr(tenancy, "resolve", lambda **kw: Resolution("unknown"))

    from config import settings
    raw, headers = _signed(event, settings.GOWA_WEBHOOK_SECRET)
    r = _client().post("/webhook/gowa", content=raw, headers=headers)
    assert r.status_code == 200, r.text
    assert seen, "handle_inbound was never called"
    assert seen[0]["from"] == jid.split("@")[0], (
        "the foreign sender's number was rewritten before the router ran")


# ------------------------------------------------------------------ full pipeline
@pytest.fixture
def pharmacy():
    """A live-looking tenant: active, with a device slot and JID, so wa.compose()
    will build rows for it (nothing is ever delivered -- no GOWA slot by this
    name exists). Same pattern as test_prices_briefings.shop."""
    import tenancy
    from db import ex, ex1

    mark = secrets.token_hex(3)
    dig = f"{int(mark, 16) % 10**7:07d}"
    ph = ex1("""insert into pharmacies (name, kind, status, wa_jid, gowa_device_id)
                values (%s,'tenant','active',%s,%s) returning id""",
             (f"GLOB-{mark}", f"25479{mark[:6]}@s.whatsapp.net", f"glob-{mark}"))
    pid = str(ph["id"])
    yield {"pid": pid, "dig": dig}
    for t in ("inbound_history", "wa_messages", "wa_state", "customers"):
        ex(f"delete from {t} where pharmacy_id=%s", (pid,))
    ex("delete from pharmacies where id=%s", (pid,))


FOREIGN_CUSTOMERS = [
    ("919876543210", "+91 98765 43210"),    # India
    ("14155552671", "+1 415 555 2671"),      # USA
    ("447700900123", "+44 7700 900123"),     # UK
]


def _last_out(pid: str, phone: str) -> str:
    from db import q
    rows = q("""select body from wa_messages where pharmacy_id=%s
                 and to_phone=%s and direction='out'
                 order by created_at desc limit 1""", (pid, phone))
    return (rows[0]["body"] or "") if rows else ""


def _send(pharmacy, phone: str, text: str) -> None:
    """One inbound message on the tenant's device, as webhook_gowa would build it."""
    import router
    router.handle_inbound({
        "wa_id": f"glob-{secrets.token_hex(6)}",
        "from": phone,
        "type": "text",
        "text": text,
        "device_kind": "tenant",
        "pharmacy_id": pharmacy["pid"],
    })


@pytest.mark.parametrize("phone,pretty", FOREIGN_CUSTOMERS, ids=["india", "usa", "uk"])
@db
def test_foreign_customer_gets_consent_then_answers(pharmacy, phone, pretty):
    """The exact journey the owner's friends tried: text the pharmacy's number
    from abroad, get the consent message, say YES, then ask something and get
    a deterministic answer -- addressed to the unmangled foreign number."""
    from db import q1

    # 1. first contact -> consent ask (no LLM, keyword-free path)
    _send(pharmacy, phone, "hello")
    out = _last_out(pharmacy["pid"], phone)
    assert "Reply *YES* to continue" in out, (
        f"first contact from {pretty} got {out!r} -- the friend heard silence")

    # the customer row exists under the foreign number, tenant-scoped
    cust = q1("select * from customers where pharmacy_id=%s and phone=%s",
              (pharmacy["pid"], phone))
    assert cust is not None, "no customer row was created for the foreign number"
    assert not cust["consent_given"]

    # 2. consent
    _send(pharmacy, phone, "YES")
    out = _last_out(pharmacy["pid"], phone)
    assert "Thank you" in out
    cust = q1("select * from customers where pharmacy_id=%s and phone=%s",
              (pharmacy["pid"], phone))
    assert cust["consent_given"], "consent was never recorded for the foreign number"

    # 3. a deterministic enquiry answer -- proves the reply path end to end
    _send(pharmacy, phone, "POINTS")
    out = _last_out(pharmacy["pid"], phone)
    assert "loyalty points" in out, f"POINTS from {pretty} got {out!r}"


@db
def test_foreign_number_is_never_rewritten_in_transit(pharmacy):
    """Every stored row must carry the foreign E.164 number unchanged -- an
    inbound from India is logged as 919876543210, replied to as 919876543210,
    and never becomes a 254-prefixed stranger."""
    from db import q

    phone = "919876543210"
    _send(pharmacy, phone, "hello")
    _send(pharmacy, phone, "YES")

    rows = q("""select direction, from_phone, to_phone from wa_messages
                 where pharmacy_id=%s and (from_phone=%s or to_phone=%s)""",
             (pharmacy["pid"], phone, phone))
    assert rows, "no message rows were recorded for the foreign number"
    for r in rows:
        assert r["from_phone"] in (phone, None)
        assert r["to_phone"] in (phone, None)
    # and nothing was sent to a made-up Kenyan number derived from it
    kenyan = q("""select id from wa_messages where pharmacy_id=%s
                   and (from_phone like '254%%' or to_phone like '254%%')
                   and coalesce(from_phone,'') <> %s and coalesce(to_phone,'') <> %s""",
               (pharmacy["pid"], phone, phone))
    assert not kenyan, "a message was addressed to a Kenyan-format number"
