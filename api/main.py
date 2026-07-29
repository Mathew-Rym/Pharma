"""FastAPI entrypoint.

The webhook returns 200 immediately and does the real work in a background task.
Baileys will time out and re-deliver if you make it wait for a 30-second vision call.
"""
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse

from config import settings
from db import ensure_buckets, q, q1, upload
from jobs import JOBS
from router import handle_inbound
from tenant import resolve_pharmacy_by_device, resolve_tenant
from utils import from_pieces, kes, norm_phone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pharmaos")

app = FastAPI(title="Pharma OS Pharmacy API", version="0.2.0")

from agent_api import router as agent_router  # noqa: E402

app.include_router(agent_router)


def _auth(secret: str | None) -> None:
    if secret != settings.SHARED_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")


@app.on_event("startup")
def _startup() -> None:
    try:
        ensure_buckets()
    except Exception:
        log.warning("bucket check skipped", exc_info=True)
    log.info("pharmaos api up · pharmacy=%s · model=%s",
             settings.PHARMACY_ID, settings.MODEL_VISION)


@app.get("/health")
def health():
    row = q1("select count(*) as n from products where pharmacy_id=%s", (settings.PHARMACY_ID,))
    return {"ok": True, "products": row["n"], "time": datetime.utcnow().isoformat()}


# ============================================================ WhatsApp webhook
@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  x_pharmaos_secret: str | None = Header(None)):
    """Text messages from the gateway. Returns immediately."""
    _auth(x_pharmaos_secret)
    body = await request.json()
    log.info("inbound text from=%s", body.get("from"))
    background.add_task(handle_inbound, body)
    return {"ok": True}


@app.post("/webhook/media")
async def webhook_media(background: BackgroundTasks,
                        x_pharmaos_secret: str | None = Header(None),
                        wa_id: str = Form(...),
                        sender: str = Form(...),
                        msg_type: str = Form("image"),
                        caption: str = Form(""),
                        file: UploadFile = File(...)):
    """Images arrive as multipart so Supabase credentials stay in one service."""
    _auth(x_pharmaos_secret)
    phone = norm_phone(sender)
    data = await file.read()

    # Resolve which pharmacy this image is for
    pid = resolve_tenant(phone) or settings.PHARMACY_ID
    staff = q1("select id from staff where phone=%s and pharmacy_id=%s and is_active",
               (phone, pid))
    bucket = settings.BUCKET_INVOICES if staff else settings.BUCKET_RX
    ext = (file.filename or "img.jpg").rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    path = f"{datetime.utcnow():%Y/%m}/{phone}/{uuid.uuid4().hex[:10]}.{ext}"

    upload(bucket, path, data, file.content_type or "image/jpeg")
    log.info("media stored bucket=%s path=%s bytes=%s", bucket, path, len(data))

    background.add_task(handle_inbound, {
        "wa_id": wa_id,
        "from": phone,
        "type": "image",
        "text": caption,
        "media_bucket": bucket,
        "media_path": path,
        "pharmacy_id": pid,
    })
    return {"ok": True, "path": path}


# ============================================================ GOWA webhook
def _gowa_media_path(payload: dict) -> tuple[str, str] | None:
    """Return (kind, relative_path) for the first media field GOWA sent, else None.

    GOWA's shape is version-dependent and documented as such: with auto-download on
    and no caption, `image` is a bare path string; with a caption it becomes
    {"path": ..., "caption": ...}; with auto-download off it is {"url": ...}. Handle
    all three rather than assuming one.
    """
    for kind in ("image", "document", "video", "audio", "sticker"):
        v = payload.get(kind)
        if not v:
            continue
        if isinstance(v, str):
            return kind, v
        if isinstance(v, dict):
            p = v.get("path") or v.get("url")
            if p:
                return kind, p
    return None


