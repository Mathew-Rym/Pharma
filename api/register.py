"""Self-service onboarding over WhatsApp: REGISTER, JOIN and OWNER.

Three things happen here, and they are the only paths in the system where a phone we have
never seen before receives a reply:

  REGISTER        an owner creates a pharmacy and gets the 8-character code that links
                  the pharmacy's handset to our gateway
  OWNER <code>    a manager attaches themselves to an existing pharmacy
  JOIN <code>     an attendant does the same, with less authority

Two design rules hold this together.

**The conversation is a pure function.** `_step()` takes (flow, message, answers so far)
and returns (next flow, answers, reply). It touches no database, no network and no model.
Registration has to work when Gemini is rate-limited, because a pharmacy that cannot sign
up is revenue we never see -- and because a slot-filling model that "helpfully" invents a
licence number would put fiction in a regulated field.

**The handset is asked for, never inferred.** A pharmacy's `wa_jid` is the WhatsApp
account the bot runs on. The obvious shortcut -- use the number that texted REGISTER --
is wrong twice over: it puts every customer conversation inside the owner's personal
WhatsApp, and a ban then takes out their own messaging along with the shop's. So the flow
asks which handset the shop will use, and the JID itself is read back from GOWA after that
handset links. It is never taken from a message.
"""
import logging
import re
import secrets

import provision
import tenancy
from db import ex, q, q1
from safety import GateBlocked, record_inbound
from state import clear_state, get_state, set_state
from utils import is_valid_ke_mobile, norm_phone
from wa import UnroutableMessage, compose, deliver

log = logging.getLogger(__name__)

START = "reg_owner_name"
PROVISION = "reg_provision"          # a sentinel, never stored: "the caller must act now"
_TTL_MIN = 120                       # registration is a form, not a command; be patient

# Ambiguous glyphs removed. These codes are read down a phone line and typed on a handset,
# where O/0 and I/1/L are the classic transcription failures -- and a mistyped code is
# indistinguishable from a broken system to the person typing it.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

_CODE_RE = re.compile(rf"^[{_ALPHABET}]{{8}}$")


def new_code(n: int = 8) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


# ------------------------------------------------------------------ recognising intent
def wants_registration(text: str) -> bool:
    """True only when REGISTER is the FIRST WORD.

    Not `startswith`: "registered" is ordinary English a customer may well send to a
    pharmacy, and matching it would drag them into an onboarding flow they never asked
    for -- and out of the conversation they did.
    """
    first = re.split(r"[\s.,!?]+", (text or "").strip(), maxsplit=1)[0]
    return first.upper() == "REGISTER"


def parse_redemption(text: str) -> tuple[str, str] | None:
    """('join'|'owner', CODE) or None. The code must be code-shaped, so "JOIN the queue"
    stays an ordinary message rather than becoming a failed redemption."""
    parts = (text or "").strip().split()
    if len(parts) != 2:
        return None
    kind, code = parts[0].upper(), parts[1].upper()
    if kind not in ("JOIN", "OWNER") or not _CODE_RE.match(code):
        return None
    return kind.lower(), code


def in_progress(phone: str) -> bool:
    return (get_state(phone).get("flow") or "").startswith("reg_")


# ------------------------------------------------------------------ the conversation
_QUESTIONS = {
    "reg_owner_name": "Welcome to Pharma OS 👋\n\nLet's get your pharmacy set up. It "
                      "takes about a minute.\n\nFirst — what is *your* name?",
    "reg_name": "Thanks {owner_name}. What is the pharmacy's registered name?",
    "reg_licence": "What is the PPB premises licence number for *{name}*?\n\n"
                   "(As it appears on the certificate, e.g. PPB/12345/2024)",
    "reg_town": "Which town or estate is *{name}* in?",
    "reg_wa": "Last one. Which WhatsApp number will the pharmacy use for its bot?\n\n"
              "This must be a phone you can pick up right now — you'll type an "
              "8-character code into it.\n\n⚠️ Use the *shop's* line, not your personal "
              "number. Every customer message lands in that WhatsApp.",
}

