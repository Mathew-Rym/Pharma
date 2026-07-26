"""Cloud side of the agent protocol.

Direction of travel is always agent -> cloud. We never dial into a pharmacy PC.
That gives us: no inbound ports, no router config, no firewall exception, no
remote-shell surface, and it works from any NAT'd machine on any ISP.

"Ask the agent something from WhatsApp" is therefore a queue, not a call:
    WhatsApp msg -> /jobs or router -> insert agent_commands
    -> agent long-polls -> executes locally -> POSTs result
    -> we WhatsApp the result to reply_to
Latency is up to one poll interval (60s). For "resync now" that is fine.
"""
import json
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request

from config import settings
from db import apply_movement, ex, ex1, q, q1, tx
from utils import kes

log = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])
PID = settings.PHARMACY_ID


def _agent(token: str | None) -> dict:
    if not token:
        raise HTTPException(401, "missing agent token")
    a = q1("select * from agents where agent_token = %s", (token,))
    if not a:
        raise HTTPException(401, "unknown agent token")
    ex("update agents set last_seen_at = now() where id = %s", (a["id"],))
    return a


# ============================================================ enrolment
@router.post("/enrol")
async def enrol(request: Request):
    """One-time handshake. Trades the install token for a per-install token so a
    leaked token can never affect another pharmacy."""
    body = await request.json()
    tok = body.get("enrolment_token")
    a = q1("""select * from agents where enrolment_token = %s and agent_token is null""",
           (tok,))
    if not a:
        # allow re-enrolment of an existing agent (reinstall on the same PC)
        a = q1("select * from agents where enrolment_token = %s", (tok,))
        if not a:
            raise HTTPException(403, "invalid or already-used enrolment token")

    agent_token = secrets.token_urlsafe(32)
    ex("""update agents set agent_token=%s, machine_name=%s, agent_version=%s,
                            last_seen_at=now() where id=%s""",
       (agent_token, body.get("machine_name"), body.get("agent_version"), a["id"]))
    log.info("agent enrolled: %s on %s", a["id"], body.get("machine_name"))
    return {"agent_id": str(a["id"]), "agent_token": agent_token,
            "poll_seconds": 60, "ingest_seconds": 900}


@router.post("/heartbeat")
async def heartbeat(request: Request, x_agent_token: str | None = Header(None)):
    a = _agent(x_agent_token)
    body = await request.json()
    ex("""update agents set agent_version=%s, ingest_mode=%s, db_engine=%s,
                            db_detail=%s, last_seen_at=now() where id=%s""",
       (body.get("agent_version"), body.get("ingest_mode"),
        (body.get("db_detail") or {}).get("engine"),
        json.dumps(body.get("db_detail") or {}), a["id"]))
    return {"ok": True, "suspended": bool(a["suspended"]),
            "server_time": datetime.now(timezone.utc).isoformat()}


# ============================================================ command queue
@router.get("/commands")
def take_commands(x_agent_token: str | None = Header(None)):
    a = _agent(x_agent_token)
    rows = q("""update agent_commands set status='taken', taken_at=now()
                 where id in (select id from agent_commands
                               where agent_id=%s and status='queued'
                               order by created_at limit 5)
                 returning id, command, args""", (a["id"],))
    return {"commands": [{"id": str(r["id"]), "command": r["command"],
                          "args": r["args"]} for r in rows]}


@router.post("/commands/{command_id}/result")
async def command_result(command_id: str, request: Request,
                         x_agent_token: str | None = Header(None)):
    _agent(x_agent_token)
    body = await request.json()
    ok = bool(body.get("ok"))
    result = body.get("result") or {}

    row = ex1("""update agent_commands
                    set status=%s, result=%s, done_at=now()
                  where id=%s returning command, reply_to""",
              ("done" if ok else "error", json.dumps(result), command_id))
    if row and row["reply_to"]:
        from wa import send_text
        send_text(row["reply_to"], _humanise(row["command"], ok, result))
    return {"ok": True}


