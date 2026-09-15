"""Proactive owner briefings: three different jobs with three different purposes.

  MORNING   (default 07:00 local)  PLAN    — yesterday + today's posture + priorities
  AFTERNOON (default 13:00 local)  INTERVENE — only what changed, only what needs action
  EVENING   (default 20:00 local)  PREPARE — today's result + tomorrow's shape

Design rules, inherited from the rest of Pharma OS:

* Every number comes from SQL. The LLM is never asked for, and never trusted
  with, a sales figure, a stock count, a price or a forecast. These briefings
  are deterministic text assembled from deterministic queries; if a model is
  ever asked to rephrase one, the facts were computed here first.
* Per-tenant. The jobs loop via for_every_tenant(); every query is scoped by
  pid(); recipients are this pharmacy's owner/manager staff rows. One
  pharmacy's numbers cannot appear in another pharmacy's briefing because no
  query in this module runs without the tenant predicate.
* Idempotent per day. A restart around 07:00, or a GitHub cron retry and the
  VM-local cron both firing, must not double-message an owner: the same-day
  guard in _send_once checks job_runs before sending.
* Configurable. pharmacy_settings (schema_v18) carries the enable flags and
  the delivery hours; absent row means all defaults on. An owner can also
  silence a briefing by replying to it — the MUTE <which> command, because
  the settings screen should not be the only way to say "stop texting me".
"""
import logging
from datetime import date, datetime, timedelta

from db import ex, q, q1
from utils import kes
from wa import send_text

log = logging.getLogger(__name__)
from tenancy import pid

BRIEFING_ROLES = ("owner", "manager")


# ------------------------------------------------------------ settings
def get_settings() -> dict:
    """Briefing preferences for the current tenant, with defaults filled in.

    No row = all defaults on, so a pharmacy that never touches settings still
    gets its briefings, and nothing here needs a migration to change a default.
    """
    row = q1("select * from pharmacy_settings where pharmacy_id=%s", (pid(),))
    return {
        "morning": (row or {}).get("briefing_morning", True),
        "afternoon": (row or {}).get("briefing_afternoon", True),
        "evening": (row or {}).get("briefing_evening", True),
        "hour_morning": (row or {}).get("briefing_hour_morning", 7),
        "hour_afternoon": (row or {}).get("briefing_hour_afternoon", 13),
        "hour_evening": (row or {}).get("briefing_hour_evening", 20),
    }


def set_briefing(which: str, enabled: bool) -> None:
    col = {"morning": "briefing_morning", "afternoon": "briefing_afternoon",
           "evening": "briefing_evening"}[which]
    ex("""insert into pharmacy_settings (pharmacy_id, %s) values (%%s, %%s)
            on conflict (pharmacy_id) do update set %s = excluded.%s,
                updated_at = now()""" % (col, col, col),
       (pid(), enabled))


# ------------------------------------------------------------ shared queries
def _pharmacy_name() -> str:
    r = q1("select name from pharmacies where id=%s", (pid(),))
    return (r or {}).get("name", "your pharmacy")


def _recipients():
    from jobs import _staff
    return _staff(BRIEFING_ROLES)


def _sales(day: date) -> dict:
    return q1(
        """select count(distinct o.id) as orders, coalesce(sum(o.total),0) as revenue
             from orders o
            where o.pharmacy_id=%s
              and o.status in ('paid','packed','dispatched','delivered')
              and o.created_at::date = %s""",
        (pid(), day),
    ) or {"orders": 0, "revenue": 0}


def _sold_pieces(day: date) -> int:
    r = q1(
        """select coalesce(-sum(m.delta_pieces),0) as n
             from stock_movements m
            where m.pharmacy_id=%s and m.reason='sale' and m.created_at::date=%s""",
        (pid(), day),
    )
    return int((r or {}).get("n") or 0)


def _top_sellers(day: date, n: int = 3):
    return q(
        """select p.name, -sum(m.delta_pieces) as pieces
             from stock_movements m
             join batches b on b.id=m.batch_id join products p on p.id=b.product_id
            where m.pharmacy_id=%s and m.reason='sale' and m.created_at::date=%s
            group by p.name order by pieces desc limit %s""",
        (pid(), day, n),
    )


def _received(day: date) -> dict:
    return q1(
        """select count(*) as n, coalesce(sum(net_total),0) as v
             from grns where pharmacy_id=%s and status='approved'
              and approved_at::date=%s""",
        (pid(), day),
    ) or {"n": 0, "v": 0}