_ORDER = ["reg_owner_name", "reg_name", "reg_licence", "reg_town", "reg_wa"]
_FIELD = {"reg_owner_name": "owner_name", "reg_name": "name", "reg_licence": "licence",
          "reg_town": "town", "reg_wa": "wa"}


def _validate(flow: str, text: str, ctx: dict) -> tuple[str | None, str | None]:
    """(clean value, complaint). Exactly one is None.

    Rejecting is cheap; advancing on a bad answer is not. A mistyped handset number sends
    a pairing code to a stranger and invites them to link a device to our gateway.
    """
    t = (text or "").strip()
    if flow == "reg_wa":
        p = norm_phone(t)
        if not is_valid_ke_mobile(p):
            return None, ("That doesn't look like a Kenyan mobile number. Send it as "
                          "*0712 345 678* or *254712345678*.")
        # REFUSED, not warned. The first real registration answered this question with the
        # owner's own number, and the warning printed directly above it was not enough.
        #
        # When the bot line and the owner's line are one account, WhatsApp marks the
        # owner's messages is_from_me and main.webhook_gowa drops them before anything
        # runs -- so the owner can never use their own pharmacy's bot. Worse, every
        # personal contact who messages them is processed as pharmacy traffic: two real
        # people were auto-created as customers within minutes.
        #
        # Compared on the NORMALISED form, so 0720…, 254720… and +254 720 … are all the
        # same number. Comparing raw text would let it through on a leading zero.
        if ctx.get("sender") and p == norm_phone(ctx["sender"]):
            return None, ("That's the number you're texting me from, so it can't also be "
                          "the shop's bot.\n\nIf it were, WhatsApp would treat your own "
                          "messages as the bot's and you'd never be able to use it — and "
                          "everyone who messages you personally would reach the pharmacy.\n\n"
                          "Send a *different* number for the shop's line.")
        return p, None

    if flow == "reg_licence":
        if len(t) < 4 or not re.search(r"\d", t):
            return None, ("That doesn't look like a licence number. Send it as it "
                          "appears on the certificate, e.g. *PPB/12345/2024*.")
        return t[:60], None

    minimum = {"reg_owner_name": 2, "reg_name": 3, "reg_town": 2}[flow]
    label = {"reg_owner_name": "your name", "reg_name": "the pharmacy name",
             "reg_town": "the town or estate"}[flow]
    if len(t) < minimum:
        return None, f"That's a bit short for {label}. Please send it again."
    if len(t) > 120:
        # Not truncated: 400 characters in the name field is a mis-sent message, and
        # trimming it would file the first 120 characters of somebody's paragraph as a
        # pharmacy's registered name.
        return None, f"That's too long for {label}. Please send just {label}."
    return t, None


def _restart_ctx(ctx: dict) -> dict:
    """A fresh context that still remembers who is asking.

    `sender` is not an answer, it is the identity of the person answering, so it survives
    a restart. Every other key is an answer and must not.
    """
    return {"sender": ctx["sender"]} if ctx.get("sender") else {}


def _summary(ctx: dict) -> str:
    return ("Please check this:\n\n"
            f"🏥 *{ctx['name']}*\n"
            f"📋 Licence: {ctx['licence']}\n"
            f"📍 {ctx['town']}\n"
            f"👤 Owner: {ctx['owner_name']}\n"
            f"📱 Bot line: +{ctx['wa']}\n\n"
            "Reply *YES* to create it, or *EDIT* to start again.")


