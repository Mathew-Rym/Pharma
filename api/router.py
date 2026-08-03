"""The router. Every inbound WhatsApp message lands here exactly once.

Design rule: keyword shortcuts are checked BEFORE the LLM. Deterministic commands
must never depend on a model call — if Anthropic has a bad minute, "OK" must still
receive a delivery.
"""
import json
import logging

import tenancy
import register
from db import ex, q, q1
from llm import chat
from reports import TOOLS, denial_message, may_use, run_tool, tools_for
from safety import record_inbound
from state import clear_state, get_state, set_state
from utils import norm_phone
from wa import reply_text

log = logging.getLogger(__name__)

STAFF_SYSTEM = """You are Pharma OS, the operations assistant for a Kenyan retail pharmacy.
You are talking to a staff member on WhatsApp.

Use the provided tools to answer from live pharmacy data. Never invent stock figures,
prices, expiry dates or supplier phone numbers — if a tool returns nothing, say so.

Reply in WhatsApp style: short, plain, no markdown headings, no tables. Use *bold* for
emphasis and • for lists. Amounts in KES. Keep it under 8 lines unless the user asked
for a full list.

If the user asks something outside pharmacy operations, say briefly that you only handle
pharmacy operations."""

CUSTOMER_SYSTEM = """You are the WhatsApp assistant for a Kenyan retail pharmacy, talking
to a customer.

You may: confirm whether a medicine is in stock and its price, explain how to order,
explain delivery, and check order status.

You must NOT: give medical advice, suggest a dose, recommend a medicine for symptoms,
or say a prescription-only medicine will be supplied. For anything clinical, tell them
a pharmacist will help and offer to have one call them.

Reply warmly and briefly, 4 lines maximum. Use tools for stock and price questions."""

CUSTOMER_TOOLS = [t for t in TOOLS if t["name"] == "get_stock"]


# ------------------------------------------------------------------ entry point
def handle_inbound(msg: dict) -> None:
    """msg: {wa_id, from, type, text, media_bucket, media_path, mime}"""
    phone = norm_phone(msg.get("from", ""))
    if not phone:
        return

    # Onboarding runs BEFORE resolution, because everything it handles comes from someone
    # the resolver cannot place: a stranger sending REGISTER, or a new hire who is not
    # staff until the moment their JOIN code is accepted. Resolving first would send both
    # down the unknown-sender path and answer neither.
    if register.intercept(phone, msg):
        return

    # Which pharmacy is this message for?
    #
    # 1. The webhook's device_id, when it maps to a tenant. Strongest signal: it is OUR
    #    number and the sender cannot influence it.
    # 2. Otherwise the sender's identity, which is all we have while one number serves
    #    several pharmacies.
    #
    # There is deliberately no third fallback to a configured pharmacy. That was the
    # `or pid()` this replaces, and it is how one pharmacy's customer ends up writing into
    # another pharmacy's data with every log line looking correct.
    resolved_pid = msg.get("pharmacy_id")
    if not resolved_pid:
        candidates = tenancy.resolve_by_sender(phone)
        if len(candidates) == 1:
            resolved_pid = candidates[0]
        elif len(candidates) > 1:
            # Known at several pharmacies. Ask rather than guess -- guessing locks them
            # into whichever tenant was created first, invisibly and permanently.
            _ask_which_pharmacy(phone, candidates)
            return
        else:
            _greet_unknown(phone)
            return

    with tenancy.pharmacy_scope(resolved_pid):
        _dispatch(phone, msg, resolved_pid)


def _ask_which_pharmacy(phone: str, candidates: list[str]) -> None:
    """One reply, from the first candidate, listing the options.

    Sending from every candidate would mean several pharmacies messaging one person at
    once, which is both confusing and exactly the burst pattern that gets numbers banned.
    """
    names = q("""select id, name from pharmacies where id = any(%s) order by name""",
              (candidates,))
    listing = "\n".join(f"{i}. {r['name']}" for i, r in enumerate(names, 1))
    with tenancy.pharmacy_scope(str(names[0]["id"])):
        reply_text(phone, "You're registered at more than one pharmacy. "
                          f"Which one?\n\n{listing}\n\nReply with the number.")


