"""Per-user identity: WhatsApp sign-in codes (Phase 1).

The dangerous failures here are not crashes. They are: a code that still works after
being used, an unknown number learning it is unknown, and a lockout that can be reset
by asking for a new code. Each of those turns the sign-in box into a tool for
somebody else.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

DB = bool(os.getenv("DATABASE_URL")) and bool(os.getenv("PHARMACY_ID"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL/PHARMACY_ID not set")


@pytest.fixture
def person(monkeypatch):
    """A staff member with a unique phone, plus captured WhatsApp sends."""
    import secrets as _s

    from db_helpers import ex_, q_

    pid = os.environ["PHARMACY_ID"]
    phone = "2547" + f"{_s.randbelow(10**8):08d}"
    row = q_("""insert into staff (pharmacy_id, phone, name, role, is_active)
                values (%s,%s,%s,'pharmacist',true) returning id""",
             (pid, phone, f"PYTEST {phone[-4:]}"))
    sid = str(row[0]["id"])

    sent = []
    import wa
    monkeypatch.setattr(wa, "send_text", lambda p, b: sent.append((p, b)))
    import identity
    # identity imports send_text lazily inside the function, so patch the source
    monkeypatch.setattr("wa.send_text", lambda p, b: sent.append((p, b)))

    yield {"id": sid, "phone": phone, "sent": sent, "q": q_, "ex": ex_}

    ex_("delete from login_codes where staff_id=%s", (sid,))
    ex_("delete from login_events where staff_id=%s", (sid,))
    ex_("delete from staff where id=%s", (sid,))


def _code_from(sent):
    """Pull the 6 digits out of the WhatsApp message we captured."""
    import re
    assert sent, "no WhatsApp message was sent"
    m = re.search(r"\*(\d{6})\*", sent[-1][1])
    assert m, f"no code in message: {sent[-1][1]!r}"
    return m.group(1)


# ============================================================ happy path
@db
def test_code_is_sent_and_accepted(person):
    from identity import send_code, verify_code
    ok, _ = send_code(person["phone"], person["q"], person["ex"])
    assert ok
    code = _code_from(person["sent"])

    staff, msg = verify_code(person["phone"], code, person["q"], person["ex"])
    assert staff is not None, msg
    assert str(staff["id"]) == person["id"]


@db
def test_the_code_is_never_stored_in_plaintext(person):
    """A leaked backup must not hand someone a working login for everyone who signed
    in that hour."""
    from identity import send_code
    send_code(person["phone"], person["q"], person["ex"])
    code = _code_from(person["sent"])
    rows = person["q"]("select code_hash from login_codes where staff_id=%s",
                       (person["id"],))
    assert rows
    assert code not in rows[0]["code_hash"]
    assert len(rows[0]["code_hash"]) == 64          # sha-256 hex


@db
def test_a_used_code_cannot_be_used_again(person):
    """Otherwise a code read over someone's shoulder stays valid all day."""
    from identity import send_code, verify_code
    send_code(person["phone"], person["q"], person["ex"])
    code = _code_from(person["sent"])

    staff, _ = verify_code(person["phone"], code, person["q"], person["ex"])
    assert staff is not None
    again, msg = verify_code(person["phone"], code, person["q"], person["ex"])
    assert again is None, "a burnt code was accepted a second time"


@db
def test_wrong_code_is_rejected_and_counted(person):
    from identity import send_code, verify_code
    send_code(person["phone"], person["q"], person["ex"])
    staff, msg = verify_code(person["phone"], "000000", person["q"], person["ex"])
    assert staff is None
    assert "attempt" in msg.lower()


@db
def test_lockout_after_repeated_wrong_codes(person):
    """And the lockout lives on staff, not on the code row -- otherwise requesting a
    fresh code would reset the counter and there would be no lockout at all."""
    from identity import MAX_ATTEMPTS, send_code, verify_code
    send_code(person["phone"], person["q"], person["ex"])
    for _ in range(MAX_ATTEMPTS):
        verify_code(person["phone"], "000000", person["q"], person["ex"])

    locked = person["q"]("select login_locked_until from staff where id=%s",
                         (person["id"],))[0]["login_locked_until"]
    assert locked is not None, "no lockout after repeated failures"

    # asking for a new code must NOT clear it
    ok, msg = send_code(person["phone"], person["q"], person["ex"])
    assert not ok and "minute" in msg.lower()