@app.post("/webhook/gowa")
async def webhook_gowa(request: Request, background: BackgroundTasks,
                       x_hub_signature_256: str | None = Header(None)):
    """Inbound from go-whatsapp-web-multidevice.

    Translates GOWA's envelope into the one shape router.handle_inbound already
    understands, so adding this transport touches no business logic.
    """
    raw = await request.body()

    # HMAC-SHA256 over the raw body. GOWA's own default secret is the literal
    # "secret", so a missing/incorrect WHATSAPP_WEBHOOK_SECRET is a real exposure:
    # anyone who can reach this URL could inject a staff message and move stock.
    expected = hmac.new(settings.GOWA_WEBHOOK_SECRET.encode(), raw,
                        hashlib.sha256).hexdigest()
    got = (x_hub_signature_256 or "").replace("sha256=", "").strip()
    if not got or not hmac.compare_digest(expected, got):
        log.warning("gowa webhook rejected: bad signature")
        raise HTTPException(status_code=401, detail="bad signature")

    body = json.loads(raw or b"{}")
    if body.get("event") != "message":
        return {"ok": True, "ignored": body.get("event")}

    payload = body.get("payload") or {}
    if payload.get("is_from_me"):
        return {"ok": True, "ignored": "own message"}

    chat_id = str(payload.get("chat_id") or "")
    if chat_id.endswith("@g.us"):
        # Group traffic would let any member of any group drive the pharmacy.
        return {"ok": True, "ignored": "group"}

    phone = norm_phone(payload.get("from") or chat_id)
    if not phone:
        return {"ok": True, "ignored": "no sender"}

    inbound = {
        "wa_id": payload.get("id") or f"gowa-{uuid.uuid4().hex[:12]}",
        "from": phone,
        "type": "text",
        "text": payload.get("body") or payload.get("caption") or "",
    }

    media = _gowa_media_path(payload)
    if media:
        kind, rel = media
        from wa import gowa_fetch_media
        data = (gowa_fetch_media(rel) if not str(rel).startswith("http")
                else None)
        if data is None and str(rel).startswith("http"):
            try:
                import httpx
                data = httpx.get(rel, timeout=120).content
            except Exception:
                log.exception("could not fetch remote media %s", rel)
        if data:
            # Resolve which pharmacy received this media
            pid = resolve_pharmacy_by_device(
                body.get("device_id") or "") or settings.PHARMACY_ID
            staff = q1("""select id from staff where phone=%s and pharmacy_id=%s
                           and is_active""", (phone, pid))
            bucket = settings.BUCKET_INVOICES if staff else settings.BUCKET_RX
            ext = str(rel).rsplit(".", 1)[-1].lower().split("?")[0]
            if ext not in ("jpg", "jpeg", "png", "webp", "pdf"):
                ext = "jpg"
            path = f"{datetime.utcnow():%Y/%m}/{phone}/{uuid.uuid4().hex[:10]}.{ext}"
            upload(bucket, path, data,
                   "application/pdf" if ext == "pdf" else "image/jpeg")
            log.info("gowa media stored bucket=%s path=%s bytes=%s",
                     bucket, path, len(data))
            inbound.update({"type": "image", "media_bucket": bucket,
                            "media_path": path})
        else:
            log.warning("gowa media %s could not be retrieved; treating as text", rel)

    # Resolve pharmacy from GOWA device and inject into the message
    device_pharmacy = resolve_pharmacy_by_device(
        body.get("device_id") or "") or settings.PHARMACY_ID
    inbound["pharmacy_id"] = device_pharmacy

    log.info("gowa inbound from=%s type=%s device=%s pharmacy=%s",
             phone, inbound["type"], body.get("device_id"), device_pharmacy)
    background.add_task(handle_inbound, inbound)
    return {"ok": True}


# ============================================================ M-Pesa
@app.post("/mpesa/callback")
async def mpesa_callback(request: Request):
    """Safaricom calls this. No shared secret available, so it must be idempotent
    and must never trust the payload for anything except matching a known checkout."""
    body = await request.json()
    log.info("mpesa callback: %s", body)
    from mpesa import handle_callback
    try:
        handle_callback(body)
    except Exception:
        log.exception("mpesa callback processing failed")
    # Always 200 — a non-200 makes Safaricom retry forever
    return JSONResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