def _greet_unknown(phone: str) -> None:
    """No relationship anywhere.

    Cannot reply: the anti-ban gates require a relationship, and inventing a customer row
    to satisfy them would let anyone who texts create data in a pharmacy of their choosing.
    Logged so it is visible rather than silent.
    """
    log.info("unresolved sender %s -- no pharmacy relationship; not replying", phone)


def _dispatch(phone: str, msg: dict, resolved_pid: str) -> None:
    """Everything below runs with the tenant bound, so pid() is correct throughout."""
    # Record inbound — this opens Gate 3 for future replies to this phone
    record_inbound(phone, resolved_pid)

    # idempotency — Baileys re-delivers on reconnect
    if msg.get("wa_id"):
        dup = q1("select 1 from wa_messages where wa_id = %s", (msg["wa_id"],))
        if dup:
            log.info("duplicate message %s ignored", msg["wa_id"])
            return

    text = (msg.get("text") or "").strip()
    ex(
        """insert into wa_messages (pharmacy_id, wa_id, direction, from_phone, msg_type,
                                    body, media_path, handled)
           values (%s,%s,'in',%s,%s,%s,%s,false)
           on conflict (wa_id) do nothing""",
        (resolved_pid, msg.get("wa_id"), phone, msg.get("type", "text"),
         text[:4000], msg.get("media_path")),
    )

    staff = q1(
        "select * from staff where phone=%s and pharmacy_id=%s and is_active",
        (phone, resolved_pid),
    )
    try:
        # Unsupported media stops here, before either branch. _handle_staff sends any image
        # to grn.add_page and _handle_customer sends any image to the prescription
        # extractor, so a voice note reaching either is the failure this guard exists for.
        # The row above already recorded its TRUE msg_type and media_path.
        if msg.get("unsupported_media"):
            _refuse_unsupported_media(phone, msg["unsupported_media"])
        elif staff:
            _handle_staff(phone, staff, msg, text)
        else:
            _handle_customer(phone, msg, text)
    except Exception as e:
        log.exception("handler failed for %s", phone)
        reply_text(phone, "Something went wrong on our side. Please try again, "
                         "or type *HELP*.")
        ex("update wa_messages set error=%s where wa_id=%s",
           (f"{type(e).__name__}: {e}"[:500], msg.get("wa_id")))
    finally:
        ex("update wa_messages set handled=true where wa_id=%s", (msg.get("wa_id"),))


_MEDIA_REFUSALS = {
    "audio": "I can't listen to voice notes yet",
    "voice": "I can't listen to voice notes yet",
    "ptt":   "I can't listen to voice notes yet",
    "video": "I can't watch videos",
    "sticker": "I can't read stickers",
    "document": "I can't open documents yet",
}


def _refuse_unsupported_media(phone: str, kind: str) -> None:
    """Say so once, and say what WILL work.

    Silence would be worse than the old behaviour in one respect: the sender would keep
    resending. Naming the alternative -- text, or a photo -- is what turns a refusal into an
    instruction. One message per inbound, and inbound is deduplicated by wa_id, so a
    re-delivered message cannot produce a second refusal.
    """
    what = _MEDIA_REFUSALS.get(kind, f"I can't handle {kind} messages")
    log.info("refused unsupported media (%s) from %s", kind, phone)
    reply_text(phone, f"{what}. Please send it as *text*, or as a *photo* if it's a "
                      f"prescription or an invoice.")


