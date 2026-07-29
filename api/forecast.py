"""Demand forecasting. No ML library, and that is a deliberate choice.

A pharmacy owner will not act on a number he cannot interrogate. Every forecast here
carries a `method` string in plain English, so when the system says "order 220 packs"
the answer to "why?" is one line, not a model card.

The method is seasonal-naive with a blended baseline:

    baseline  = live ledger sales if we have >= 21 days of them,
                else backfilled phAMACore monthly history,
                else nothing (and we say so, rather than forecasting zero)

    seasonal  = this month's historical average / that product's annual average
                e.g. an allergy drug at 1.8 in August, 0.6 in January

    forecast  = baseline * season_index * 30
    cover     = on_hand / (baseline * season_index)

The seasonal index is what answers the thing the pharmacist actually told you:
"some meds are a hit at different seasons where docs prescribe them to many patients
and then they just aren't any more." You cannot see that in a 90-day trailing average.
You can see it in 24 months of monthly totals, which is why the history backfill is
the highest-value thing the agent does.
"""
import json
import logging
from datetime import date

from config import settings
from db import ex, ex1, q, q1
from utils import from_pieces, kes

log = logging.getLogger(__name__)
from tenancy import pid          # tenant comes from the request, not from .env


def recompute_all() -> dict:
    """Rebuild the forecast cache. Cheap enough to run on every history import."""
    month = date.today().month
    rows = q("""select b.product_id, b.name, b.avg_daily, b.confidence, b.method,
                       coalesce(s.season_index, 1.0) as season_index,
                       coalesce(oh.qty_pieces, 0) as on_hand,
                       p.pack_size, p.cost_price
                  from v_demand_baseline b
                  join products p on p.id = b.product_id
                  left join v_seasonality s
                         on s.product_id = b.product_id and s.mo = %s
                  left join v_stock_on_hand oh on oh.product_id = b.product_id
                 where b.pharmacy_id = %s""", (month, pid()))

    written, with_signal = 0, 0
    for r in rows:
        base = float(r["avg_daily"] or 0)
        season = float(r["season_index"] or 1.0)
        # clamp the seasonal multiplier: a single freak month must not triple an order
        season = max(0.4, min(season, 2.5))
        effective = base * season
        forecast30 = int(round(effective * 30))
        cover = (float(r["on_hand"]) / effective) if effective > 0 else None

        if base > 0:
            with_signal += 1
            method = r["method"]
            if abs(season - 1.0) > 0.15:
                direction = "higher" if season > 1 else "lower"
                method += f"; {date.today():%B} runs {season:.1f}x {direction} than average"
        else:
            method = "no demand signal yet — import phAMACore history to fix this"

        ex("""insert into demand_forecast (product_id, pharmacy_id, avg_daily,
                    season_index, forecast_30d, days_of_cover, confidence, method,
                    computed_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s, now())
              on conflict (product_id) do update
                set avg_daily=excluded.avg_daily, season_index=excluded.season_index,
                    forecast_30d=excluded.forecast_30d,
                    days_of_cover=excluded.days_of_cover,
                    confidence=excluded.confidence, method=excluded.method,
                    computed_at=now()""",
           (r["product_id"], pid(), round(base, 3), round(season, 3), forecast30,
            round(cover, 1) if cover is not None else None,
            r["confidence"], method[:300]))
        written += 1

    log.info("forecast recomputed: %s products, %s with a demand signal",
             written, with_signal)
    return {"products": written, "with_signal": with_signal,
            "without_signal": written - with_signal}


def reorder_list(limit: int = 25) -> list[dict]:
    """What to order, why, and from whom. Ordered by urgency, not alphabetically."""
    return q("""select f.product_id, p.name, p.legacy_code, p.pack_size, p.cost_price,
                       f.avg_daily, f.season_index, f.forecast_30d, f.days_of_cover,
                       f.confidence, f.method,
                       coalesce(oh.qty_pieces,0) as on_hand,
                       sup.id as supplier_id, sup.name as supplier, sup.phone as sup_phone,
                       coalesce(sup.lead_time_days, 2) as lead_days
                  from demand_forecast f
                  join products p on p.id = f.product_id
                  left join v_stock_on_hand oh on oh.product_id = f.product_id
                  left join suppliers sup on sup.id = p.preferred_supplier_id
                 where f.pharmacy_id = %s
                   and f.avg_daily > 0
                   and f.days_of_cover is not null
                   and f.days_of_cover <= (coalesce(sup.lead_time_days,2) + 10)
                 order by f.days_of_cover asc
                 limit %s""", (pid(), limit))