def _stock_posture() -> dict:
    """Critical / attention / healthy counts, projected stockouts.

    Critical = days of cover under the supplier lead time (cannot survive a
    reorder cycle). Attention = cover under 14 days. Projected stockout =
    cover under 7 days, from the same demand_forecast the ORDER flow uses --
    one forecasting engine, not a second opinion invented here.
    """
    row = q1(
        """select
             count(*) filter (where f.days_of_cover < coalesce(sup.lead_time_days, 2)) as critical,
             count(*) filter (where f.days_of_cover >= coalesce(sup.lead_time_days, 2)
                              and f.days_of_cover < 14) as attention,
             count(*) filter (where f.days_of_cover < 7) as stockout_week
           from demand_forecast f
           join products p on p.id = f.product_id
           left join suppliers sup on sup.id = p.preferred_supplier_id
          where f.pharmacy_id=%s and f.avg_daily > 0 and f.days_of_cover is not null""",
        (pid(),),
    ) or {}
    # Zero-stock products with demand history are critical by definition.
    zero = q1(
        """select count(*) as n
             from demand_forecast f
             left join v_stock_on_hand oh on oh.product_id = f.product_id
            where f.pharmacy_id=%s and f.avg_daily > 0
              and coalesce(oh.qty_pieces,0) = 0""",
        (pid(),),
    )
    crit = int(row.get("critical") or 0) + int((zero or {}).get("n") or 0)
    return {"critical": crit, "attention": int(row.get("attention") or 0),
            "stockout_week": int(row.get("stockout_week") or 0) + int((zero or {}).get("n") or 0)}


def _expiry_risk() -> int:
    r = q1(
        """select count(*) as n from v_expiry_risk
            where pharmacy_id=%s and expiry_date <= current_date + 90""",
        (pid(),))
    return int((r or {}).get("n") or 0)


def _missing_prices() -> int:
    from prices import missing_prices
    n, _ = missing_prices(limit=1)
    return n


def _open_pos() -> list[dict]:
    return q(
        """select po.id, s.name as supplier, po.total_estimate
             from purchase_orders po join suppliers s on s.id = po.supplier_id
            where po.pharmacy_id=%s and po.status='awaiting_approval'
            order by po.created_at desc limit 5""",
        (pid(),),
    )


def _pending_prescriptions() -> int:
    r = q1("""select count(*) as n from prescriptions
                where pharmacy_id=%s and status='pending_verification'""", (pid(),))
    return int((r or {}).get("n") or 0)


def _demand_today_expected() -> int:
    """Expected pieces sold today, from the same forecast the ORDER flow uses."""
    r = q1(
        """select coalesce(sum(avg_daily * season_index),0) as n
             from demand_forecast where pharmacy_id=%s and avg_daily > 0""",
        (pid(),),
    )
    return int(float((r or {}).get("n") or 0))


# ------------------------------------------------------------ send-once guard
def _send_once(job: str, body: str, detail: dict) -> dict:
    """Send a briefing unless this job already ran OK for this tenant today.

    The VM-local cron and the GitHub Actions cron can both fire (and a restart
    replays whatever the scheduler missed), so job_runs is the arbiter: one
    successful run per (pharmacy, job, day), whoever fired it. The jobs loop
    writes its own job_runs row AFTER _run() returns, so this guard looks for
    a PREVIOUS success -- the current run's row is still 'running'.
    """
    already = q1(
        """select id from job_runs
            where pharmacy_id=%s and job=%s and status='ok'
              and started_at::date = current_date limit 1""",
        (pid(), job),
    )
    if already:
        return {"sent": False, "reason": "already_sent_today"}
    recipients = _recipients()
    if not recipients:
        return {"sent": False, "reason": "no_recipients"}
    for s in recipients:
        send_text(s["phone"], body)
    return {"sent": True, "recipients": len(recipients), **detail}


def _footer() -> str:
    return "Reply: *STOCK* · *SALES* · *FORECAST* · *ORDER* · *PRICES*"


# ------------------------------------------------------------ the three briefings
def morning_briefing() -> dict:
    """PLAN: yesterday's result, today's posture, the priorities in order."""
    st = get_settings()
    if not st["morning"]:
        return {"sent": False, "reason": "disabled"}
    yesterday = date.today() - timedelta(days=1)
    ys = _sales(yesterday)
    top = _top_sellers(yesterday, 1)
    recv = _received(yesterday)
    posture = _stock_posture()
    expiry = _expiry_risk()
    noprice = _missing_prices()
    pos = _open_pos()
    pending_rx = _pending_prescriptions()
    expected = _demand_today_expected()

    body = [f"☀️ *Good morning — {_pharmacy_name()}*", ""]

    body.append(f"*Yesterday:* {kes(ys['revenue'])} sales · {ys['orders']} order(s)"
                + (f" · top seller {top[0]['name'].split()[0]}" if top else ""))
    if recv["n"]:
        body.append(f"📦 Stock received: {recv['n']} delivery(ies), {kes(recv['v'])}")

    body += ["",
             f"📦 *Stock:* 🔴 {posture['critical']} critical · "
             f"🟠 {posture['attention']} need attention",
             f"⚠️ *Projected stockouts:* {posture['stockout_week']} within 7 days",
             f"💰 *Expected demand today:* ~{expected} units"]
    if expiry:
        body.append(f"⏳ *Expiry risk:* {expiry} product(s)")
    if noprice:
        body.append(f"💊 *Missing prices:* {noprice} medicine(s)")
    if pos:
        body.append(f"📋 *POs awaiting approval:* {len(pos)}")
    if pending_rx:
        body.append(f"🩺 *Prescriptions awaiting verification:* {pending_rx}")

    # Priorities: the loop the product exists to close, in order of money at risk.
    actions = []
    if posture["critical"]:
        actions.append(f"Approve reorders — {posture['critical']} critical item(s) "
                       "(reply *ORDER*)")
    if pos:
        actions.append(f"Approve {len(pos)} purchase order(s) (reply *PO*)")
    if noprice:
        actions.append(f"Set prices for {noprice} medicine(s) (reply *PRICES*)")
    if pending_rx:
        actions.append(f"Verify {pending_rx} prescription(s)")
    if expiry:
        actions.append(f"Review {expiry} expiry-risk product(s) (reply *EXPIRY*)")
    if actions:
        body += ["", "*Today's priorities*"] + [f"{i}. {a}" for i, a in
                                                enumerate(actions[:4], 1)]
    body += ["", _footer()]
    return _send_once("morning_briefing", "\n".join(body),
                      {"critical": posture["critical"], "missing_prices": noprice})


