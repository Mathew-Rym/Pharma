"""FLOW D — Scheduled jobs, called by GitHub Actions cron.

Every job writes to job_runs. A silent cron failure during a pilot is how you lose the
client, so make failure visible.
"""
import json
import logging
from datetime import date, timedelta

from config import settings
from db import ex, ex1, q, q1, signed_url
from reports import build_report_pdf, get_expiry_risk, get_reorder_suggestions
from utils import from_pieces, kes
from wa import send_document, send_text

import register
import tenancy
from tenancy import pid          # tenant comes from the request, not from .env

log = logging.getLogger(__name__)


def for_every_tenant(job_fn) -> list[dict]:
    """Run a job once per paired tenant, each with its own tenant bound.

    Three specifics that matter:

    * Only tenants that are actually LIVE, via tenancy.LIVE_SQL -- the same definition
      wa.compose() uses. This used to test gowa_device_id alone, which is weaker: a
      pharmacy that has been issued a slot but whose handset never linked has a device and
      no wa_jid, so it was selected, every send was composed, and every send was then
      refused by deliver()'s JID guard. One registration produced six such messages. The
      alert cannot arrive either way; skipping at selection is the difference between
      silence and a log full of refusals that look like a bug.
    * Platform rows are excluded: no inventory, nobody to alert.
    * Each tenant is caught separately. One bad row must not abort the loop, or a single
      pharmacy with dirty data silences alerts for every other pharmacy -- and cron
      failures are invisible by nature, which is the whole reason job_runs exists.
    """
    tenants = q(f"""select id, name from pharmacies
                     where kind = 'tenant' and {tenancy.LIVE_SQL}
                     order by name""")
    if not tenants:
        log.warning("no paired tenants; %s did not run", getattr(job_fn, "__name__", job_fn))
        return []
    out = []
    for t in tenants:
        try:
            with tenancy.pharmacy_scope(str(t["id"])):
                out.append({"pharmacy": t["name"], **(job_fn() or {})})
        except Exception as e:
            log.exception("job failed for pharmacy %s", t["name"])
            out.append({"pharmacy": t["name"], "status": "error",
                        "error": f"{type(e).__name__}: {e}"})
    return out


def _staff(roles: tuple[str, ...]) -> list[dict]:
    return q(
        """select phone, name, role from staff
            where pharmacy_id=%s and is_active and role = any(%s)""",
        (pid(), list(roles)),
    )


def _run(job: str, fn) -> dict:
    # pharmacy_id is not optional here even though the column is nullable. v2 added it
    # for multi-tenant hygiene and this helper never set it, so every job_runs row was
    # tenant-less -- which the dashboard papered over with `or pharmacy_id is null`.
    # Under RLS a null would fail the WITH CHECK outright and every cron job would
    # start erroring the moment isolation is switched on.
    row = ex1("""insert into job_runs (pharmacy_id, job, status)
                 values (%s,%s,'running') returning id""", (pid(), job))
    try:
        detail = fn() or {}
        ex("update job_runs set status='ok', detail=%s, ended_at=now() where id=%s",
           (json.dumps(detail, default=str), row["id"]))
        return {"job": job, "status": "ok", **detail}
    except Exception as e:
        log.exception("job %s failed", job)
        ex("update job_runs set status='error', detail=%s, ended_at=now() where id=%s",
           (json.dumps({"error": f"{type(e).__name__}: {e}"}), row["id"]))
        return {"job": job, "status": "error", "error": str(e)}


