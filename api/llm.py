"""All LLM calls. Strict JSON, temperature 0, per-field confidence.

Supports both Gemini (Google GenAI) and Anthropic models seamlessly.
"""
import base64
import json
import logging
import re
from dataclasses import dataclass, field

from config import settings

log = logging.getLogger(__name__)


@dataclass
class ContentBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    # Gemini 3 attaches a "thought signature" to every functionCall part it emits, and
    # the API REJECTS a follow-up request whose echoed functionCall is missing it
    # (400 INVALID_ARGUMENT, "Function call is missing a thought_signature"). The
    # signature is opaque, per-call, and cannot be reconstructed -- so it must ride on
    # the block and be written back out verbatim when the history is rebuilt, or the
    # second turn of every tool loop dies. None for Anthropic, which has no equivalent.
    thought_signature: str | None = None


@dataclass
class ChatResponse:
    content: list[ContentBlock]


gemini_client = None
anthropic_client = None
openrouter_key = ""

if settings.GEMINI_API_KEY:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    except Exception as e:
        log.warning("Could not initialize Gemini client: %s", e)

if settings.ANTHROPIC_API_KEY:
    try:
        # pyrefly: ignore [missing-import]
        from anthropic import Anthropic
        anthropic_client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    except Exception as e:
        log.warning("Could not initialize Anthropic client: %s", e)

# OpenRouter needs no client object -- plain httpx against the OpenAI-format API in
# _chat_openai(). (An earlier attempt used AgentRouter, a coding-agent gateway whose
# content filter rejected pharmacy queries outright -- drug names, symptoms, whole
# languages. A general-purpose router is the only sane fallback for this domain.)


# ============================================================== prompts
INVOICE_SYSTEM = """You extract structured data from East African pharmaceutical supplier invoices.

Return ONLY a single JSON object. No prose, no markdown fences.

Schema:
{
  "supplier_name": string|null,
  "invoice_no": string|null,
  "invoice_date": "YYYY-MM-DD"|null,
  "po_ref": string|null,
  "printed_subtotal": number|null,
  "printed_vat": number|null,
  "printed_net": number|null,
  "lines": [
    {
      "line_no": integer,
      "code": string|null,
      "description": string,
      "batch_no": string|null,
      "expiry_raw": string|null,
      "expiry_date": "YYYY-MM-DD"|null,
      "qty_whole": integer|null,
      "qty_pieces": integer|null,
      "unit_price": number|null,
      "line_total": number|null,
      "confidence": number,
      "unreadable_fields": [string]
    }
  ]
}

Critical rules:
1. QUANTITY NOTATION. Kenyan pharmacy invoices write quantities as whole packs and
   loose pieces: "1W" = 1 whole pack, "5W0P" = 5 whole packs 0 pieces, "2WOP" is the
   same thing with the zero misprinted as a letter O. Put whole packs in qty_whole and
   loose pieces in qty_pieces. Never merge them into one number.
2. BATCH AND EXPIRY are often printed on a SECOND line underneath the description, in
   the BATCH NO. and EXPIRY DATE columns. Associate them with the line above.
3. EXPIRY. Copy the literal printed string into expiry_raw (e.g. "01/2028", "Jul-28",
   "2027-08"). Additionally give expiry_date as the LAST calendar day of that month.
   If you cannot read it, set both to null and add "expiry_date" to unreadable_fields.
4. NEVER GUESS. If a batch number is smudged, set it to null and list it in
   unreadable_fields. A null is useful; an invented value corrupts a pharmacy's records.
5. ECHO THE PRINTED TOTALS exactly as shown (SUB TOTAL, VAT TOTAL, NET / TOTAL) so the
   caller can reconcile against the sum of your lines.
6. Handwritten ticks, stamps and signatures are NOT data. Ignore them.
7. confidence is your own 0.0-1.0 certainty for that whole line.
8. If multiple images are supplied they are consecutive pages of ONE invoice. Number
   lines continuously across pages and read totals from whichever page has them."""