def _step(flow: str, text: str, ctx: dict) -> tuple[str, dict, str]:
    """One turn of the conversation. Pure: no database, no network, no model."""
    t = (text or "").strip()

    if flow == "reg_confirm":
        if t.upper() in ("YES", "Y", "CONFIRM", "OK", "SAWA", "NDIO"):
            return PROVISION, ctx, ""
        if t.upper() in ("EDIT", "NO", "N", "CHANGE", "RESTART"):
            # Everything is cleared EXCEPT who is asking. Wiping the sender too would
            # silently disable the own-number check for the rest of the conversation, so
            # the one path most likely to be taken by someone correcting that exact
            # mistake would be the one path that no longer catches it.
            return START, _restart_ctx(ctx), ("No problem, let's start again.\n\n"
                                              + _QUESTIONS[START])
        # Anything else re-asks. Falling through to creation on an ambiguous answer would
        # create a real pharmacy from a message that was never a confirmation.
        return "reg_confirm", ctx, _summary(ctx)

    if flow not in _FIELD:
        return START, _restart_ctx(ctx), _QUESTIONS[START]

    value, complaint = _validate(flow, t, ctx)
    if complaint:
        return flow, ctx, complaint

    ctx = {**ctx, _FIELD[flow]: value}
    nxt = _ORDER[_ORDER.index(flow) + 1] if flow != _ORDER[-1] else "reg_confirm"
    return nxt, ctx, (_summary(ctx) if nxt == "reg_confirm"
                      else _QUESTIONS[nxt].format(**ctx))


# ------------------------------------------------------------------ the platform line
def platform_pid() -> str | None:
    """The pharmacy row representing US -- the line that answers strangers.

    Registration replies have to leave by some device, and by definition the pharmacy
    being registered has none yet. That is what a platform row is for.
    """
    row = q1("select id from pharmacies where kind = 'platform' order by created_at limit 1")
    return str(row["id"]) if row else None


def onboarding_line(msg: dict) -> str | None:
    """Which pharmacy answers this stranger.

    A dedicated platform line is the right answer and the one to aim for: onboarding
    traffic is bursts of messages to numbers with no history, which is the exact pattern
    WhatsApp bans for, and a platform row keeps that exposure off the pharmacies' own
    numbers.

    But a pilot with a single SIM has no such row, and refusing outright would mean
    self-service registration simply does not exist until a second number is bought. So we
    fall back to the pharmacy that owns the device the message arrived on: the stranger
    texted that number, so answering from it is at least not a cold contact. The tenant
    carries the risk, which is why it is logged as a warning every time and why
    `./run.sh platform <slot>` exists to end it.
    """
    p = platform_pid()
    if p:
        return p
    fallback = msg.get("pharmacy_id")
    if fallback:
        log.warning("no kind='platform' pharmacy: answering onboarding from tenant %s. "
                    "Run ./run.sh platform <slot> once a dedicated line is paired.",
                    fallback)
        return str(fallback)
    return None


def _make_contactable(phone: str, pharmacy_id: str) -> None:
    """Let `pharmacy_id` reply to this phone, for as long as the registration is live.

    This is the "onboarding flow" escape hatch Gate 3's own error message points at, and
    it is narrow on purpose: it only ever runs for a phone that has *just sent us a
    message*, so nothing here lets us contact someone who did not start the conversation.

    The row goes in onboarding_contacts, NOT customers, and this table is NOT redundant
    now that resolve_by_sender() has stopped reading customers. Two independent reasons,
    and only the first has changed:

      1. (historical) resolve_by_sender() used to read customers, so the row was an
         identity signal and the registering owner stayed pinned to the answering pharmacy
         forever. That specific mechanism is gone.
      2. (still true) a customers row is PERMANENT and unscoped, while this grant is meant
         to expire -- an abandoned registration must not leave a stranger messageable for
         good. And when the answering line is a tenant rather than a dedicated platform
         number, a customers row would file everyone registering some OTHER pharmacy into
         that pharmacy's customer list.

    So do not "simplify" this back to a customers row on the strength of reason 1 alone.
    """
    ex("""insert into onboarding_contacts (pharmacy_id, phone)
          values (%s,%s)
          on conflict (pharmacy_id, phone) do update set created_at = now()""",
       (pharmacy_id, phone))
    record_inbound(phone, pharmacy_id)


def _say(phone: str, body: str, pharmacy_id: str) -> bool:
    """Send one message, from a named pharmacy, through the ordinary gated path.

    Deliberately routed through wa.compose()/deliver() rather than posting to GOWA
    directly. Onboarding is exactly the traffic pattern that gets numbers banned -- bursts
    to numbers with no history -- so it must be rate-limited and recorded like everything
    else, not slipped past the machinery built to prevent that.
    """
    try:
        return deliver(compose(pharmacy_id, phone, "text", body))
    except (GateBlocked, UnroutableMessage) as e:
        log.error("onboarding reply to %s from %s blocked: %s", phone, pharmacy_id, e)
        return False