# ------------------------------------------------------------------ staff branch
def _handle_staff(phone: str, staff: dict, msg: dict, text: str) -> None:
    import grn

    st = get_state(phone)
    up = text.upper()

    # --- RECEIVE declares intent, so a photo is never a guess.
    #
    # Every staff image used to become an invoice page unconditionally. That gave a
    # pharmacist photographing a walk-in's prescription no way to say so: it was extracted
    # as a supplier invoice, and if the extractor found line-shaped text it would move
    # stock. There is no undo for that, and nothing in the reply would look wrong.
    #
    # Deterministic keyword, no model. Intent in a privilege-adjacent path must not depend
    # on a classifier being right, for the same reason register.py is a pure function.
    if up in ("RECEIVE", "RECEIVING"):
        clear_state(phone)
        set_state(phone, "grn_collect", {"pages": []})
        reply_text(phone, "Receiving a delivery. Send photos of the supplier invoice — "
                          "all pages — then reply *DONE*.\n\n"
                          "Reply *CANCEL* to stop.")
        return

    if msg.get("type") == "image" and msg.get("media_path"):
        # Which KIND of photo depends on where we are: after the invoice has been read, a
        # photo is the goods being counted rather than another invoice page. Getting this
        # order wrong would file a photo of the delivery as invoice page 3.
        if st["flow"] == "grn_goods":
            grn.add_goods_photo(phone, msg["media_path"])
            return
        if st["flow"] == "grn_collect":
            grn.add_page(phone, msg["media_path"])
            return
        # No active flow: ASK. The two things a staff photo can be are a supplier invoice
        # and a customer's prescription, and they move in opposite directions -- one adds
        # stock, the other dispenses it. Assuming was the bug.
        reply_text(phone, "What is this photo?\n\n"
                          "• Reply *RECEIVE* if it's a supplier invoice — I'll take the "
                          "pages and add the stock\n"
                          "• For a customer's prescription, have the customer send it to "
                          "this number themselves so it's linked to them\n\n"
                          "I haven't filed it anywhere yet.")
        return

    if up == "HELP":
        reply_text(phone, _staff_help(staff["role"]))
        return

    if up in ("CANCEL", "STOP") and st["flow"] != "idle":
        clear_state(phone)
        reply_text(phone, "Cancelled.")
        return

    # --- mid-flow handling takes priority over anything else
    if st["flow"] == "grn_collect":
        if up == "DONE":
            grn.process_pages(phone, staff)
        else:
            reply_text(phone, "Send the next invoice page, or reply *DONE* to process "
                             "what you have sent, or *CANCEL*.")
        return

    if st["flow"] == "grn_goods":
        if grn.handle_goods_reply(phone, staff, text):
            return
        reply_text(phone, "Send a photo of the delivered goods, reply *COUNT* when you "
                         "have sent them all, or *SKIP* to receive on the invoice "
                         "quantities.")
        return

    if st["flow"] == "grn_review":
        grn.handle_review(phone, staff, text)
        return

    # --- deterministic shortcuts before any model call
    #
    # Guarded by the SAME policy as the agent path. These bypass the model entirely, so
    # scoping only the tool list handed to the LLM would have left the cheapest route to
    # the day's takings -- one word, `TODAY` -- completely open.
    if up in ("EXPIRY", "EXPIRING"):
        _guard(phone, staff, "get_expiry_risk", {"days": 90})
        return
    if up in ("LOW", "LOWSTOCK", "LOW STOCK"):
        _guard(phone, staff, "get_stock", {"low_stock_only": True})
        return
    if up in ("TODAY", "SALES"):
        _guard(phone, staff, "get_sales_summary", {"period": "today"})
        return
    if up in ("REPORT", "REPORT MONTH"):
        _guard(phone, staff, "generate_report_pdf", {"period": "month"}, reply=False)
        return
    if up == "REPORT WEEK":
        _guard(phone, staff, "generate_report_pdf", {"period": "week"}, reply=False)
        return
    if up in ("ORDER", "REORDER"):
        # Not run_tool, but the same data -- what to buy and how much. Gated on the tool
        # that answers that question, or ORDER becomes the way round the guard.
        if not may_use(staff["role"], "get_reorder_suggestions"):
            _deny(phone, staff, "get_reorder_suggestions")
            return
        from forecast import reorder_message
        reply_text(phone, reorder_message())
        return

    # --- approvals with a PIN (prescription, then purchase order)
    from approvals import handle_pharmacist_reply, handle_po_reply
    if handle_pharmacist_reply(phone, staff, text):
        return
    if handle_po_reply(phone, staff, text):
        return

    # --- draft purchase orders from the forecast
    if up == "PO" or up.startswith("PO "):
        # Money action: drafts purchase orders and routes them for approval.
        if not may_use(staff["role"], "draft_po"):
            _deny(phone, staff, "draft_po"); return
        from approvals import send_po_for_approval
        from forecast import create_draft_pos
        filt = text[3:].strip() or None
        created = create_draft_pos(str(staff["id"]), filt)
        if not created:
            reply_text(phone, "Nothing to order right now.")
            return
        for c in created:
            send_po_for_approval(c["po_id"])
        return

    # --- talk to the agent on the pharmacy PC
    if up in ("SYNC", "RESYNC", "SYNC NOW"):
        if not may_use(staff["role"], "pc_sync"):
            _deny(phone, staff, "pc_sync"); return
        from agent_api import queue_command
        if queue_command("resync", reply_to=phone, requested_by=str(staff["id"])):
            reply_text(phone, "Asking the pharmacy PC to sync now. This takes up to a "
                             "minute — I will message you when it is done.")
        else:
            reply_text(phone, "No agent is installed on the pharmacy PC yet, so I can "
                             "only see stock that Pharma OS itself received.")
        return

    if up in ("PC", "AGENT", "PC STATUS"):
        if not may_use(staff["role"], "pc_status"):
            _deny(phone, staff, "pc_status"); return
        from agent_api import agent_status
        reply_text(phone, agent_status())
        return

    if up == "PROBE":
        if not may_use(staff["role"], "pc_probe"):
            _deny(phone, staff, "pc_probe"); return
        from agent_api import queue_command
        if queue_command("probe", reply_to=phone, requested_by=str(staff["id"])):
            reply_text(phone, "Scanning the pharmacy PC for the phAMACore database...")
        else:
            reply_text(phone, "No agent installed yet.")
        return

    if up in ("VARIANCE", "RECON", "SHRINKAGE"):
        # Till-vs-stock figures. An attendant denied TODAY must not read them here.
        if not may_use(staff["role"], "variance"):
            _deny(phone, staff, "variance"); return
        from agent_api import reconciliation_summary
        reply_text(phone, reconciliation_summary())
        return

    if up.startswith("WHY "):
        # Explains a reorder suggestion, so it exposes the same sales-derived data.
        if not may_use(staff["role"], "forecast_why"):
            _deny(phone, staff, "forecast_why"); return
        from forecast import forecast_explain
        reply_text(phone, forecast_explain(text[4:].strip()))
        return

    # --- everything else: let the model pick a tool
    # Role-scoped. The model cannot call what it was never shown, and _guard
    # re-checks at execution in case the list and the policy ever drift.
    _agent_reply(phone, text, STAFF_SYSTEM, tools_for(staff["role"]))


