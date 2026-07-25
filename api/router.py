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
from state import clear_state, get_state, set_state
from utils import norm_phone
from wa import send_text

log = logging.getLogger(__name__)
PID = settings.PHARMACY_ID

STAFF_SYSTEM = """You are Dishii, the operations assistant for a Kenyan retail pharmacy.
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
        (PID, msg.get("wa_id"), phone, msg.get("type", "text"),
         text[:4000], msg.get("media_path")),
    )

    staff = q1(
        "select * from staff where phone=%s and pharmacy_id=%s and is_active",
        (phone, PID),
    )
    try:
        if staff:
            _handle_staff(phone, staff, msg, text)
        else:
            _handle_customer(phone, msg, text)
    except Exception as e:
        log.exception("handler failed for %s", phone)
        send_text(phone, "Something went wrong on our side. Please try again, "
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

    # --- images always mean "receiving a delivery" for staff
    if msg.get("type") == "image" and msg.get("media_path"):
        grn.add_page(phone, msg["media_path"])
        return

    if up == "HELP":
        send_text(phone, _staff_help(staff["role"]))
        return

    if up in ("CANCEL", "STOP") and st["flow"] != "idle":
        clear_state(phone)
        send_text(phone, "Cancelled.")
        return

    # --- mid-flow handling takes priority over anything else
    if st["flow"] == "grn_collect":
        if up == "DONE":
            grn.process_pages(phone, staff)
        else:
            send_text(phone, "Send the next invoice page, or reply *DONE* to process "
                             "what you have sent, or *CANCEL*.")
        return

    if st["flow"] == "grn_review":
        grn.handle_review(phone, staff, text)
        return

    # --- deterministic shortcuts before any model call
    if up in ("EXPIRY", "EXPIRING"):
        send_text(phone, run_tool("get_expiry_risk", {"days": 90}, phone))
        return
    if up in ("LOW", "LOWSTOCK", "LOW STOCK"):
        send_text(phone, run_tool("get_stock", {"low_stock_only": True}, phone))
        return
    if up in ("TODAY", "SALES"):
        send_text(phone, run_tool("get_sales_summary", {"period": "today"}, phone))
        return
    if up in ("REPORT", "REPORT MONTH"):
        run_tool("generate_report_pdf", {"period": "month"}, phone)
        return
    if up == "REPORT WEEK":
        run_tool("generate_report_pdf", {"period": "week"}, phone)
        return
    if up in ("ORDER", "REORDER"):
        send_text(phone, run_tool("get_reorder_suggestions", {}, phone))
        return

    # --- everything else: let the model pick a tool
    _agent_reply(phone, text, STAFF_SYSTEM, TOOLS)


def _staff_help(role: str) -> str:
    base = (
        "*Dishii commands*\n"
        "📸 Send a photo of a supplier invoice → I receive the stock (batch + expiry)\n"
        "• *EXPIRY* — what is expiring in 90 days\n"
        "• *LOW* — what is below reorder level\n"
        "• *TODAY* — today's sales\n"
        "• *ORDER* — what to reorder\n"
        "• *REPORT* — full PDF report\n"
        "• *who supplies prenor* — supplier contact\n"
        "• *do we have amoxil* — stock check\n"
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

    if up in ("CONFIRM", "PAY", "YES") and st["flow"] == "awaiting_confirm":
        rx.customer_confirm(phone)
        return

    if up in ("CANCEL", "STOP"):
        clear_state(phone)
        send_text(phone, "Cancelled. Send a prescription photo whenever you are ready.")
        return

    if up == "STATUS":
        rx.order_status(phone)
        return

    if up == "POINTS":
        send_text(phone, f"You have *{cust['loyalty_points'] or 0}* loyalty points. "
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
            send_text(phone, reply or "I did not understand that. Type *HELP* for options.")
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

    send_text(phone, "I could not finish that request. Try asking more simply, "
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
