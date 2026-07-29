"""The router. Every inbound WhatsApp message lands here exactly once.

Design rule: keyword shortcuts are checked BEFORE the LLM. Deterministic commands
must never depend on a model call — if Anthropic has a bad minute, "OK" must still
receive a delivery.
"""
import json
import logging

from config import settings
from db import ex, q, q1
from llm import chat
from reports import TOOLS, run_tool
from safety import record_inbound
from state import clear_state, get_state, set_state
from tenant import resolve_tenant
from utils import norm_phone
from wa import reply_text, send_text

log = logging.getLogger(__name__)
PID = settings.PHARMACY_ID

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

    # Resolve which pharmacy this message is for.
    # Try: msg-level override (set by webhook) > tenant resolver > env fallback.
    resolved_pid = msg.get("pharmacy_id") or resolve_tenant(phone) or PID

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
        if staff:
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


# ------------------------------------------------------------------ staff branch
def _handle_staff(phone: str, staff: dict, msg: dict, text: str) -> None:
    import grn

    st = get_state(phone)
    up = text.upper()

    # --- images always mean "receiving a delivery" for staff.
    # Which KIND of photo depends on where we are: mid-receiving, after the invoice has
    # been read, a photo is the goods being counted rather than another invoice page.
    # Getting this order wrong would file a photo of the delivery as invoice page 3 and
    # send it to the extractor.
    if msg.get("type") == "image" and msg.get("media_path"):
        if st["flow"] == "grn_goods":
            grn.add_goods_photo(phone, msg["media_path"])
        else:
            grn.add_page(phone, msg["media_path"])
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
    if up in ("EXPIRY", "EXPIRING"):
        reply_text(phone, run_tool("get_expiry_risk", {"days": 90}, phone))
        return
    if up in ("LOW", "LOWSTOCK", "LOW STOCK"):
        reply_text(phone, run_tool("get_stock", {"low_stock_only": True}, phone))
        return
    if up in ("TODAY", "SALES"):
        reply_text(phone, run_tool("get_sales_summary", {"period": "today"}, phone))
        return
    if up in ("REPORT", "REPORT MONTH"):
        run_tool("generate_report_pdf", {"period": "month"}, phone)
        return
    if up == "REPORT WEEK":
        run_tool("generate_report_pdf", {"period": "week"}, phone)
        return
    if up in ("ORDER", "REORDER"):
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
        from agent_api import queue_command
        if queue_command("resync", reply_to=phone, requested_by=str(staff["id"])):
            reply_text(phone, "Asking the pharmacy PC to sync now. This takes up to a "
                             "minute — I will message you when it is done.")
        else:
            reply_text(phone, "No agent is installed on the pharmacy PC yet, so I can "
                             "only see stock that Pharma OS itself received.")
        return

    if up in ("PC", "AGENT", "PC STATUS"):
        from agent_api import agent_status
        reply_text(phone, agent_status())
        return

    if up == "PROBE":
        from agent_api import queue_command
        if queue_command("probe", reply_to=phone, requested_by=str(staff["id"])):
            reply_text(phone, "Scanning the pharmacy PC for the phAMACore database...")
        else:
            reply_text(phone, "No agent installed yet.")
        return

    if up in ("VARIANCE", "RECON", "SHRINKAGE"):
        from agent_api import reconciliation_summary
        reply_text(phone, reconciliation_summary())
        return

    if up.startswith("WHY "):
        from forecast import forecast_explain
        reply_text(phone, forecast_explain(text[4:].strip()))
        return

    # --- everything else: let the model pick a tool
    _agent_reply(phone, text, STAFF_SYSTEM, TOOLS)


def _staff_help(role: str) -> str:
    base = (
        "*Pharma OS commands*\n"
        "📸 Send a photo of a supplier invoice → I receive the stock (batch + expiry)\n"
        "• *EXPIRY* — what is expiring in 90 days\n"
        "• *LOW* — what is below reorder level\n"
        "• *TODAY* — today's sales\n"
        "• *ORDER* — what to reorder\n"
        "• *REPORT* — full PDF report\n"
        "• *who supplies prenor* — supplier contact\n"
        "• *do we have amoxil* — stock check\n"
        "• *PO* — draft purchase orders from the forecast\n"
        "• *WHY prenor* — why the system suggests ordering it\n"
        "• *VARIANCE* — where the till and our stock disagree\n"
        "• *SYNC* — pull fresh data from the pharmacy PC\n"
        "• *PC* — is the pharmacy PC online\n"
        "Or just ask me in your own words."
    )
    return base


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