# ------------------------------------------------------------ 07:00 expiry sweep
def expiry_sweep() -> dict:
    def _go():
        buckets = {}
        for days in (30, 60, 90):
            rows = q(
                """select name, batch_no, expiry_date, qty_pieces, value_at_risk, days_left
                     from v_expiry_risk
                    where pharmacy_id=%s and days_left <= %s and days_left > %s
                    order by expiry_date""",
                (pid(), days, days - 30 if days > 30 else -3650),
            )
            buckets[days] = rows

        total = sum(float(r["value_at_risk"] or 0)
                    for rows in buckets.values() for r in rows)
        count = sum(len(r) for r in buckets.values())
        if count == 0:
            return {"batches": 0}

        parts = [f"☀️ *Good morning*\n\n🔔 *Expiry watch* — {count} batch(es), "
                 f"{kes(total)} at risk"]
        labels = {30: "⛔ Within 30 days — push or write off now",
                  60: "⚠️ 31–60 days — discount today",
                  90: "🟡 61–90 days — plan a promo"}
        for days in (30, 60, 90):
            rows = buckets[days]
            if not rows:
                continue
            body = "\n".join(
                f"• {r['name']} — {r['batch_no'] or '?'} — {r['expiry_date']:%b %Y} — "
                f"{r['qty_pieces']} pcs — {kes(r['value_at_risk'])}"
                for r in rows[:8]
            )
            more = f"\n  …and {len(rows) - 8} more" if len(rows) > 8 else ""
            parts.append(f"{labels[days]}\n{body}{more}")
        parts.append("Reply *CLEARANCE* to offer these to loyalty customers, "
                     "or *EXPIRY* for the full list.")
        msg = "\n\n".join(parts)

        recipients = _staff(("owner", "manager", "pharmacist"))
        for s in recipients:
            send_text(s["phone"], msg)

        ex("""insert into alerts (pharmacy_id, kind, severity, payload, sent_to, sent_at)
              values (%s,'expiry_90','warn',%s,%s, now())""",
           (pid(), json.dumps({"count": count, "value": total}),
            [s["phone"] for s in recipients]))
        return {"batches": count, "value_at_risk": total}

    return _run("expiry_sweep", _go)


# ------------------------------------------------------------ 07:05 low stock / PO
def low_stock_check() -> dict:
    def _go():
        rows = q(
            """select s.product_id, s.name, s.pack_size, s.qty_pieces,
                      s.reorder_level_pieces, coalesce(v.avg_daily,0) as avg_daily,
                      p.preferred_supplier_id, sup.name as supplier, sup.phone as sup_phone,
                      p.cost_price
                 from v_stock_on_hand s
                 join products p on p.id = s.product_id
                 left join v_velocity_90d v on v.product_id = s.product_id
                 left join suppliers sup on sup.id = p.preferred_supplier_id
                where s.pharmacy_id=%s
                  and s.qty_pieces <= greatest(s.reorder_level_pieces,
                                               coalesce(v.avg_daily,0) * 14)
                  and coalesce(v.avg_daily,0) > 0
                order by s.qty_pieces / nullif(v.avg_daily,0) asc""",
            (pid(),),
        )
        if not rows:
            return {"items": 0}

        # group by supplier so each supplier gets one draft PO
        by_sup: dict = {}
        for r in rows:
            by_sup.setdefault(r["preferred_supplier_id"], []).append(r)

        created = []
        for sup_id, items in by_sup.items():
            if not sup_id:
                continue
            est = sum(float(i["cost_price"] or 0) * _suggest_qty(i) for i in items)
            po = ex1(
                """insert into purchase_orders (pharmacy_id, supplier_id, status, reason,
                                                total_estimate)
                   values (%s,%s,'awaiting_approval',%s,%s) returning id""",
                (pid(), sup_id, json.dumps({"trigger": "reorder_level"}), est),
            )
            for i in items:
                qty = _suggest_qty(i)
                ex("""insert into po_lines (po_id, product_id, qty_pieces, unit_cost, rationale)
                      values (%s,%s,%s,%s,%s)""",
                   (po["id"], i["product_id"], qty, i["cost_price"],
                    f"{i['qty_pieces']} pcs left, {i['avg_daily']:.1f}/day, "
                    f"{i['qty_pieces'] / i['avg_daily']:.0f}d cover"))
            created.append((po["id"], items[0]["supplier"], est, len(items)))

        lines = "\n".join(
            f"• {r['name']} — {from_pieces(r['qty_pieces'], r['pack_size'])} left, "
            f"{r['qty_pieces'] / r['avg_daily']:.0f}d cover"
            for r in rows[:12]
        )
        msg = (f"📉 *Reorder needed* — {len(rows)} item(s)\n\n{lines}\n\n"
               + ("\n".join(f"Draft PO for *{name}*: {n} items, ~{kes(est)}"
                            for _, name, est, n in created) if created else "")
               + "\n\nOpen the dashboard to approve and send, or reply *ORDER* for the list.")
        for s in _staff(("owner", "manager")):
            send_text(s["phone"], msg)
        return {"items": len(rows), "pos": len(created)}

    return _run("low_stock_check", _go)