# ------------------------------------------------------------------ creating a pharmacy
def _new_pharmacy_columns(ctx: dict, owner_phone: str) -> dict:
    """The row REGISTER writes. Split out so it can be asserted on without a database.

    `wa_jid` is absent, not empty: it is unknown until the handset links, and the moment
    it acquires a placeholder, tenancy.resolve() starts matching inbound messages against
    a pharmacy that cannot receive them.
    """
    return {
        "name": ctx["name"],
        "ppb_licence": ctx["licence"],
        "address": ctx["town"],
        "wa_number": ctx["wa"],
        "owner_phone": owner_phone,
        "kind": "tenant",
        "status": "pending_activation",
        "timezone": "Africa/Nairobi",
        "gowa_device_id": provision.slot_for(ctx["wa"]),
        "join_code": None,          # filled by _unique_code, which needs the database
        "owner_code": None,
    }


def _unique_code(column: str) -> str:
    for _ in range(20):
        code = new_code()
        if not q1(f"select 1 from pharmacies where {column} = %s", (code,)):
            return code
    raise RuntimeError(f"could not allocate a unique {column}")


def _provision(phone: str, ctx: dict, platform: str) -> None:
    """Create the pharmacy, its owner, its codes -- then ask WhatsApp for a link code.

    Order matters. The database work happens first and is allowed to fail loudly; the GOWA
    call happens last and is allowed to fail *softly*, because a gateway hiccup must not
    lose a completed registration. If the code cannot be issued now, the pharmacy still
    exists and `./run.sh pair` (or a retry of REGISTER) can finish the job.
    """
    dup = q1("select id, name from pharmacies where wa_number = %s", (ctx["wa"],))
    if dup:
        _say(phone, f"+{ctx['wa']} is already registered to *{dup['name']}*.\n\n"
                    "If that's your pharmacy, ask the owner for a JOIN code. "
                    "Otherwise reply *REGISTER* and use a different number.", platform)
        clear_state(phone)
        return

    cols = _new_pharmacy_columns(ctx, owner_phone=phone)
    cols["join_code"] = _unique_code("join_code")
    cols["owner_code"] = _unique_code("owner_code")

    keys = list(cols)
    row = q1(f"""insert into pharmacies ({', '.join(keys)})
                 values ({', '.join(['%s'] * len(keys))}) returning id""",
             tuple(cols[k] for k in keys))
    pid = str(row["id"])

    # Through _grant_role so the owner's role lands in the audit trail like every other
    # role change. 'owner' is the top rank, so effective_role() can only ever grant it.
    _grant_role(pid, phone, "owner", mechanism="register", name=ctx["owner_name"])

    # DELIBERATELY no record_inbound() against the new pharmacy.
    #
    # An earlier version did exactly that, reasoning that the owner "must be reachable on
    # their own pharmacy's line the moment it comes up". That reasoning was wrong, and the
    # comment made it sound principled. The owner messaged the PLATFORM line; they have
    # never messaged the pharmacy that was just created. Writing inbound_history for them
    # against the tenant invents a conversation, which is the same class of error as the
    # schema_v10 backfill that made 254746294224 reachable and cost this project a WhatsApp
    # sending restriction.
    #
    # Activation confirmations therefore go out over the platform line, where the owner's
    # conversation genuinely lives -- see activation_sweep(). The confirmation itself tells
    # them to text HELP to the new number, and THAT inbound opens Gate 3 honestly.
    clear_state(phone)
    log.info("registered pharmacy %s (%s) for owner %s", cols["name"], pid, phone)

    try:
        code = provision.pair_code(cols["gowa_device_id"], ctx["wa"])
    except provision.ProvisionError as e:
        log.error("pairing code for %s failed: %s", cols["name"], e)
        _say(phone, f"*{cols['name']}* is registered ✅\n\n"
                    "I couldn't reach WhatsApp for the link code just now. Reply "
                    "*CODE* in a few minutes and I'll try again.\n\n"
                    f"Your codes to keep:\n"
                    f"👤 Managers: OWNER {cols['owner_code']}\n"
                    f"🧑‍💼 Attendants: JOIN {cols['join_code']}", platform)
        return

    _say(phone, _activation_message(cols, code), platform)