def _humanise(command: str, ok: bool, result: dict) -> str:
    """The agent speaks JSON; the pharmacist reads WhatsApp."""
    if not ok:
        return (f"⚠️ Could not run *{command}* on the pharmacy PC: "
                f"{result.get('error', 'unknown error')}")
    if command == "ping":
        return (f"✅ Pharmacy PC is online ({result.get('machine', '?')}, "
                f"agent {result.get('agent_version', '?')}).")
    if command == "probe":
        eng = result.get("engine") or "not identified"
        files = result.get("files") or []
        live = result.get("likely_live_db")
        msg = [f"🔍 *Pharmacy PC scan*\nDatabase engine: {eng}",
               f"Recommended sync mode: {result.get('recommended_mode')}"]
        if live:
            msg.append(f"Live database looks like:\n{live['path']}\n"
                       f"({live['size_mb']} MB, modified {live['modified'][:16]})")
        if result.get("open_ports"):
            msg.append("Open DB ports: " +
                       ", ".join(f"{p['port']} ({p['engine']})"
                                 for p in result["open_ports"]))
        msg.append(f"{len(files)} database file(s) found on disk.")
        return "\n\n".join(msg)
    if command in ("export_now", "resync", "full_backfill"):
        f = result.get("folder", result)
        parts = []
        if f.get("files"):
            parts.append(f"{f['files']} file(s) picked up")
        if f.get("sales"):
            parts.append(f"{f['sales']} sales lines")
        if f.get("history"):
            parts.append(f"{f['history']} history rows")
        if f.get("snapshot"):
            parts.append(f"{f['snapshot']} stock rows")
        if f.get("rejected"):
            parts.append(f"⚠️ {f['rejected']} file(s) rejected")
        return "📥 *Sync complete*\n" + ("\n".join("• " + p for p in parts)
                                        if parts else "Nothing new to import.")
    return f"✅ {command} finished."


def queue_command(command: str, reply_to: str | None = None,
                  requested_by: str | None = None, args: dict | None = None) -> bool:
    """Called from the WhatsApp router. Returns False if no agent is enrolled."""
    a = q1("""select id, last_seen_at from agents where pharmacy_id=%s
               order by last_seen_at desc nulls last limit 1""", (PID,))
    if not a:
        return False
    ex("""insert into agent_commands (agent_id, command, args, reply_to, requested_by)
          values (%s,%s,%s,%s,%s)""",
       (a["id"], command, json.dumps(args or {}), reply_to, requested_by))
    return True


