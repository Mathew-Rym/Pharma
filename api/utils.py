"""Small pure functions. These are the bits most likely to bite you in production,
so they live alone and are unit-testable.
"""
import calendar
import re
from datetime import date, datetime


# ------------------------------------------------------------------ phone
def norm_phone(raw: str) -> str:
    """Normalise any Kenyan format to bare E.164 digits: 2547XXXXXXXX.

    0713755274        -> 254713755274
    +254 713 755 274  -> 254713755274
    254713755274@s.whatsapp.net -> 254713755274

    A 10-digit string beginning 7 or 1 is returned UNCHANGED, deliberately. It is not a
    valid Kenyan format -- mobiles are 9 significant digits, optionally with a leading 0
    -- so there is no way to know which digit is spurious. An earlier version dropped the
    first one ("7137552744" -> "254137552744"), which does not fail: it produces a real,
    validating number belonging to somebody else. The message then reaches a stranger who
    never contacted the pharmacy, which is exactly what gets a WhatsApp number reported
    and banned. Leave it malformed and let is_valid_ke_mobile() reject it.
    """
    if not raw:
        return ""
    s = raw.split("@")[0]                      # strip WhatsApp JID suffix
    s = re.sub(r"\D", "", s)                   # digits only
    if s.startswith("0"):
        s = "254" + s[1:]
    elif (s.startswith("7") or s.startswith("1")) and len(s) == 9:
        s = "254" + s                          # 713755274
    elif s.startswith("254254"):
        s = s[3:]
    # Already 12 digits starting with 254 → keep as-is
    return s


def is_valid_ke_mobile(phone: str) -> bool:
    """Check if a normalised phone looks like a valid Kenyan mobile number.

    Valid Kenyan mobiles are 12 digits starting with 2547 or 2541.
    """
    p = norm_phone(phone)
    return bool(p) and len(p) == 12 and p.startswith("254") and p[3] in "17"


def pretty_phone(p: str) -> str:
    p = norm_phone(p)
    return f"+{p[:3]} {p[3:6]} {p[6:9]} {p[9:]}" if len(p) == 12 else p


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