def _activation_message(cols: dict, code: str) -> str:
    return (f"*{cols['name']}* is registered ✅\n\n"
            f"*Link the shop's phone now*\n"
            f"On the handset for +{cols['wa_number']}:\n"
            f"1. WhatsApp → Settings → Linked devices\n"
            f"2. Link a device → *Link with phone number instead*\n"
            f"3. Enter this code:\n\n"
            f"      *{code}*\n\n"
            f"It expires in a few minutes. Reply *CODE* for a fresh one.\n\n"
            f"───────────\n"
            f"*Keep these — they're how your team joins:*\n"
            f"👤 Managers text: *OWNER {cols['owner_code']}*\n"
            f"🧑‍💼 Attendants text: *JOIN {cols['join_code']}*\n\n"
            f"They must text it to +{cols['wa_number']} once it's linked.")


def _resend_code(phone: str, platform: str) -> None:
    """`CODE` — reissue the link code for the pharmacy this owner registered.

    Pairing codes expire quickly and the shop phone is often in another room, so a retry
    path is not a nicety. Restricted to a pharmacy still waiting to be linked, so it
    cannot be used to force a re-pair of a live one.
    """
    ph = q1("""select name, wa_number, gowa_device_id, join_code, owner_code
                 from pharmacies
                where owner_phone = %s and status = 'pending_activation'
                order by created_at desc limit 1""", (phone,))
    if not ph:
        _say(phone, "You have no pharmacy waiting to be linked. Reply *REGISTER* to "
                    "set one up.", platform)
        return
    try:
        code = provision.pair_code(ph["gowa_device_id"], ph["wa_number"])
    except provision.ProvisionError as e:
        _say(phone, f"WhatsApp wouldn't issue a code: {e}", platform)
        return
    _say(phone, _activation_message(dict(ph), code), platform)


# ------------------------------------------------------------------ roles
# Order matters and matches the staff.role CHECK constraint exactly
# (owner|manager|pharmacist|attendant). A rank missing a role would leave its precedence
# undefined, so tests assert this covers the constraint.
#
# pharmacist sits below manager because manager is an ADMINISTRATIVE authority -- approving
# purchase orders, seeing the money -- while pharmacist is a CLINICAL one. They are not
# really comparable, and collapsing them onto one axis is a simplification: a pharmacist
# redeeming a manager code gains admin rights, which is intended, but the reverse (a manager
# redeeming a pharmacist code) does NOT grant clinical authority, and must not, because
# dispensing authority has to come from a verified PPB registration rather than a code
# somebody forwarded. That asymmetry is not expressible in a single rank and is the known
# limit of this model -- see the owner-initiated ADD design.
ROLE_RANK = {"attendant": 1, "pharmacist": 2, "manager": 3, "owner": 4}


def effective_role(existing: str | None, granted: str) -> str:
    """The role someone ends up with. Never lower than the one they already hold.

    Last-code-wins was the previous rule and it silently demoted a manager to attendant
    three minutes after promoting them. Privilege loss with no notification is the worst
    shape of this bug: it surfaces days later as "the system won't let me approve this".
    """
    if not existing:
        return granted
    return existing if ROLE_RANK.get(existing, 0) >= ROLE_RANK.get(granted, 0) else granted


