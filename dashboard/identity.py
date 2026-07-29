"""Per-user identity: WhatsApp sign-in codes backed by Supabase Auth.

THE SPLIT THAT MATTERS

  staff.phone        a communication channel. Changes when someone gets a new SIM.
  auth.users.id      WHO they are. Permanent. Survives phone, email and role changes.
  staff row          what that person may do in ONE pharmacy.

A 6-digit code sent over WhatsApp proves possession of the phone. Supabase Auth then
supplies the durable identity that a phone number cannot: Phase 2's RLS policies key
on auth.uid(), and that has to keep working after someone's phone is stolen.

WHY NOT EMAIL OR GOOGLE (yet)

Both need configuration nobody has done -- SMTP for Supabase email, a Google Cloud
project and consent screen for OAuth -- and many pharmacy staff have neither a work
email nor a Google account. Every one of them has WhatsApp, and staff.phone is already
the access list. Google stays available as a later addition, not a blocker now.

No email is ever sent: admin.generate_link mints a token without delivering it, and
verify_otp redeems it server-side. That avoids the usual Streamlit problem where
Supabase returns tokens in a URL fragment the server cannot read.

FAIL-SAFE

AUTH_MODE controls this, and the default is deliberately the old behaviour:
  shared     one DASHBOARD_PASSWORD, exactly as before  (default)
  whatsapp   per-user codes, shared password still accepted as a break-glass
  strict     per-user codes only
Nobody gets locked out of a working pharmacy because an auth migration half-landed.
"""
import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone

log = logging.getLogger(__name__)

CODE_TTL_MIN = 10
MAX_ATTEMPTS = 5
LOCKOUT_MIN = 15
AUTH_MODE = os.getenv("AUTH_MODE", "shared").lower()


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