RX_SYSTEM = """You extract structured data from handwritten or printed medical prescriptions.

Return ONLY a single JSON object. No prose, no markdown fences.

Schema:
{
  "patient_name": string|null,
  "prescriber_name": string|null,
  "prescriber_reg": string|null,
  "issued_date": "YYYY-MM-DD"|null,
  "drugs": [
    {
      "drug": string,
      "strength": string|null,
      "form": string|null,
      "qty": integer|null,
      "dosage": string|null,
      "duration_days": integer|null,
      "legible": boolean,
      "confidence": number
    }
  ],
  "overall_confidence": number,
  "notes": string|null
}

Critical rules:
1. NEVER infer a dose, strength or quantity that is not written. Missing means null.
2. If a drug name is not clearly legible, still include it with your best reading but
   set legible=false and confidence low. A pharmacist will review it.
3. Do not correct or substitute drug names to something you consider more likely.
   Report what is written.
4. If the image is not a prescription at all, return an empty drugs array and explain
   in notes."""


# ============================================================== core calls
def _with_429_backoff(fn, *args, **kwargs):
    """Run a Gemini generate_content call, waiting out rate-limit windows.

    Free-tier keys are capped at 20 requests/minute per model. Without this, a burst of
    WhatsApp messages during a demo turns into a wall of "Something went wrong on our
    side" replies -- the SDK's own retry is far too short to outlast a rolling window.
    The 429 body helpfully says "Please retry in Ns", so honour it: sleep a little past
    that and try again, up to a total of ~75s. A reply that lands a minute late is
    recoverable; an error reply mid-demo reads as broken.
    """
    import time
    deadline = time.monotonic() + 75
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "429" not in msg[:300]:
                raise
            # A per-DAY quota is exhausted. No amount of retrying inside this request
            # will help -- the window resets at midnight Pacific -- so raise NOW and let
            # chat() fall back to the next provider in milliseconds. The API's own
            # "Please retry in Ns" hint is about a per-minute limit and is actively
            # misleading here: honouring it made every reply wait 75s before failing over.
            if "PerDay" in msg or "PerProjectPerModel" in msg:
                raise
            if time.monotonic() >= deadline:
                raise
            attempt += 1
            wait = min(15.0 * attempt, 30.0)
            m = re.search(r"retry in ([\d.]+)s", msg)
            if m:
                wait = max(3.0, min(float(m.group(1)) + 1.5, 45.0))
            log.warning("Gemini rate-limited (attempt %d); backing off %.1fs",
                        attempt, wait)
            time.sleep(wait)


def _extract_json(text: str) -> dict:
    """Models occasionally wrap JSON in fences despite instructions. Be forgiving."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def vision_json(system: str, images: list[bytes], instruction: str,
                media_types: list[str] | None = None) -> dict:
    provider = settings.LLM_PROVIDER
    if provider == "gemini" and gemini_client:
        from google.genai import types
        contents = []
        for i, img in enumerate(images):
            mt = (media_types[i] if media_types and i < len(media_types) else "image/jpeg")
            contents.append(types.Part.from_bytes(data=img, mime_type=mt))
        contents.append(instruction)

        res = _with_429_backoff(
            gemini_client.models.generate_content,
            model=settings.MODEL_VISION,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0.0,
            )
        )
        return _extract_json(res.text)

    elif anthropic_client:
        content: list[dict] = []
        for i, img in enumerate(images):
            mt = (media_types[i] if media_types and i < len(media_types) else "image/jpeg")
            content.append({"type": "text", "text": f"--- page {i + 1} ---"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mt,
                    "data": base64.b64encode(img).decode(),
                },
            })
        content.append({"type": "text", "text": instruction})

        resp = anthropic_client.messages.create(
            model=settings.MODEL_VISION,
            max_tokens=8000,
            temperature=0,
            system=system,
            messages=[
                {"role": "user", "content": content},
                {"role": "assistant", "content": "{"},
            ],
        )
        raw = "{" + "".join(b.text for b in resp.content if b.type == "text")
        return _extract_json(raw)
    else:
        raise RuntimeError("No configured LLM client available.")


def extract_invoice(images: list[bytes], media_types: list[str] | None = None) -> dict:
    return vision_json(
        INVOICE_SYSTEM, images,
        "Extract every line item from this supplier invoice as JSON.",
        media_types,
    )


COUNT_SYSTEM = """You count pharmaceutical stock in photographs of a delivery.