def _suggest_qty(row: dict) -> int:
    """Order up to 30 days of cover, rounded up to whole packs."""
    target = float(row["avg_daily"] or 0) * 30
    need = max(target - float(row["qty_pieces"] or 0), 0)
    ps = max(int(row["pack_size"] or 1), 1)
    packs = int(-(-need // ps))
    return packs * ps


# ------------------------------------------------------------ 20:00 daily digest
def daily_digest() -> dict:
    def _go():
        today = date.today()
        fin = q1(
            """select count(distinct o.id) as orders, coalesce(sum(o.total),0) as revenue
                 from orders o
                where o.pharmacy_id=%s
                  and o.status in ('paid','packed','dispatched','delivered')
                  and o.created_at::date = %s""",
            (pid(), today),
        )
        recv = q1(
            """select count(*) as n, coalesce(sum(net_total),0) as v
                 from grns where pharmacy_id=%s and status='approved'
                  and approved_at::date = %s""",
            (pid(), today),
        )
        top = q(
            """select p.name, -sum(m.delta_pieces) as pieces
                 from stock_movements m
                 join batches b on b.id=m.batch_id join products p on p.id=b.product_id
                where m.pharmacy_id=%s and m.reason='sale' and m.created_at::date=%s
                group by p.name order by pieces desc limit 5""",
            (pid(), today),
        )
        pending = q1(
            """select count(*) as n from prescriptions
                where pharmacy_id=%s and status='pending_verification'""",
            (pid(),),
        )
        msg = (f"🌙 *{today:%A %d %b} summary*\n\n"
               f"• Revenue: {kes(fin['revenue'])} from {fin['orders']} order(s)\n"
               f"• Stock received: {recv['n']} delivery(ies), {kes(recv['v'])}\n"
               + (f"• Top sellers: " + ", ".join(f"{t['name'][:22]} ({t['pieces']})"
                                                 for t in top) + "\n" if top else "")
               + (f"\n⚠️ {pending['n']} prescription(s) still awaiting verification"
                  if pending["n"] else ""))
        for s in _staff(("owner", "manager")):
            send_text(s["phone"], msg)
        return {"revenue": float(fin["revenue"]), "orders": fin["orders"]}

    return _run("daily_digest", _go)


# ------------------------------------------------------------ Monday weekly PDF
def weekly_report() -> dict:
    def _go():
        path, fname = build_report_pdf("week")
        url = signed_url(settings.BUCKET_DOCS, path, 604800)
        for s in _staff(("owner", "manager")):
            send_document(s["phone"], url, fname, "Your weekly pharmacy report")
        return {"file": fname}

    return _run("weekly_report", _go)


# ------------------------------------------------------------ refill reminders
def refill_reminders() -> dict:
    def _go():
        rows = q(
            """select distinct c.phone, c.name, p.name as drug, o.created_at,
                      l.qty_pieces
                 from orders o
                 join customers c on c.id = o.customer_id
                 join order_lines l on l.order_id = o.id
                 join products p on p.id = l.product_id
                where o.pharmacy_id=%s and o.status in ('delivered','dispatched','paid')
                  and c.marketing_opt_in = true
                  and o.created_at::date = current_date - 25
                limit 50""",
            (pid(),),
        )
        for r in rows:
            send_text(r["phone"],
                      f"Hello{(' ' + r['name']) if r['name'] else ''}, your "
                      f"{r['drug']} should be running low around now. "
                      f"Reply *REFILL* and we will prepare it for delivery.")
        return {"reminders": len(rows)}

    return _run("refill_reminders", _go)


# ------------------------------------------------------------ every 15 min
def reconcile() -> dict:
    def _go():
        stuck = q(
            """select id, order_id, phone from payments
                where status='pending' and created_at < now() - interval '10 minutes'""",
        )
        for p in stuck:
            ex("update payments set status='timeout' where id=%s", (p["id"],))
            if p["phone"]:
                send_text(p["phone"], "Your M-Pesa prompt expired. Reply *CONFIRM* "
                                      "to try again.")

        unhandled = q1(
            """select count(*) as n from wa_messages
                where direction='in' and handled=false
                  and created_at < now() - interval '5 minutes'""",
        )
        if unhandled["n"]:
            for s in _staff(("owner",)):
                send_text(s["phone"],
                          f"⚙️ {unhandled['n']} message(s) were not processed. "
                          "The team has been alerted.")

        # ledger invariant — the one number that must never drift
        drift = q(
            """select b.id, b.qty_pieces, coalesce(sum(m.delta_pieces),0) as ledger
                 from batches b
                 left join stock_movements m on m.batch_id = b.id
                where b.pharmacy_id=%s
                group by b.id, b.qty_pieces
               having b.qty_pieces <> coalesce(sum(m.delta_pieces),0)
                limit 20""",
            (pid(),),
        )
        if drift:
            log.error("LEDGER DRIFT on %s batches: %s", len(drift), drift[:3])

        return {"timeouts": len(stuck), "unhandled": unhandled["n"], "drift": len(drift)}

    return _run("reconcile", _go)


def forecast_refresh() -> dict:
    """Rebuild the seasonal demand baseline. Runs before the reorder digest so the
    numbers the owner sees at 07:06 were computed minutes earlier, not yesterday."""
    def _go():
        from forecast import recompute_all
        return recompute_all()
    return _run("forecast_refresh", _go)


def variance_report() -> dict:
    """Received-minus-sold vs what the till says is on the shelf. The gap is
    miscounts, unrecorded sales and shrinkage — the number an owner acts on."""
    def _go():
        from agent_api import reconciliation_summary
        rows = q("""select count(*) as n, coalesce(sum(abs(variance_value)),0) as v
                      from stock_reconciliation
                     where pharmacy_id=%s and status='open' and variance <> 0""", (pid(),))
        if not rows or rows[0]["n"] == 0:
            return {"variances": 0}
        summary = reconciliation_summary()
        for s in _staff(("owner",)):
            send_text(s["phone"], summary)
        return {"variances": rows[0]["n"], "value": float(rows[0]["v"])}
    return _run("variance_report", _go)


# Jobs that run ONCE, across the whole platform, rather than once per tenant.
#
# activation_sweep is the only one so far, and it has to be here rather than in JOBS
# because for_every_tenant() selects pharmacies that are already paired -- precisely the
# ones activation has nothing left to do for. Running it through that loop would mean a
# pharmacy waiting to be linked is never looked at, which is the one state this job exists
# to get out of.
GLOBAL_JOBS = {
    "activation_sweep": register.activation_sweep,
}

JOBS = {
    "expiry_sweep": expiry_sweep,
    "forecast_refresh": forecast_refresh,
    "variance_report": variance_report,
    "low_stock_check": low_stock_check,
    "daily_digest": daily_digest,
    "weekly_report": weekly_report,
    "refill_reminders": refill_reminders,
    "reconcile": reconcile,
}