def _grant_role(pharmacy_id: str, phone: str, granted: str, mechanism: str,
                actor: str | None = None, name: str | None = None) -> str:
    """Add or promote someone, append to the audit trail, and return the role they hold.

    Every role change lands in staff_role_changes. staff.role decides who may approve a
    prescription-only medicine, and that approval is logged against a PPB number -- so role
    is part of the regulatory record, and a mutable column with no history answers "who
    authorised this, and were they entitled to?" with only the latest value.

    A no-op writes no audit row: an unchanged role is not a change, and padding the trail
    with them makes the real ones harder to find.
    """
    row = q1("select role from staff where pharmacy_id = %s and phone = %s",
             (str(pharmacy_id), phone))
    existing = row["role"] if row else None
    final = effective_role(existing, granted)

    if existing is None:
        ex("""insert into staff (pharmacy_id, name, phone, role, is_active)
              values (%s,%s,%s,%s,true)""",
           (str(pharmacy_id), name or f"Staff {phone[-4:]}", phone, final))
    else:
        # role is set explicitly to the COMPUTED value, never to excluded.role. That single
        # clause -- `do update set role = excluded.role` -- was the whole bug.
        ex("""update staff set role = %s, is_active = true
               where pharmacy_id = %s and phone = %s""",
           (final, str(pharmacy_id), phone))

    if final != existing:
        ex("""insert into staff_role_changes
                (pharmacy_id, phone, old_role, new_role, mechanism, actor)
              values (%s,%s,%s,%s,%s,%s)""",
           (str(pharmacy_id), phone, existing, final, mechanism, actor or phone))
        log.info("role %s -> %s for %s at %s via %s", existing, final, phone,
                 pharmacy_id, mechanism)
    return final


# ------------------------------------------------------------------ joining a pharmacy
def _redeem(phone: str, kind: str, code: str, platform: str | None) -> None:
    column, role = ("owner_code", "manager") if kind == "owner" else ("join_code", "attendant")
    ph = q1(f"""select id, name, status from pharmacies where {column} = %s""", (code,))
    if not ph:
        # Answered from the platform line only if there IS one; a wrong code from a
        # stranger is not worth creating a relationship over, and silence is the correct
        # response to code-guessing.
        log.info("bad %s code %s from %s", kind, code, phone)
        if platform:
            _make_contactable(phone, platform)
            _say(phone, "That code isn't recognised. Ask the pharmacy owner to send you "
                        "a fresh one.", platform)
        return

    pid = str(ph["id"])
    before = q1("select role from staff where pharmacy_id = %s and phone = %s",
                (pid, phone))
    before = before["role"] if before else None
    after = _grant_role(pid, phone, role,
                        mechanism="owner_code" if kind == "owner" else "join_code")

    # Record the message against the line that ACTUALLY received it, not the pharmacy
    # whose code was redeemed. Those are the same thing when staff text the pharmacy's own
    # number, as the instructions tell them to -- and different when they text the platform
    # line, which is exactly what happened on the first live registration.
    #
    # `record_inbound(phone, pid)` was the same fabrication removed from _provision in
    # 681cfa4, one function further down: it asserts a conversation with a pharmacy this
    # phone may never have messaged, and an inbound_history row is precisely what opens
    # Gate 3. Being handed a code is not a conversation.
    #
    # Consequence, deliberate: staff who redeem over the platform line do NOT thereby
    # become reachable on the pharmacy's line. They become reachable by texting it -- which
    # the confirmation asks them to do.
    if platform:
        record_inbound(phone, platform)

    if before == after:
        body = (f"You're already a *{after}* at *{ph['name']}*.\n\n"
                "Text *HELP* any time to see what you can do.")
    elif before and after != role:
        # They redeemed a lower-ranked code than the role they hold. Say so, rather than
        # confirming a role they were not given or silently doing nothing: a code that
        # appears to have been ignored is indistinguishable from a broken one.
        body = (f"You're already a *{after}* at *{ph['name']}*, which is higher than that "
                f"code grants — so nothing changed.\n\n"
                "Text *HELP* any time to see what you can do.")
    else:
        was = f" (was {before})" if before else ""
        body = (f"You're in ✅\n\nYou're now a *{after}* at *{ph['name']}*{was}.\n\n"
                "Text *HELP* any time to see what you can do.")
    # Reply from the pharmacy they just joined when it is live; that is the number they
    # will be talking to from now on, and answering from a different one is confusing.
    # Before it is live, the platform line is the only device that exists.
    if ph["status"] == "active":
        _say(phone, body, pid)
    elif platform:
        _make_contactable(phone, platform)
        _say(phone, body + "\n\n(The pharmacy's own line isn't linked yet — you'll hear "
                           "from it once it is.)", platform)
    log.info("%s joined pharmacy %s as %s", phone, ph["name"], role)