Return ONLY a single JSON object. No prose, no markdown fences.

You are given the lines of a supplier invoice, and one or more photos of the goods
that arrived. Your job is to say how many of each invoiced item you can SEE.

COUNT SEALED PACKS / BOXES / CARTONS. Do not attempt to count individual tablets,
capsules or blisters inside a sealed pack — you cannot see them, and guessing is worse
than saying you are unsure. If loose single units are visible outside a pack, count
those separately as "loose".

Rules that matter more than being helpful:
- Only count items that appear on the invoice line list you were given. Ignore shelves,
  fixtures, other stock, hands, and the counter.
- If items are stacked so you cannot see the back or bottom of a pile, you are seeing
  a SUBSET. Say so in "note" and lower "confidence". Do NOT extrapolate a total from
  a visible face.
- If you cannot find an invoiced item in any photo at all, return it with packs 0 and
  note "not visible in photo". That is different from "zero were delivered", and the
  pharmacist will decide which it is.
- If a pack looks crushed, wet, torn or opened, say so in "note".
- confidence is your own honesty about the count: 1.0 only when every unit of that
  item is individually and unambiguously visible.
- fully_visible is a SEPARATE judgement from confidence, and the more important one.
  Set it false whenever anything could be outside the frame, behind another pack, cut
  off at an edge, or under a pile. "I can clearly see 2 packs and there may be more"
  is confidence 1.0 with fully_visible false.