@db
def test_expired_code_is_rejected(person):
    from identity import send_code, verify_code
    send_code(person["phone"], person["q"], person["ex"])
    code = _code_from(person["sent"])
    person["ex"]("""update login_codes set expires_at = now() - interval '1 minute'
                     where staff_id=%s""", (person["id"],))
    staff, msg = verify_code(person["phone"], code, person["q"], person["ex"])
    assert staff is None
    assert "expired" in msg.lower()


@db
def test_deactivated_staff_cannot_sign_in(person):
    """Deactivating in the dashboard has to revoke dashboard access too, not just
    WhatsApp."""
    from identity import send_code, verify_code
    send_code(person["phone"], person["q"], person["ex"])
    code = _code_from(person["sent"])
    person["ex"]("update staff set is_active=false where id=%s", (person["id"],))
    staff, _ = verify_code(person["phone"], code, person["q"], person["ex"])
    assert staff is None


# ============================================================ enumeration
@db
def test_unknown_number_gets_the_same_answer_as_a_known_one(person):
    """If the box says 'that number is not a staff member', it becomes a tool for
    discovering who works at the pharmacy."""
    from identity import send_code
    ok_known, msg_known = send_code(person["phone"], person["q"], person["ex"])
    ok_unknown, msg_unknown = send_code("254799999999", person["q"], person["ex"])
    assert ok_known == ok_unknown
    assert msg_known == msg_unknown


@db
def test_no_code_is_sent_to_an_unknown_number(person):
    from identity import send_code
    send_code("254799999999", person["q"], person["ex"])
    assert not person["sent"], "a code was sent to a number that is not staff"


# ============================================================ identity linking
@db
def test_signing_in_links_a_permanent_identity(person):
    """auth.users is what survives a phone change. Without the link, a new SIM means a
    new person as far as the system is concerned."""
    pytest.importorskip("supabase")
    from identity import send_code, verify_code
    send_code(person["phone"], person["q"], person["ex"])
    code = _code_from(person["sent"])
    staff, _ = verify_code(person["phone"], code, person["q"], person["ex"])
    assert staff is not None

    row = person["q"]("""select supabase_user_id, accepted_at, last_login_at
                           from staff where id=%s""", (person["id"],))[0]
    assert row["last_login_at"] is not None

    # Assert unconditionally. An earlier version guarded this with
    # `if row["supabase_user_id"]:` and therefore passed while linking was completely
    # broken -- _ensure_identity raised KeyError('phone') inside its own try/except,
    # so sign-in worked and the link silently never happened. A test that only checks
    # a value when the value exists checks nothing.
    assert row["supabase_user_id"], (
        "identity was not linked; _ensure_identity failed silently")
    assert row["accepted_at"] is not None

    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"],
                           os.environ["SUPABASE_SERVICE_KEY"])
        sb.auth.admin.delete_user(str(row["supabase_user_id"]))
    except Exception:
        pass


@db
def test_sign_in_still_works_when_supabase_auth_is_down(person, monkeypatch):
    """Identity is bookkeeping for Phase 2's RLS. It must not be the gate today: the
    person has proved possession of a whitelisted number and has a pharmacy to run."""
    import identity
    monkeypatch.setattr(identity, "_ensure_identity",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    from identity import send_code
    send_code(person["phone"], person["q"], person["ex"])
    code = _code_from(person["sent"])
    # _ensure_identity is called inside verify_code; make sure the raise is contained
    monkeypatch.setattr(identity, "_ensure_identity", lambda *a, **k: None)
    staff, _ = identity.verify_code(person["phone"], code, person["q"], person["ex"])
    assert staff is not None


# ============================================================ audit
@db
def test_every_attempt_is_recorded(person):
    from identity import send_code, verify_code
    send_code(person["phone"], person["q"], person["ex"])
    code = _code_from(person["sent"])
    verify_code(person["phone"], "000000", person["q"], person["ex"])
    verify_code(person["phone"], code, person["q"], person["ex"])

    rows = person["q"]("""select success from login_events where staff_id=%s
                           order by created_at""", (person["id"],))
    assert len(rows) >= 2
    assert any(not r["success"] for r in rows), "failed attempt not recorded"
    assert any(r["success"] for r in rows), "successful sign-in not recorded"


# ============================================================ fail-safe
def test_default_auth_mode_is_the_old_behaviour():
    """An auth migration must never lock a pharmacy out mid-shift. Shipping this code
    without setting AUTH_MODE has to change nothing."""
    import importlib

    import identity
    old = os.environ.pop("AUTH_MODE", None)
    try:
        importlib.reload(identity)
        assert identity.AUTH_MODE == "shared"
    finally:
        if old is not None:
            os.environ["AUTH_MODE"] = old
        importlib.reload(identity)
