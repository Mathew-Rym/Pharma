"""Small pure functions. These are the bits most likely to bite you in production,
so they live alone and are unit-testable.
"""
import calendar
import re
from datetime import date, datetime


# ------------------------------------------------------------------ phone
def norm_phone(raw: str) -> str:
    """Normalise ANY phone number to bare E.164 digits (no plus).

    Kenyan local formats are expanded to the country code:

    0713755274        -> 254713755274
    713755274         -> 254713755274
    +254 713 755 274  -> 254713755274
    254713755274@s.whatsapp.net -> 254713755274

    Every other country passes through unchanged, so a customer writing from
    India, the USA or Europe is addressable exactly as WhatsApp delivered them:

    +91 98765 43210   -> 919876543210
    +1 415 555 2671   -> 14155552671
    +44 7700 900123   -> 447700900123
    0091 98765 43210  -> 919876543210      (00 international prefix)

    This is the "Kenya is home, everywhere else is a guest" rule: a number with
    an explicit foreign country code is never rewritten, because guessing a
    country code for it would invent a number belonging to somebody else. A
    10-digit string beginning 7 or 1 is returned UNCHANGED, deliberately. It is
    not a valid Kenyan format -- mobiles are 9 significant digits, optionally
    with a leading 0 -- so there is no way to know which digit is spurious. An
    earlier version dropped the first one ("7137552744" -> "254137552744"),
    which does not fail: it produces a real, validating number belonging to
    somebody else. The message then reaches a stranger who never contacted the
    pharmacy, which is exactly what gets a WhatsApp number reported and banned.
    Leave it malformed and let the caller's validity check reject it.
    """
    if not raw:
        return ""
    s = raw.split("@")[0]                      # strip WhatsApp JID suffix
    s = re.sub(r"\D", "", s)                   # digits only
    if s.startswith("00"):
        # 00 is the international dialling prefix (0091… == +91…). Strip it
        # BEFORE the Kenyan leading-zero rule, or "0044…" would become
        # "254044…" -- a Kenyan number that reaches a stranger in Britain.
        s = s[2:]
    if s.startswith("0"):
        # A single leading 0 with no country code is the LOCAL convention, and
        # local here is Kenyan. Foreign senders always arrive with their
        # country code (WhatsApp normalises to E.164), so this branch is only
        # ever taken for numbers typed the Kenyan way.
        s = "254" + s[1:]
    elif (s.startswith("7") or s.startswith("1")) and len(s) == 9:
        s = "254" + s                          # 713755274
    elif s.startswith("254254"):
        s = s[3:]                              # double country code from "+254 2547…"
    # Anything else is already E.164-shaped with a foreign country code → keep as-is
    return s


def is_valid_phone(phone: str) -> bool:
    """Check if a normalised phone looks like a valid E.164 number, any country.

    E.164 allows 7-15 significant digits after the country code and never a
    leading zero. Used for numbers whose country we do not control (customers
    writing from anywhere). Kenyan-specific rules stay in is_valid_ke_mobile.
    """
    p = norm_phone(phone)
    return bool(p) and 7 <= len(p) <= 15 and not p.startswith("0")


def is_valid_ke_mobile(phone: str) -> bool:
    """Check if a normalised phone looks like a valid Kenyan mobile number.

    Valid Kenyan mobiles are 12 digits starting with 2547 or 2541.
    """
    p = norm_phone(phone)
    return bool(p) and len(p) == 12 and p.startswith("254") and p[3] in "17"


def pretty_phone(p: str) -> str:
    p = norm_phone(p)
    if len(p) == 12 and p.startswith("254"):
        return f"+{p[:3]} {p[3:6]} {p[6:9]} {p[9:]}"
    return f"+{p}" if p else p


# ------------------------------------------------------------------ units
def to_pieces(qty_whole: int | None, qty_pieces: int | None, pack_size: int) -> int:
    """phAMACore / supplier notation: '5W0P' = 5 whole packs, 0 loose pieces.

    We store everything in pieces internally so arithmetic is never ambiguous.
    """
    w = int(qty_whole or 0)
    p = int(qty_pieces or 0)
    return w * max(int(pack_size or 1), 1) + p