Schema:
{
  "items": [
    {
      "line_no": integer,        // the invoice line you are counting
      "packs": integer,          // whole sealed packs/boxes you can see
      "loose": integer,          // loose single units outside a pack, else 0
      "confidence": number,      // 0.0-1.0, how sure you are of what you counted
      "fully_visible": boolean,  // false if any unit could be hidden or out of frame
      "note": string|null        // "3 boxes behind the front row are partially hidden"
    }
  ],
  "photo_quality": string|null,   // "blurry", "too dark", "glare on labels", or null
  "unlisted_items_seen": integer  // how many distinct products you saw that are NOT
                                  // on the invoice, 0 if none
}"""


def count_delivery(images: list[bytes], lines: list[dict],
                   media_types: list[str] | None = None) -> dict:
    """Count the physical goods against the invoice lines.

    The invoice lines are passed IN as a reference list on purpose. Asking "how many
    boxes are in this photo" of a mixed pharmaceutical delivery is hopeless — the boxes
    are all small white cardboard with small print. Asking "how many AMOXIL 500MG 21S
    can you see, and how many PANADOL 500MG 24S" is a far easier and more accurate
    question, and it lets the model return an answer already keyed to line_no.
    """
    manifest = "\n".join(
        f"line {l.get('line_no')}: {l.get('raw_description') or l.get('description')}"
        + (f" (pack of {l['pack_size']})" if l.get("pack_size") else "")
        + (f" — invoice says {l['qty_invoiced_pieces']} pieces"
           if l.get("qty_invoiced_pieces") else "")
        for l in lines
    )
    return vision_json(
        COUNT_SYSTEM, images,
        "Count how many of each of these invoiced items you can see in the photo(s).\n\n"
        f"INVOICE LINES:\n{manifest}",
        media_types,
    )


def extract_prescription(images: list[bytes], media_types: list[str] | None = None) -> dict:
    return vision_json(
        RX_SYSTEM, images,
        "Extract this prescription as JSON.",
        media_types,
    )


def _gemini_function_call_part(name: str, args: dict, thought_signature: str | None):
    """A functionCall part, carrying the thought signature through when there is one.

    Part.from_function_call() cannot attach one, and a rebuilt functionCall without the
    signature the model originally emitted is rejected by the API on the next turn.
    """
    from google.genai import types
    return types.Part(
        function_call=types.FunctionCall(name=name, args=args or {}),
        thought_signature=thought_signature or None,
    )


def chat(system: str, messages: list[dict], tools: list[dict] | None = None,
         max_tokens: int = 2000):
    """One turn of conversation, with provider selection and fallback.

    Order of preference:
      1. the configured provider (gemini by default),
      2. OpenRouter (chat only) if the primary raised -- a rate-limited or dead
         primary should degrade the reply, not kill it.
    Vision never falls back: free router models cannot see images, and an invoice
    silently read by the wrong engine is worse than a visible failure.
    """
    provider = settings.LLM_PROVIDER

    if provider == "gemini" and gemini_client:
        try:
            return _chat_gemini(system, messages, tools)
        except Exception as e:
            if not settings.OPENROUTER_API_KEY:
                raise
            log.warning("primary LLM failed (%s); answering via OpenRouter",
                        str(e).splitlines()[0][:200])
            return _chat_openai(system, messages, tools, max_tokens)

    if provider == "openrouter" and settings.OPENROUTER_API_KEY:
        return _chat_openai(system, messages, tools, max_tokens)

    if anthropic_client:
        return _chat_anthropic(anthropic_client, settings.MODEL_CHAT,
                               system, messages, tools, max_tokens)

    raise RuntimeError("No configured LLM client available.")


def _chat_openai(system: str, messages: list[dict], tools: list[dict] | None,
                 max_tokens: int):
    """One turn via OpenRouter's OpenAI-format chat completions, tools included.

    The tool loop keeps history in Anthropic-ish shapes (ContentBlock dataclasses from
    Gemini turns, dicts from everywhere else), so both are translated to OpenAI's
    wire format here: tool_use -> assistant.tool_calls, tool_result -> role:"tool".
    """
    import httpx

    oai_msgs: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = m.get("content", "")

        if isinstance(content, str):
            if content.strip():
                oai_msgs.append({"role": role, "content": content})
            continue

        tool_calls, text_parts, tool_results = [], [], []
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict):
                itype = item.get("type")
                if itype == "tool_use":
                    tool_calls.append({
                        "id": item.get("id") or item.get("name"),
                        "type": "function",
                        "function": {"name": item["name"],
                                     "arguments": json.dumps(item.get("input") or {})},
                    })
                elif itype == "tool_result":
                    tool_results.append({"role": "tool",
                                         "tool_call_id": item.get("tool_use_id"),
                                         "content": str(item.get("content", ""))})
                elif itype == "text" and item.get("text"):
                    text_parts.append(item["text"])
            else:
                if getattr(item, "type", None) == "tool_use":
                    tool_calls.append({
                        "id": item.id or item.name,
                        "type": "function",
                        "function": {"name": item.name,
                                     "arguments": json.dumps(item.input or {})},
                    })
                elif getattr(item, "type", None) == "text" and item.text:
                    text_parts.append(item.text)

        if tool_calls:
            oai_msgs.append({"role": "assistant",
                             "content": "\n".join(text_parts) or None,
                             "tool_calls": tool_calls})
        elif text_parts:
            oai_msgs.append({"role": role, "content": "\n".join(text_parts)})
        oai_msgs.extend(tool_results)

    payload: dict = {
        "model": settings.OPENROUTER_MODEL,
        "messages": oai_msgs,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if tools:
        payload["tools"] = [{"type": "function",
                             "function": {"name": t["name"],
                                          "description": t.get("description", ""),
                                          "parameters": t.get("input_schema",
                                                              {"type": "object"})}}
                            for t in tools]
    r = httpx.post(
        f"{settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                 "X-Title": "Pharma OS"},
        json=payload, timeout=60,
    )
    r.raise_for_status()
    msg = (r.json().get("choices") or [{}])[0].get("message") or {}

    blocks = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        blocks.append(ContentBlock(type="tool_use", id=tc.get("id") or fn.get("name"),
                                   name=fn.get("name"), input=args))
    if msg.get("content"):
        blocks.append(ContentBlock(type="text", text=msg["content"]))
    return ChatResponse(content=blocks)


def _blocks_to_anthropic_dicts(content) -> list:
    """Normalise an assistant/content list to plain dicts for the Anthropic wire format.

    The tool loop keeps history in two shapes depending on which engine produced the
    previous turn: Gemini turns yield ContentBlock dataclasses, Anthropic-shaped turns
    yield dicts. The Anthropic SDK serialises dicts only, so dataclasses are converted
    and anything it cannot express is dropped rather than crashing the fallback.
    """
    out = []
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict):
            if item.get("type") in ("tool_use", "tool_result", "text"):
                out.append(item)
        elif getattr(item, "type", None) == "tool_use":
            out.append({"type": "tool_use", "id": item.id, "name": item.name,
                        "input": item.input or {}})
        elif getattr(item, "type", None) == "text" and item.text:
            out.append({"type": "text", "text": item.text})
    return out


def _chat_anthropic(client, model: str, system: str, messages: list[dict],
                    tools: list[dict] | None, max_tokens: int):
    safe_messages = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            content = _blocks_to_anthropic_dicts(content)
        if content in ("", None, []):
            continue        # the API rejects empty content entries outright
        safe_messages.append({"role": m.get("role", "user"), "content": content})
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "system": system,
        "messages": safe_messages,
    }
    if tools:
        kwargs["tools"] = tools
    return client.messages.create(**kwargs)


def _chat_gemini(system: str, messages: list[dict], tools: list[dict] | None):
    from google.genai import types

    # Build Gemini tool declarations
    g_tools = None
    if tools:
        func_decls = []
        for t in tools:
            schema_props = {}
            props = t.get("input_schema", {}).get("properties", {})
            for p_name, p_info in props.items():
                t_type = "STRING"
                if p_info.get("type") == "boolean":
                    t_type = "BOOLEAN"
                elif p_info.get("type") == "integer":
                    t_type = "INTEGER"
                elif p_info.get("type") == "number":
                    t_type = "NUMBER"
                schema_props[p_name] = types.Schema(
                    type=t_type,
                    description=p_info.get("description", "")
                )
            func_decls.append(types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=types.Schema(type="OBJECT", properties=schema_props)
            ))
        g_tools = [types.Tool(function_declarations=func_decls)]

    # Convert Anthropic-style message history to Gemini Content objects.
    # Roles: "user" -> "user", "assistant" -> "model".
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        content = m.get("content", "")

        if isinstance(content, str):
            if content.strip():
                contents.append(types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=content)]
                ))

        elif isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    # ContentBlock dataclass from a previous assistant turn
                    if getattr(item, "type", None) == "tool_use":
                        parts.append(_gemini_function_call_part(
                            item.name, item.input or {},
                            getattr(item, "thought_signature", None)))
                    elif getattr(item, "type", None) == "text" and item.text:
                        parts.append(types.Part.from_text(text=item.text))
                    continue

                itype = item.get("type")
                if itype == "tool_use":
                    parts.append(_gemini_function_call_part(
                        item["name"], item.get("input") or {},
                        item.get("thought_signature")))
                elif itype == "tool_result":
                    parts.append(types.Part.from_function_response(
                        name=item.get("tool_use_id", "tool"),
                        response={"output": item.get("content", "")}
                    ))
                elif itype == "text" and item.get("text"):
                    parts.append(types.Part.from_text(text=item["text"]))

            if parts:
                contents.append(types.Content(role=role, parts=parts))

    res = _with_429_backoff(
        gemini_client.models.generate_content,
        model=settings.MODEL_CHAT,
        contents=contents,
        config=types.GenerateContentConfig(
            tools=g_tools,
            system_instruction=system,
            temperature=0.0,
        )
    )

    # Parse the RAW candidate parts rather than the res.function_calls convenience
    # list: only the raw parts carry each call's thought_signature, which must be
    # echoed back on the next turn or the API returns 400 INVALID_ARGUMENT.
    blocks = []
    try:
        cand_parts = ((res.candidates or [None])[0].content.parts) or []
    except Exception:
        cand_parts = []
    for p in cand_parts:
        fc = getattr(p, "function_call", None)
        if fc is not None:
            blocks.append(ContentBlock(
                type="tool_use",
                id=fc.name,
                name=fc.name,
                input=dict(fc.args or {}),
                thought_signature=getattr(p, "thought_signature", None),
            ))
    if getattr(res, "text", None):
        blocks.append(ContentBlock(type="text", text=res.text))

    return ChatResponse(content=blocks)