# ------------------------------------------------------------------ going live
def activation_sweep() -> dict:
    """Bind the JID of every pharmacy whose handset has finished linking, and say so.

    Pairing is asynchronous: we hand over a code and someone walks to another room. Only
    GOWA knows when the handset actually linked, and it does not tell us -- so something
    has to look. Until this runs, the pharmacy has a device slot but no `wa_jid`, which
    means tenancy.resolve() cannot route its inbound messages and wa.compose() refuses its
    outbound ones. It is registered and completely mute.

    Runs across ALL pending tenants, so it deliberately does NOT go through
    for_every_tenant() -- that selects only pharmacies which are already paired, which is
    the exact opposite of the set this cares about.
    """
    pending = q("""select id, name, owner_phone, gowa_device_id, join_code, owner_code,
                          wa_number
                     from pharmacies
                    where status = 'pending_activation'
                      and gowa_device_id is not null""")
    activated, waiting = [], []
    for ph in pending:
        try:
            jid = provision.linked_jid(ph["gowa_device_id"])
        except provision.ProvisionError as e:
            log.warning("activation sweep: %s", e)
            return {"status": "gateway unreachable", "checked": 0}
        if not jid:
            waiting.append(ph["name"])
            continue

        # Refuse a slot that linked to a DIFFERENT number than the one registered. It
        # means someone else typed the code -- so the pharmacy would be bound to a handset
        # its owner does not control, and every customer message would go to a stranger.
        if norm_phone(jid) != norm_phone(ph["wa_number"]):
            log.error("slot %s linked to %s but %s registered %s -- not binding",
                      ph["gowa_device_id"], jid, ph["name"], ph["wa_number"])
            waiting.append(f"{ph['name']} (wrong handset)")
            continue

        ex("update pharmacies set wa_jid = %s, status = 'active' where id = %s",
           (jid, str(ph["id"])))
        activated.append(ph["name"])
        log.info("activated %s on %s", ph["name"], jid)

        if ph["owner_phone"]:
            _confirm_activation(ph)
    return {"activated": activated, "still_waiting": waiting}


def _confirm_activation(ph: dict) -> None:
    """Tell the owner their pharmacy is live -- over the PLATFORM line.

    The owner's conversation lives on the platform line: that is the number they texted
    REGISTER to, so Gate 3 is open there and WhatsApp shows one continuous thread. The new
    pharmacy line, by contrast, is a number they have never messaged. Sending from it would
    be a cold contact, and the only ways to make it pass would both be worse than the
    problem: fabricate an inbound_history row (the schema_v10 mistake), or teach Gate 2/3
    that pharmacies.owner_phone is inherently reachable, which would make every registered
    owner cold-messageable by a tenant they have never contacted.

    So the message goes out over the platform line and ASKS them to text the new number.
    Their reply is what opens Gate 3 on the tenant, honestly and by their own action.

    When no platform row exists -- a single-SIM deployment -- the line the owner texted IS
    this tenant's device, so sending from the tenant is factual there, not a bypass. That is
    the only case in which it is used.
    """
    plat = platform_pid()
    sender = plat or str(ph["id"])
    body = (f"*{ph['name']}* is live ✅\n\n"
            f"Your pharmacy's assistant is now running on +{ph['wa_number']}.\n\n"
            f"*Text HELP to +{ph['wa_number']}* to start using it — that also lets it "
            f"reply to you.\n\n"
            f"Your team joins by texting that number:\n"
            f"👤 Managers: *OWNER {ph['owner_code']}*\n"
            f"🧑‍💼 Attendants: *JOIN {ph['join_code']}*")
    if _say(ph["owner_phone"], body, sender):
        return
    # Not silent. A confirmation that cannot be delivered is the single most visible failure
    # in the whole flow -- the owner types the code and nothing arrives -- so it has to be
    # findable in the log rather than inferred from an absence.
    log.error("activation confirmation for %s could not be delivered to %s (sent from %s): "
              "the owner has no open conversation with that line",
              ph["name"], ph["owner_phone"], "platform" if plat else "tenant")