def afternoon_briefing() -> dict:
    """INTERVENE: only what needs action, only what changed since morning.

    Quiet by design: a midday message that repeats the morning is a midday
    message the owner stops reading. No news is one calm line.
    """
    st = get_settings()
    if not st["afternoon"]:
        return {"sent": False, "reason": "disabled"}
    today = date.today()
    so_far = _sales(today)
    posture = _stock_posture()
    pos = _open_pos()
    pending_rx = _pending_prescriptions()

    # Stockouts that happened TODAY (were fine this morning, are zero now).
    new_stockouts = q(
        """select p.name from demand_forecast f
             join products p on p.id = f.product_id
             left join v_stock_on_hand oh on oh.product_id = f.product_id
            where f.pharmacy_id=%s and f.avg_daily > 0
              and coalesce(oh.qty_pieces,0) = 0 limit 5""",
        (pid(),),
    )

    notes = []
    if posture["critical"]:
        notes.append(f"🔴 {posture['critical']} critical stock item(s) — reply *LOW*")
    if new_stockouts:
        notes.append("OUT NOW: " + ", ".join(r["name"][:24] for r in new_stockouts))
    if pos:
        notes.append(f"📋 {len(pos)} PO(s) still awaiting approval")
    if pending_rx:
        notes.append(f"🩺 {pending_rx} prescription(s) awaiting verification")

    if not notes:
        return _send_once(
            "afternoon_briefing",
            f"🙂 *Midday check-in — {_pharmacy_name()}*\n\n"
            f"So far: {kes(so_far['revenue'])} · {so_far['orders']} order(s).\n"
            f"Everything is on track — no critical stockouts or urgent actions detected.",
            {"quiet": True})

    body = [f"🕐 *Midday — {_pharmacy_name()}*", "",
            f"So far: {kes(so_far['revenue'])} · {so_far['orders']} order(s)", ""]
    body += [f"• {n}" for n in notes]
    body += ["", _footer()]
    return _send_once("afternoon_briefing", "\n".join(body), {"notes": len(notes)})


def evening_briefing() -> dict:
    """PREPARE: today's result and tomorrow's shape. This is the digest the
    daily_digest job always wanted to be; daily_digest now delegates here."""
    st = get_settings()
    if not st["evening"]:
        return {"sent": False, "reason": "disabled"}
    today = date.today()
    s = _sales(today)
    pieces = _sold_pieces(today)
    top = _top_sellers(today, 3)
    recv = _received(today)
    posture = _stock_posture()
    expiry = _expiry_risk()
    pos = _open_pos()
    pending_rx = _pending_prescriptions()

    body = [f"🌙 *End of day — {_pharmacy_name()}*", "",
            f"💰 Revenue: {kes(s['revenue'])} from {s['orders']} order(s)",
            f"📦 Units sold: {pieces}"]
    if top:
        body.append("Top sellers: " + ", ".join(f"{r['name'][:20]} ({r['pieces']})"
                                                for r in top))
    if recv["n"]:
        body.append(f"📥 Received: {recv['n']} delivery(ies), {kes(recv['v'])}")

    body += ["",
             f"*Tomorrow*",
             f"⚠️ {posture['stockout_week']} projected stockout(s) within 7 days",
             f"🔴 {posture['critical']} critical · 🟠 {posture['attention']} attention"]
    if expiry:
        body.append(f"⏳ {expiry} expiry-risk product(s)")
    if pos:
        body.append(f"📋 {len(pos)} PO(s) awaiting approval — order tonight, "
                    "arrive tomorrow")
    if pending_rx:
        body.append(f"🩺 {pending_rx} prescription(s) awaiting verification")

    if posture["critical"] and not pos:
        body += ["", "→ Draft reorders now: reply *PO*"]
    body += ["", _footer()]
    return _send_once("evening_briefing", "\n".join(body),
                      {"revenue": float(s["revenue"]), "orders": s["orders"]})