# ============================================================ cron jobs
@app.post("/jobs/{name}")
def run_job(name: str, x_pharmaos_secret: str | None = Header(None)):
    _auth(x_pharmaos_secret)
    fn = JOBS.get(name)
    if not fn:
        raise HTTPException(404, f"unknown job. available: {list(JOBS)}")
    return fn()


# ============================================================ QR verification
@app.get("/verify/{token}", response_class=HTMLResponse)
def verify(token: str):
    """The page behind the QR code on every receipt. Public, read-only, no PII."""
    o = q1(
        """select o.id, o.created_at, o.total, o.status, ph.name as pharmacy
             from orders o join pharmacies ph on ph.id = o.pharmacy_id
            where o.qr_token = %s""",
        (token,),
    )
    if not o:
        return HTMLResponse("<h2>Not found</h2>", status_code=404)
    lines = q(
        """select p.name, p.pack_size, l.qty_pieces, b.batch_no, b.expiry_date
             from order_lines l
             join products p on p.id = l.product_id
             left join batches b on b.id = l.batch_id
            where l.order_id = %s""",
        (o["id"],),
    )
    rows = "".join(
        f"<tr><td>{l['name']}</td><td>{l['batch_no'] or '-'}</td>"
        f"<td>{l['expiry_date']:%b %Y}</td>"
        f"<td>{from_pieces(l['qty_pieces'], l['pack_size'])}</td></tr>"
        if l["expiry_date"] else
        f"<tr><td>{l['name']}</td><td>{l['batch_no'] or '-'}</td><td>-</td>"
        f"<td>{from_pieces(l['qty_pieces'], l['pack_size'])}</td></tr>"
        for l in lines
    )
    return f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Verify order</title>
<style>
 body{{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:24px;
       background:#f7f9f9;color:#17252a}}
 .card{{max-width:560px;margin:0 auto;background:#fff;border-radius:14px;padding:24px;
        box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 h1{{font-size:19px;margin:0 0 4px}} .m{{color:#6e7a80;font-size:13px}}
 table{{width:100%;border-collapse:collapse;margin-top:18px;font-size:14px}}
 th{{text-align:left;font-size:11px;text-transform:uppercase;color:#6e7a80;
     border-bottom:1px solid #dee4e6;padding:6px 4px}}
 td{{padding:8px 4px;border-bottom:1px solid #f0f3f4}}
 .ok{{display:inline-block;background:#e7f6f1;color:#0d7c66;padding:3px 10px;
      border-radius:20px;font-size:12px;font-weight:600;margin-top:10px}}
</style>
<div class=card>
 <h1>{o['pharmacy']}</h1>
 <div class=m>Order {str(o['id'])[:8].upper()} · {o['created_at']:%d %b %Y %H:%M}</div>
 <div class=ok>✓ Genuine · {o['status']}</div>
 <table><tr><th>Item</th><th>Batch</th><th>Expiry</th><th>Qty</th></tr>{rows}</table>
 <div class=m style="margin-top:16px">Total {kes(o['total'])} · verified by Pharma OS</div>
</div>"""


# ============================================================ dev helper
@app.post("/dev/simulate")
async def simulate(request: Request, background: BackgroundTasks,
                   x_pharmaos_secret: str | None = Header(None)):
    """Fire a fake inbound message without WhatsApp. Invaluable for testing.

    curl -X POST $API/dev/simulate -H "x-pharmaos-secret: $S" \
         -H 'content-type: application/json' \
         -d '{"from":"254700000001","text":"EXPIRY"}'
    """
    _auth(x_pharmaos_secret)
    body = await request.json()
    body.setdefault("wa_id", f"sim-{uuid.uuid4().hex[:12]}")
    body.setdefault("type", "text")
    background.add_task(handle_inbound, body)
    return {"ok": True, "wa_id": body["wa_id"]}