def agent_status() -> str:
    a = q1("""select machine_name, agent_version, ingest_mode, db_engine, last_seen_at,
                     suspended
                from agents where pharmacy_id=%s
               order by last_seen_at desc nulls last limit 1""", (PID,))
    if not a:
        return ("No agent is installed on the pharmacy PC yet. Stock levels reflect "
                "only what Dishii has received and sold, not the till.")
    if not a["last_seen_at"]:
        return "Agent is registered but has never connected."
    age = datetime.now(a["last_seen_at"].tzinfo) - a["last_seen_at"]
    mins = int(age.total_seconds() // 60)
    if mins < 5:
        health = "🟢 online"
    elif mins < 120:
        health = f"🟡 last seen {mins} min ago"
    else:
        health = f"🔴 offline for {mins // 60}h"
    return (f"*Pharmacy PC agent*\n{health}\n"
            f"Machine: {a['machine_name'] or '?'}\n"
            f"Mode: {a['ingest_mode']}"
            + (f" ({a['db_engine']})" if a["db_engine"] else "")
            + f"\nVersion: {a['agent_version'] or '?'}")


# ============================================================ POS sales ingest
@router.post("/pos-sales")
async def pos_sales(request: Request, x_agent_token: str | None = Header(None)):
    """Land raw, then apply to the ledger. Two phases on purpose: a parsing bug
    must never be unrecoverable without going back to the pharmacy PC."""
    a = _agent(x_agent_token)
    body = await request.json()
    rows = body.get("rows") or []
    landed = 0

    for r in rows:
        try:
            ex("""insert into pos_sales (pharmacy_id, source, external_id, sold_at,
                        legacy_code, description, qty_pieces, unit_price, line_total,
                        payment_method, raw)
                  values (%s,'phamacore',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict (pharmacy_id, source, external_id) do nothing""",
               (PID, r.get("external_id"), r.get("sold_at"), r.get("legacy_code"),
                r.get("description"), int(r.get("qty_pieces") or 0),
                r.get("unit_price"), r.get("line_total"), r.get("payment_method"),
                json.dumps(r)))
            landed += 1
        except Exception as e:
            log.warning("pos row rejected: %s", e)

    ex("""insert into sync_state (agent_id, stream, last_ts, rows_seen, updated_at)
          values (%s,'sales', now(), %s, now())
          on conflict (agent_id, stream) do update
            set last_ts = now(), rows_seen = sync_state.rows_seen + excluded.rows_seen,
                updated_at = now()""", (a["id"], landed))

    applied = apply_pos_sales()
    return {"ok": True, "landed": landed, "applied": applied}


def _resolve_pieces(raw: dict | None, fallback_pieces: int, pack_size: int | None) -> int:
    """Convert an agent-reported quantity into pieces, using pack_size.

    The agent sends qty_packs / qty_loose / qty_is_packs because only the cloud knows
    pack_size. Older agents (or a hand-built payload) send just qty_pieces, so fall
    back to that. This is the one place packs become pieces — keep it that way.
    """
    ps = max(int(pack_size or 1), 1)
    if isinstance(raw, dict) and raw.get("qty_is_packs"):
        return int(raw.get("qty_packs") or 0) * ps + int(raw.get("qty_loose") or 0)
    return fallback_pieces


def apply_pos_sales(limit: int = 2000) -> int:
    """Turn landed POS rows into ledger movements, FEFO-allocated.

    phAMACore does not record which batch was sold, so we assume the pharmacy
    dispensed oldest-first. That is what good practice says they do, and it is an
    assumption we state out loud rather than hide. Where it is wrong, the daily
    reconciliation catches it.
    """
    # A pack-denominated row ('2W0P') lands with qty_pieces = 0 and its real quantity
    # in raw.qty_packs, because only the cloud knows pack_size. Filtering on
    # qty_pieces > 0 alone would silently drop every such row — never applied, no
    # apply_error, stock never decremented. That is the exact drift this module
    # exists to prevent, so the filter has to admit them and let _resolve_pieces
    # below decide. Genuinely-zero rows are caught after resolution.
    rows = q("""select * from pos_sales
                 where pharmacy_id=%s and applied=false
                   and (qty_pieces > 0
                        or coalesce((raw->>'qty_is_packs')::boolean, false))
                 order by sold_at limit %s""", (PID, limit))
    applied = 0

    for r in rows:
        product = None
        if r["legacy_code"]:
            product = q1("select id, pack_size from products "
                         "where pharmacy_id=%s and legacy_code=%s",
                         (PID, r["legacy_code"]))
        if not product and r["description"]:
            product = q1("""select id, pack_size from products
                             where pharmacy_id=%s and similarity(name,%s) > 0.5
                             order by similarity(name,%s) desc limit 1""",
                         (PID, r["description"], r["description"]))
        if not product:
            ex("update pos_sales set apply_error=%s where id=%s",
               ("no matching product", r["id"]))
            continue

        # Resolve units now that we know pack_size. The agent reports packs and loose
        # pieces separately for '5W0P' notation because it cannot know pack_size;
        # treating 5W0P as 5 pieces would understate a 30s pack by 30x.
        remaining = _resolve_pieces(r["raw"], int(r["qty_pieces"] or 0),
                                    product["pack_size"])
        if remaining <= 0:
            ex("update pos_sales set applied=true, apply_error=%s where id=%s",
               ("zero quantity after unit resolution", r["id"]))
            continue

        batches = q("""select id, qty_pieces from batches
                        where pharmacy_id=%s and product_id=%s and qty_pieces > 0
                        order by expiry_date nulls last, created_at""",
                    (PID, product["id"]))
        if not batches:
            ex("update pos_sales set apply_error=%s where id=%s",
               ("sold but we hold no stock for it — likely missing GRN", r["id"]))
            continue

        try:
            with tx() as cur:
                for b in batches:
                    if remaining <= 0:
                        break
                    take = min(remaining, b["qty_pieces"])
                    apply_movement(cur, b["id"], -take, "pos_sale",
                                   ref_table="pos_sales", ref_id=None,
                                   note=f"phAMACore {r['external_id']}")
                    remaining -= take
                cur.execute("update pos_sales set applied=true, product_id=%s, "
                            "apply_error=%s where id=%s",
                            (product["id"],
                             f"short by {remaining} pcs" if remaining > 0 else None,
                             r["id"]))
            applied += 1
        except Exception as e:
            log.warning("could not apply pos sale %s: %s", r["id"], e)
            ex("update pos_sales set apply_error=%s where id=%s", (str(e)[:200], r["id"]))

    return applied


# ============================================================ history backfill
@router.post("/history")
async def history(request: Request, x_agent_token: str | None = Header(None)):
    """Monthly totals from phAMACore's own 12/24-month screens.

    This is the single highest-value thing the agent does. Without it, avg_daily is
    zero for every product and the system cannot forecast anything for its first 90
    days while 24 months of signal sits unused on that PC.
    """
    a = _agent(x_agent_token)
    rows = (await request.json()).get("rows") or []
    inserted = 0

    for r in rows:
        if not r.get("period") or not (r.get("legacy_code") or r.get("description")):
            continue
        product = None
        if r.get("legacy_code"):
            product = q1("""select id, pack_size from products
                             where pharmacy_id=%s and legacy_code=%s""",
                         (PID, r["legacy_code"]))
        if not product and r.get("description"):
            product = q1("""select id, pack_size from products where pharmacy_id=%s
                             and similarity(name,%s) > 0.5
                             order by similarity(name,%s) desc limit 1""",
                         (PID, r["description"], r["description"]))
        # Resolve packs -> pieces here too. A monthly total in packs stored as pieces
        # would depress avg_daily by pack_size and make every forecast far too low.
        pieces = _resolve_pieces(r, int(r.get("qty_pieces") or 0),
                                 (product or {}).get("pack_size"))
        try:
            ex("""insert into sales_history_monthly (pharmacy_id, product_id, legacy_code,
                        period, qty_pieces, value)
                  values (%s,%s,%s,date_trunc('month', %s::date),%s,%s)
                  on conflict (pharmacy_id, legacy_code, period) do update
                    set qty_pieces = excluded.qty_pieces, value = excluded.value,
                        product_id = coalesce(excluded.product_id,
                                              sales_history_monthly.product_id)""",
               (PID, (product or {}).get("id"), r.get("legacy_code"), r["period"],
                pieces, r.get("value")))
            inserted += 1
        except Exception as e:
            log.warning("history row rejected: %s", e)

    ex("""insert into sync_state (agent_id, stream, last_ts, rows_seen, updated_at)
          values (%s,'history', now(), %s, now())
          on conflict (agent_id, stream) do update
            set last_ts=now(), rows_seen=sync_state.rows_seen + excluded.rows_seen,
                updated_at=now()""", (a["id"], inserted))

    from forecast import recompute_all
    stats = recompute_all()
    return {"ok": True, "inserted": inserted, "forecast": stats}


# ============================================================ stock snapshot
@router.post("/snapshot")
async def snapshot(request: Request, x_agent_token: str | None = Header(None)):
    """phAMACore's current on-hand figures. Not treated as truth — treated as a
    second opinion, and the gap between the two opinions is the product."""
    _agent(x_agent_token)
    rows = (await request.json()).get("rows") or []
    compared, variance_total = 0, 0.0

    ex("""update stock_reconciliation set status='ignored'
           where pharmacy_id=%s and status='open'""", (PID,))

    for r in rows:
        product = None
        if r.get("legacy_code"):
            product = q1("""select id, pack_size, cost_price from products
                             where pharmacy_id=%s and legacy_code=%s""",
                         (PID, r["legacy_code"]))
        if not product:
            continue
        ours = q1("""select coalesce(sum(qty_pieces),0) as n from batches
                      where pharmacy_id=%s and product_id=%s""", (PID, product["id"]))
        # Only multiply by pack_size when the export actually used W/P pack notation.
        # Multiplying a plain piece count would inflate every variance by pack_size
        # and make the headline "unexplained stock" figure fiction.
        pos_pieces = _resolve_pieces(r, int(r.get("qty_pieces") or 0),
                                     product["pack_size"])
        variance = pos_pieces - int(ours["n"])
        if variance == 0:
            continue
        value = variance * float(product["cost_price"] or 0)
        ex("""insert into stock_reconciliation (pharmacy_id, product_id, legacy_code,
                    dishii_pieces, pos_pieces, variance, variance_value, status)
              values (%s,%s,%s,%s,%s,%s,%s,'open')""",
           (PID, product["id"], r.get("legacy_code"), ours["n"], pos_pieces,
            variance, value))
        compared += 1
        variance_total += abs(value)

    return {"ok": True, "variances": compared, "abs_value": round(variance_total, 2)}


def reconciliation_summary(limit: int = 12) -> str:
    rows = q("""select name, legacy_code, dishii_pieces, pos_pieces, variance,
                       variance_value
                  from v_stock_variance where pharmacy_id=%s limit %s""", (PID, limit))
    if not rows:
        return "No stock variances open. Dishii and phAMACore agree."
    total = sum(abs(float(r["variance_value"] or 0)) for r in rows)
    lines = "\n".join(
        f"• {r['name']} — till says {r['pos_pieces']}, we calculate "
        f"{r['dishii_pieces']} ({r['variance']:+d} pcs, {kes(abs(r['variance_value']))})"
        for r in rows
    )
    return (f"⚖️ *Stock variance* — {len(rows)} item(s), {kes(total)} unexplained\n\n"
            f"{lines}\n\n"
            f"A negative figure means fewer on the shelf than received-minus-sold "
            f"accounts for: miscount at receiving, unrecorded sale, or shrinkage.")