# Every advertised command, with the capability it needs. A test asserts this covers
# STAFF_COMMANDS exactly, so a command added to one and not the other fails the build --
# which is the check that would have caught PO sitting ungated behind a help entry.
_HELP_LINES: list[tuple[str, str]] = [
    ("receive_goods",           "• *RECEIVE* — then send invoice photos, then *DONE*"),
    ("get_stock",               "• *LOW* — what is below reorder level"),
    ("get_stock",               "• *do we have amoxil* — stock check"),
    ("find_supplier",           "• *who supplies prenor* — supplier contact"),
    ("pc_sync",                 "• *SYNC* — pull fresh data from the pharmacy PC"),
    ("pc_status",               "• *PC* — is the pharmacy PC online"),
    ("get_expiry_risk",         "• *EXPIRY* — what is expiring in 90 days"),
    ("get_sales_summary",       "• *TODAY* — today's sales"),
    ("get_reorder_suggestions", "• *ORDER* — what to reorder"),
    ("generate_report_pdf",     "• *REPORT* — full PDF report"),
    ("draft_po",                "• *PO* — draft purchase orders from the forecast"),
    ("forecast_why",            "• *WHY prenor* — why the system suggests ordering it"),
    ("variance",                "• *VARIANCE* — where the till and our stock disagree"),
    ("pc_probe",                "• *PROBE* — scan the pharmacy PC for its database"),
]


def _staff_help(role: str) -> str:
    """Only what this role can actually do.

    The parameter was previously accepted and ignored -- every role got the same list,
    including commands they would be refused. That is how the gap hid: the bot advertised
    PO to an attendant, ran it for them, and nothing anywhere disagreed. Listing exactly
    what will work makes help and enforcement the same statement.
    """
    lines = [text for cap, text in _HELP_LINES if may_use(role, cap)]
    return ("*Pharma OS commands*\n"
            "📸 Send a photo of a supplier invoice → I receive the stock (batch + expiry)\n"
            + "\n".join(lines)
            + "\nOr just ask me in your own words.")


def _deny(phone: str, staff: dict, tool: str) -> None:
    log.info("tool %s denied to %s (role=%s)", tool, phone, staff.get("role"))
    reply_text(phone, denial_message(staff.get("role"), tool))