def suggest_qty(row: dict, target_days: int = 30) -> int:
    """Top up to target_days of seasonally-adjusted cover, rounded to whole packs.

    Rounding UP to a pack is not laziness — suppliers sell packs, and asking for
    17 pieces of a 30s pack makes the pharmacy look like it does not know its trade.
    """
    effective = float(row["avg_daily"] or 0) * float(row["season_index"] or 1)
    need = max(effective * target_days - float(row["on_hand"] or 0), 0)
    ps = max(int(row["pack_size"] or 1), 1)
    packs = int(-(-need // ps))
    return packs * ps


def reorder_message(limit: int = 12) -> str:
    """The WhatsApp text a pharmacist can act on with one reply."""
    rows = reorder_list(limit)
    if not rows:
        no_signal = q1("""select count(*) as n from demand_forecast
                           where pharmacy_id=%s and avg_daily = 0""", (pid(),))
        if no_signal and no_signal["n"] > 20:
            return (f"Nothing to reorder from what I can see — but {no_signal['n']} "
                    f"products have no sales history in the system yet. Reply *SYNC* "
                    f"to pull history from the pharmacy PC so I can forecast properly.")
        return "Nothing needs reordering right now."

    by_supplier: dict = {}
    for r in rows:
        by_supplier.setdefault(r["supplier"] or "No supplier set", []).append(r)

    out = ["📋 *Reorder suggestions*"]
    total = 0.0
    for supplier, items in by_supplier.items():
        lines = []
        for r in items:
            qty = suggest_qty(r)
            cost = qty * float(r["cost_price"] or 0)
            total += cost
            conf = {"high": "", "medium": " (medium confidence)",
                    "low": " (low confidence — check this one)"}[r["confidence"]]
            lines.append(
                f"• {r['name']} — order {from_pieces(qty, r['pack_size'])}\n"
                f"  {r['days_of_cover']:.0f} days cover left · {r['method']}{conf}"
            )
        out.append(f"*{supplier}*\n" + "\n".join(lines))

    out.append(f"Estimated total: {kes(total)}\n"
               f"Reply *PO* to create draft orders, or *PO <supplier>* for just one.")
    return "\n\n".join(out)


def create_draft_pos(staff_id: str | None = None,
                     supplier_filter: str | None = None) -> list[dict]:
    """Turn the suggestion list into draft purchase orders awaiting one-tap approval."""
    rows = reorder_list(60)
    if supplier_filter:
        rows = [r for r in rows
                if r["supplier"] and supplier_filter.lower() in r["supplier"].lower()]

    by_sup: dict = {}
    for r in rows:
        if r["supplier_id"]:
            by_sup.setdefault(r["supplier_id"], []).append(r)

    created = []
    for sup_id, items in by_sup.items():
        est = sum(suggest_qty(i) * float(i["cost_price"] or 0) for i in items)
        po = ex1("""insert into purchase_orders (pharmacy_id, supplier_id, status, reason,
                           total_estimate)
                    values (%s,%s,'awaiting_approval',%s,%s) returning id""",
                 (pid(), sup_id,
                  json.dumps({"trigger": "forecast", "method": "seasonal_naive",
                              "requested_by": staff_id}), est))
        if not po:
            continue
        for i in items:
            qty = suggest_qty(i)
            ex("""insert into po_lines (po_id, product_id, qty_pieces, unit_cost, rationale)
                  values (%s,%s,%s,%s,%s)""",
               (po["id"], i["product_id"], qty, i["cost_price"],
                f"{i['on_hand']} pcs on hand, {i['avg_daily']:.1f}/day "
                f"x{i['season_index']:.2f} seasonal, {i['days_of_cover']:.0f}d cover"))
        created.append({"po_id": str(po["id"]), "supplier": items[0]["supplier"],
                        "items": len(items), "estimate": est,
                        "phone": items[0]["sup_phone"]})
    return created


def forecast_explain(product_query: str) -> str:
    """'why are you telling me to order prenor' — answer in one message."""
    r = q1("""select p.name, p.pack_size, f.avg_daily, f.season_index, f.forecast_30d,
                     f.days_of_cover, f.confidence, f.method,
                     coalesce(oh.qty_pieces,0) as on_hand
                from demand_forecast f
                join products p on p.id = f.product_id
                left join v_stock_on_hand oh on oh.product_id = f.product_id
               where f.pharmacy_id=%s and similarity(p.name,%s) > 0.3
               order by similarity(p.name,%s) desc limit 1""",
           (pid(), product_query, product_query))
    if not r:
        return f"No forecast for '{product_query}'."

    hist = q("""select to_char(period,'Mon YY') as m, qty_pieces
                  from sales_history_monthly h
                  join products p on p.id = h.product_id
                 where p.pharmacy_id=%s and similarity(p.name,%s) > 0.3
                 order by period desc limit 6""", (pid(), product_query))
    trail = ("\nRecent months: " + ", ".join(f"{h['m']} {h['qty_pieces']}"
                                             for h in reversed(hist))) if hist else ""

    return (f"*{r['name']}*\n"
            f"On hand: {from_pieces(r['on_hand'], r['pack_size'])}\n"
            f"Selling: {float(r['avg_daily'] or 0):.1f} pcs/day"
            f" (x{float(r['season_index'] or 1):.2f} this month)\n"
            f"Next 30 days: about {r['forecast_30d']} pcs\n"
            f"Cover: {r['days_of_cover'] or 0:.0f} days\n"
            f"Basis: {r['method']}{trail}")