def from_pieces(pieces: int, pack_size: int) -> str:
    """Render pieces back into the notation the pharmacy staff actually read."""
    ps = max(int(pack_size or 1), 1)
    w, p = divmod(int(pieces or 0), ps)
    return f"{w}W{p}P"


WP_RE = re.compile(r"^\s*(\d+)\s*[wW]\s*(\d+)?\s*[pP]?\s*$")


def parse_wp(text: str) -> tuple[int, int] | None:
    """Parse a staff-typed quantity like '2W', '2W5P', '3w0p' -> (whole, pieces)."""
    m = WP_RE.match(text or "")
    if not m:
        if (text or "").strip().isdigit():
            return int(text.strip()), 0
        return None
    return int(m.group(1)), int(m.group(2) or 0)


# ------------------------------------------------------------------ dates
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_expiry(raw: str | None) -> date | None:
    """Supplier expiry dates are month-precision and inconsistently formatted.

    Accepts: 01/2028, 2027-08, Jul-28, 07/28, 2028-01-31, 03/2030
    Returns the LAST day of that month — a batch marked 01/2028 is good through Jan 31.
    Returns None rather than guessing when the string is unusable.
    """
    if not raw:
        return None
    s = str(raw).strip().replace(".", "/").replace(" ", "")

    # full ISO date
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = map(int, m.groups())
        return date(y, mo, min(d, calendar.monthrange(y, mo)[1]))

    # YYYY-MM or YYYY/MM
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return _eom(y, mo)

    # MM/YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{4})$", s)
    if m:
        mo, y = int(m.group(1)), int(m.group(2))
        return _eom(y, mo)

    # MM/YY
    m = re.match(r"^(\d{1,2})[-/](\d{2})$", s)
    if m:
        mo, yy = int(m.group(1)), int(m.group(2))
        return _eom(2000 + yy, mo)

    # Jul-28 / Jul-2028
    m = re.match(r"^([A-Za-z]{3,})[-/](\d{2,4})$", s)
    if m:
        mo = _MONTHS.get(m.group(1)[:3].lower())
        if mo:
            yy = int(m.group(2))
            y = yy if yy > 100 else 2000 + yy
            return _eom(y, mo)

    return None


def _eom(y: int, mo: int) -> date | None:
    if not (1 <= mo <= 12) or not (2000 <= y <= 2099):
        return None
    return date(y, mo, calendar.monthrange(y, mo)[1])


def parse_date_loose(raw: str | None) -> date | None:
    """For invoice/prescription dates where day precision exists."""
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d %b %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ money
def kes(amount) -> str:
    try:
        return f"KES {float(amount):,.2f}"
    except (TypeError, ValueError):
        return "KES 0.00"


# ------------------------------------------------------------------ approval PINs
def hash_pin(pin: str) -> str:
    """sha-256 hex of an approval PIN.

    Matches dashboard/identity.py's _hash for login codes deliberately, rather than
    introducing a second scheme: two hashing conventions in one codebase is how one of them
    ends up unmaintained.

    The PIN was stored and compared in PLAINTEXT (`str(pin).strip() ==
    str(staff["approval_pin"]).strip()`). Anyone with read access to the staff table could
    approve a prescription-only medicine as a named pharmacist, against that pharmacist's
    PPB registration number -- and the POM approval log, which is the regulatory record,
    would show a valid approval. That makes the audit trail forgeable, which is worse than
    it being absent.

    A 4-6 digit PIN is trivially brute-forced from a hash, so this is not confidentiality
    against an attacker with the table -- the lockout counter is what limits guessing
    online. What it does remove is the ability to read a working PIN straight out of a
    backup, a log, or a support query, which is the realistic exposure.
    """
    import hashlib
    return hashlib.sha256(str(pin).strip().encode()).hexdigest()