# ------------------------------------------------------------------ the router hook
def intercept(phone: str, msg: dict) -> bool:
    """True when this message was onboarding and has been dealt with.

    Runs BEFORE tenant resolution, because every message it handles comes from someone the
    resolver cannot place: a stranger registering, or a new staff member who is not staff
    until the moment their code is accepted.
    """
    text = (msg.get("text") or "").strip()
    if not text:
        return False

    redemption = parse_redemption(text)
    mid_flow = in_progress(phone)
    is_code_retry = text.upper() == "CODE"
    restart = wants_registration(text)

    if not (redemption or mid_flow or is_code_retry or restart):
        return False

    # If this message arrived on a LIVE TENANT's device, do not intercept it for
    # onboarding. A customer texting a pharmacy number must reach the pharmacy, not
    # the registration flow. The only exception is JOIN/OWNER code redemptions, which
    # can validly arrive on any line (a new staff member who texted the wrong number).
    inbound_pid = msg.get("pharmacy_id")
    if inbound_pid and not redemption:
        tenant = q1("select kind from pharmacies where id = %s", (inbound_pid,))
        if tenant and tenant["kind"] == "tenant":
            return False

    # A staff member who is already deep in a delivery must not have REGISTER hijack their
    # conversation. Redemptions are exempt: a code is unambiguous.
    if not redemption and not mid_flow:
        other = get_state(phone).get("flow") or "idle"
        if other != "idle":
            return False

    if msg.get("wa_id") and q1("select 1 from wa_messages where wa_id = %s",
                               (msg["wa_id"],)):
        log.info("duplicate onboarding message %s ignored", msg["wa_id"])
        return True

    platform = onboarding_line(msg)

    if redemption:
        _log_inbound(phone, msg, platform)
        _redeem(phone, redemption[0], redemption[1], platform)
        return True

    if not platform:
        # Nothing to answer from. Refusing beats replying off a tenant's line: a stranger
        # would get a pharmacy's number and that pharmacy would carry the ban risk.
        log.error("REGISTER from %s but there is no platform line and the inbound device "
                  "resolved to no pharmacy -- nothing can answer", phone)
        return True

    _log_inbound(phone, msg, platform)
    _make_contactable(phone, platform)

    if is_code_retry and not mid_flow:
        _resend_code(phone, platform)
        return True

    if mid_flow and text.upper() in ("CANCEL", "STOP"):
        clear_state(phone)
        _say(phone, "Registration cancelled. Reply *REGISTER* to start again.", platform)
        return True

    st = get_state(phone)
    flow = st["flow"] if mid_flow else START
    ctx = st["context"] if mid_flow else {}

    # REGISTER mid-flow means start over, not "my name is REGISTER". Without this, the one
    # word someone types when they think they have lost the thread is filed as an answer,
    # and the flow they were trying to restart carries on with it.
    if restart or not mid_flow:
        # ctx carries the sender from the first turn so _validate can refuse the
        # owner's own number as the bot line. Without it that check cannot fire at all.
        set_state(phone, START, {"sender": phone}, ttl_min=_TTL_MIN,
                  pharmacy_id=platform)
        _say(phone, _QUESTIONS[START], platform)
        return True

    nxt, ctx, reply = _step(flow, text, ctx)
    if nxt == PROVISION:
        with tenancy.pharmacy_scope(platform):
            _provision(phone, ctx, platform)
        return True

    set_state(phone, nxt, ctx, ttl_min=_TTL_MIN, pharmacy_id=platform)
    _say(phone, reply, platform)
    return True


def _log_inbound(phone: str, msg: dict, pharmacy_id: str | None) -> None:
    """Onboarding messages return before the router's own logging, so record them here.

    Without this the audit trail simply has a hole where every registration was, and the
    idempotency check above has nothing to check against.
    """
    if not (msg.get("wa_id") and pharmacy_id):
        return
    ex("""insert into wa_messages (pharmacy_id, wa_id, direction, from_phone, msg_type,
                                   body, handled)
          values (%s,%s,'in',%s,'text',%s,true)
          on conflict (wa_id) do nothing""",
       (pharmacy_id, msg["wa_id"], phone, (msg.get("text") or "")[:4000]))
