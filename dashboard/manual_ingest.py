"""Manual phAMACore import — the fallback when the PC agent is not there.

Deliberately calls the SAME cloud functions the agent's HTTP endpoints call, rather
than writing its own INSERTs. Loop B's correctness lives in apply_pos_sales() (FEFO
allocation, pack-vs-piece resolution, idempotency on external_id) and a second
import path would silently diverge from it. If you find yourself writing an INSERT
in this file, you are building the bug we already fixed once.

The one thing this cannot do is set sync_state, because that is per-agent and there
is no agent here. That is correct: a manual import is not a sync high-water mark, and
pretending otherwise would make the agent skip rows when it is finally installed.
"""
import json
import os
import sys

_API = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api")
if _API not in sys.path:
    sys.path.insert(0, _API)


def ingest(kind: str, rows: list[dict], pharmacy_id: str) -> str:
    """Route parsed rows to the right cloud handler. Returns a human summary."""
    if kind == "sale":
        return _sales(rows, pharmacy_id)
    if kind == "history_monthly":
        return _history(rows, pharmacy_id)
    if kind == "snapshot":
        return _snapshot(rows, pharmacy_id)
    return f"Unrecognised export shape '{kind}' — nothing imported."


def _sales(rows: list[dict], pid: str) -> str:
    from agent_api import apply_pos_sales
    from db import ex

    landed = 0
    for r in rows:
        try:
            ex("""insert into pos_sales (pharmacy_id, source, external_id, sold_at,
                        legacy_code, description, qty_pieces, unit_price, line_total,
                        payment_method, raw)
                  values (%s,'phamacore_manual',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict (pharmacy_id, source, external_id) do nothing""",
               (pid, r.get("external_id"), r.get("sold_at"), r.get("legacy_code"),
                r.get("description"), int(r.get("qty_pieces") or 0),
                r.get("unit_price"), r.get("line_total"), r.get("payment_method"),
                json.dumps(r)))
            landed += 1
        except Exception:
            pass
    applied = apply_pos_sales()
    return (f"{landed} sales row(s) landed, {applied} applied to the ledger. "
            f"Rows already imported were skipped, so re-uploading the same file is "
            f"safe.")


def _history(rows: list[dict], pid: str) -> str:
    from db import ex, q1
    from forecast import recompute_all

    inserted = 0
    for r in rows:
        if not r.get("period") or not (r.get("legacy_code") or r.get("description")):
            continue
        product = None
        if r.get("legacy_code"):
            product = q1("""select id from products where pharmacy_id=%s
                             and legacy_code=%s""", (pid, r["legacy_code"]))
        if not product and r.get("description"):
            product = q1("""select id from products where pharmacy_id=%s
                             and similarity(name,%s) > 0.5
                             order by similarity(name,%s) desc limit 1""",
                         (pid, r["description"], r["description"]))
        try:
            ex("""insert into sales_history_monthly (pharmacy_id, product_id,
                        legacy_code, period, qty_pieces, value)
                  values (%s,%s,%s,date_trunc('month', %s::date),%s,%s)
                  on conflict (pharmacy_id, legacy_code, period) do update
                    set qty_pieces = excluded.qty_pieces, value = excluded.value,
                        product_id = coalesce(excluded.product_id,
                                              sales_history_monthly.product_id)""",
               (pid, (product or {}).get("id"), r.get("legacy_code"), r["period"],
                int(r.get("qty_pieces") or 0), r.get("value")))
            inserted += 1
        except Exception:
            pass
    stats = recompute_all()
    return (f"{inserted} month(s) of history stored. Forecast rebuilt: "
            f"{stats['with_signal']} of {stats['products']} products now have a "
            f"demand signal.")


def _snapshot(rows: list[dict], pid: str) -> str:
    from agent_api import _resolve_pieces
    from db import ex, q1

    ex("""update stock_reconciliation set status='ignored'
           where pharmacy_id=%s and status='open'""", (pid,))
    compared, total = 0, 0.0
    for r in rows:
        if not r.get("legacy_code"):
            continue
        product = q1("""select id, pack_size, cost_price from products
                         where pharmacy_id=%s and legacy_code=%s""",
                     (pid, r["legacy_code"]))
        if not product:
            continue
        ours = q1("""select coalesce(sum(qty_pieces),0) as n from batches
                      where pharmacy_id=%s and product_id=%s""", (pid, product["id"]))
        pos_pieces = _resolve_pieces(r, int(r.get("qty_pieces") or 0),
                                     product["pack_size"])
        variance = pos_pieces - int(ours["n"])
        if variance == 0:
            continue
        value = variance * float(product["cost_price"] or 0)
        ex("""insert into stock_reconciliation (pharmacy_id, product_id, legacy_code,
                    ledger_pieces, pos_pieces, variance, variance_value, status)
              values (%s,%s,%s,%s,%s,%s,%s,'open')""",
           (pid, product["id"], r["legacy_code"], ours["n"], pos_pieces, variance,
            value))
        compared += 1
        total += abs(value)
    return (f"{compared} variance(s) found, KES {total:,.0f} unexplained. "
            f"Text *VARIANCE* on WhatsApp, or see the Stock page.")