# ============================================================ sending a code
def send_code(phone_raw: str, q, ex) -> tuple[bool, str]:
    """Look up the number, mint a code, WhatsApp it. Returns (ok, message).

    The message is deliberately identical whether or not the number is registered.
    Telling an unknown caller "that number is not a staff member" turns the sign-in
    box into a tool for discovering who works at the pharmacy.
    """
    from onboarding import norm_phone
    phone = norm_phone(phone_raw)
    generic = ("If that number belongs to a staff member, a 6-digit code is on its "
               "way to it on WhatsApp.")
    if not phone:
        return False, "Enter the WhatsApp number you were registered with."

    s = q("""select id, pharmacy_id, name, is_active, login_locked_until
               from staff where phone=%s""", (phone,))
    if not s:
        log.info("login code requested for unknown number %s", phone)
        return True, generic
    s = s[0]

    if not s["is_active"]:
        log.info("login code requested for deactivated staff %s", s["id"])
        return True, generic          # same message; do not confirm the account exists

    if s["login_locked_until"] and s["login_locked_until"] > _now():
        mins = int((s["login_locked_until"] - _now()).total_seconds() // 60) + 1
        return False, f"Too many wrong codes. Try again in {mins} minute(s)."

    ex("select purge_expired_login_codes()")

    code = f"{secrets.randbelow(1_000_000):06d}"
    ex("""insert into login_codes (staff_id, code_hash, expires_at, sent_to)
          values (%s,%s, now() + interval '%s minutes', %s)""",
       (s["id"], _hash(code), CODE_TTL_MIN, phone))

    import sys
    api = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api")
    if api not in sys.path:
        sys.path.insert(0, api)
    from wa import reply_text
    reply_text(phone,
              f"*{code}* is your Pharma OS sign-in code.\n\n"
              f"It expires in {CODE_TTL_MIN} minutes. If you did not try to sign in, "
              f"ignore this message and tell the owner.")
    log.info("login code sent to staff %s", s["id"])
    return True, generic


# ============================================================ verifying a code
def verify_code(phone_raw: str, code: str, q, ex) -> tuple[dict | None, str]:
    """Returns (staff_row, message). staff_row is None on failure."""
    from onboarding import norm_phone
    phone = norm_phone(phone_raw)
    # `phone` is in the select on purpose: _ensure_identity() needs it to synthesise
    # the auth.users handle. Omitting it made identity linking fail silently behind
    # that function's own try/except -- sign-in worked, the link never happened.
    s = q("""select id, pharmacy_id, name, role, phone, ppb_reg_no, is_active,
                    supabase_user_id, display_email, login_locked_until,
                    login_failed_count
               from staff where phone=%s""", (phone,))
    if not s or not s[0]["is_active"]:
        return None, "That code is not valid."
    s = s[0]

    if s["login_locked_until"] and s["login_locked_until"] > _now():
        mins = int((s["login_locked_until"] - _now()).total_seconds() // 60) + 1
        return None, f"Too many wrong codes. Try again in {mins} minute(s)."

    row = q("""select id, code_hash, expires_at, attempts from login_codes
                where staff_id=%s and used_at is null
                order by created_at desc limit 1""", (s["id"],))
    if not row:
        return None, "No code was requested for that number. Send one first."
    row = row[0]

    if row["expires_at"] < _now():
        return None, "That code has expired. Request a new one."

    if (row["attempts"] or 0) >= MAX_ATTEMPTS:
        return None, "That code has been tried too many times. Request a new one."

    if not secrets.compare_digest(_hash(str(code).strip()), row["code_hash"]):
        ex("update login_codes set attempts=attempts+1 where id=%s", (row["id"],))
        fails = (s["login_failed_count"] or 0) + 1
        if fails >= MAX_ATTEMPTS:
            ex("""update staff set login_failed_count=0,
                      login_locked_until = now() + interval '%s minutes'
                    where id=%s""", (LOCKOUT_MIN, s["id"]))
            _log_login(ex, s, "whatsapp", False, "lockout")
            return None, f"Too many wrong codes. Locked for {LOCKOUT_MIN} minutes."
        ex("update staff set login_failed_count=%s where id=%s", (fails, s["id"]))
        _log_login(ex, s, "whatsapp", False, "wrong code")
        return None, f"Wrong code. {MAX_ATTEMPTS - fails} attempt(s) left."

    # Correct. Burn the code before doing anything else, so a replay in another tab
    # cannot use it while Supabase is still being called.
    ex("update login_codes set used_at=now() where id=%s", (row["id"],))
    ex("""update staff set login_failed_count=0, login_locked_until=null,
              last_login_at=now() where id=%s""", (s["id"],))

    _ensure_identity(s, q, ex)
    _log_login(ex, s, "whatsapp", True, None)
    return dict(s), ""


# ============================================================ Supabase identity
def _ensure_identity(staff: dict, q, ex) -> None:
    """Create or attach the permanent auth.users identity.

    Deliberately non-fatal. If Supabase Auth is unreachable, the person has still
    proved possession of a whitelisted WhatsApp number and should be let in to run
    the pharmacy. Identity is bookkeeping for Phase 2; it is not the gate today.
    """
    if staff.get("supabase_user_id"):
        return
    try:
        import sys
        api = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api")
        if api not in sys.path:
            sys.path.insert(0, api)
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"],
                           os.environ["SUPABASE_SERVICE_KEY"])

        # A staff member may have no email at all, so synthesise a stable one from the
        # phone. It is never used for delivery -- it exists so auth.users has the
        # unique handle it requires. A real address can be attached later without
        # changing the identity.
        email = (staff.get("display_email") or "").strip().lower() \
            or f"{staff['phone']}@wa.pharmaos.local"

        uid = None
        for u in sb.auth.admin.list_users():
            if (u.email or "").lower() == email:
                uid = u.id
                break
        if not uid:
            created = sb.auth.admin.create_user({
                "email": email,
                "email_confirm": True,
                "phone": staff["phone"],
                "user_metadata": {"name": staff["name"], "pharma_os": True},
            })
            uid = created.user.id

        ex("""update staff set supabase_user_id=%s, accepted_at=coalesce(accepted_at, now())
                where id=%s""", (uid, staff["id"]))
        staff["supabase_user_id"] = uid
        log.info("linked staff %s to auth user %s", staff["id"], uid)
    except Exception:
        log.warning("could not link Supabase identity for staff %s "
                    "(sign-in still succeeded)", staff["id"], exc_info=True)


def _log_login(ex, staff: dict, method: str, ok: bool, reason: str | None) -> None:
    try:
        ex("""insert into login_events (staff_id, pharmacy_id, method, success,
                    failure_reason) values (%s,%s,%s,%s,%s)""",
           (staff["id"], staff["pharmacy_id"], method, ok, reason))
    except Exception:
        log.debug("login event not recorded", exc_info=True)


def log_shared_password_login(ex, pharmacy_id: str | None, ok: bool) -> None:
    try:
        ex("""insert into login_events (pharmacy_id, method, success, failure_reason)
              values (%s,'shared_password',%s,%s)""",
           (pharmacy_id, ok, None if ok else "wrong password"))
    except Exception:
        log.debug("login event not recorded", exc_info=True)