def _guard(phone: str, staff: dict, tool: str, args: dict, reply: bool = True) -> None:
    """Run a tool for a staff member, or say which role is needed.

    One place, used by both the keyword shortcuts and (via the filtered list) the agent, so
    the two cannot drift apart. They already had: CUSTOMER_TOOLS was filtered while the
    staff path was not, and nothing failed to make that visible.
    """
    if not may_use(staff.get("role"), tool):
        _deny(phone, staff, tool)
        return
    out = run_tool(tool, args, phone)
    if reply and out:
        reply_text(phone, out)


# ------------------------------------------------------------ customer branch
def _handle_customer(phone: str, msg: dict, text: str) -> None:
    import rx

    st = get_state(phone)
    up = text.upper()
    cust = rx.get_or_create_customer(phone)

    if up == "DELETE":
        rx.delete_my_data(phone)
        return

    # consent gate — nothing else happens until they say YES
    if not cust["consent_given"]:
        if up in ("YES", "Y", "OK", "NDIO", "SAWA"):
            rx.record_consent(phone)
            pending = st["context"].get("pending_rx")
            if pending:
                rx.receive_prescription(phone, pending)
            return
        rx.ask_consent(phone)
        return

    if msg.get("type") == "image" and msg.get("media_path"):
        rx.receive_prescription(phone, msg["media_path"])
        return

    # A forwarded M-Pesa confirmation SMS beats every other interpretation. Checked
    # before the keyword shortcuts because the SMS body contains words like "SENT"
    # and "PAID" that would otherwise be read as commands.
    from payments_sms import handle_forwarded_sms
    if handle_forwarded_sms(phone, text):
        return

    # Patient is choosing items by number from their own prescription
    if st["flow"] == "rx_select":
        from approvals import handle_selection
        handle_selection(phone, text)
        return

    if up in ("CONFIRM", "PAY", "YES") and st["flow"] in ("awaiting_confirm",
                                                          "awaiting_payment"):
        rx.customer_confirm(phone)
        return

    if up in ("CANCEL", "STOP"):
        clear_state(phone)
        reply_text(phone, "Cancelled. Send a prescription photo whenever you are ready.")
        return

    if up == "STATUS":
        rx.order_status(phone)
        return

    if up == "POINTS":
        reply_text(phone, f"You have *{cust['loyalty_points'] or 0}* loyalty points. "
                         f"100 points = KES 100 off your next order.")
        return

    _agent_reply(phone, text, CUSTOMER_SYSTEM, CUSTOMER_TOOLS)


# ------------------------------------------------------------ agentic loop
def _agent_reply(phone: str, text: str, system: str, tools: list[dict],
                 max_turns: int = 4) -> None:
    """Tool-calling loop. The model chooses tools; run_tool owns the SQL."""
    history = _recent_context(phone)
    messages = history + [{"role": "user", "content": text}]

    for _ in range(max_turns):
        resp = chat(system, messages, tools=tools)
        tool_uses = [b for b in resp.content if b.type == "tool_use"]

        if not tool_uses:
            reply = "".join(b.text for b in resp.content if b.type == "text").strip()
            reply_text(phone, reply or "I did not understand that. Type *HELP* for options.")
            return

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            out = run_tool(tu.name, dict(tu.input or {}), phone)
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": out[:6000],
            })
        messages.append({"role": "user", "content": results})

    reply_text(phone, "I could not finish that request. Try asking more simply, "
                     "or type *HELP*.")


def _recent_context(phone: str, limit: int = 6) -> list[dict]:
    """Short rolling window so follow-ups like 'and last month?' work."""
    rows = q(
        """select direction, body from wa_messages
            where (from_phone=%s or to_phone=%s) and msg_type='text'
              and body is not null and body <> ''
            order by created_at desc limit %s""",
        (phone, phone, limit),
    )
    msgs = []
    for r in reversed(rows):
        role = "user" if r["direction"] == "in" else "assistant"
        if msgs and msgs[-1]["role"] == role:      # API requires alternating roles
            msgs[-1]["content"] += "\n" + r["body"][:1000]
        else:
            msgs.append({"role": role, "content": r["body"][:1000]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if msgs and msgs[-1]["role"] == "user":        # the new message is appended by caller
        msgs.pop()
    return msgs
