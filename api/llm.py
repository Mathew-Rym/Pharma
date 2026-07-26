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


@dataclass
class ChatResponse:
    content: list[ContentBlock]


gemini_client = None
anthropic_client = None

if settings.GEMINI_API_KEY:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    except Exception as e:
        log.warning("Could not initialize Gemini client: %s", e)

if settings.ANTHROPIC_API_KEY:
    try:
        from anthropic import Anthropic
        anthropic_client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    except Exception as e:
        log.warning("Could not initialize Anthropic client: %s", e)


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

        res = gemini_client.models.generate_content(
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


def extract_prescription(images: list[bytes], media_types: list[str] | None = None) -> dict:
    return vision_json(
        RX_SYSTEM, images,
        "Extract this prescription as JSON.",
        media_types,
    )


def chat(system: str, messages: list[dict], tools: list[dict] | None = None,
         max_tokens: int = 2000):
    provider = settings.LLM_PROVIDER

    if provider == "gemini" and gemini_client:
        from google.genai import types

        # Build Gemini tools if provided
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

        # Check if messages contains tool execution results from prior turn
        prompt_text = ""
        tool_results_text = ""
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role == "user":
                if isinstance(content, str):
                    prompt_text = content
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            tool_results_text += f"\nTool output: {item.get('content', '')}"

        if tool_results_text:
            combined_prompt = f"Tool Execution Output:\n{tool_results_text}\n\nOriginal Question: {prompt_text}\nProvide the final answer based on the tool output."
            res = gemini_client.models.generate_content(
                model=settings.MODEL_CHAT,
                contents=combined_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.0,
                )
            )
        else:
            res = gemini_client.models.generate_content(
                model=settings.MODEL_CHAT,
                contents=prompt_text,
                config=types.GenerateContentConfig(
                    tools=g_tools,
                    system_instruction=system,
                    temperature=0.0,
                )
            )

        blocks = []
        if getattr(res, "function_calls", None):
            for fc in res.function_calls:
                blocks.append(ContentBlock(
                    type="tool_use",
                    id=fc.name,
                    name=fc.name,
                    input=dict(fc.args or {})
                ))
        if getattr(res, "text", None):
            blocks.append(ContentBlock(type="text", text=res.text))

        return ChatResponse(content=blocks)

    elif anthropic_client:
        kwargs: dict = {
            "model": settings.MODEL_CHAT,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return anthropic_client.messages.create(**kwargs)
    else:
        raise RuntimeError("No configured LLM client available.")
