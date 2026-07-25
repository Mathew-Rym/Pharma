"""Seed the database from a phAMACore export.

phAMACore's Purchase Order Wizard has an `export` button (visible in the screenshots
from the discovery session). Get any product/stock export as CSV or XLSX and point this
at it. Column names vary between phAMACore builds, so we fuzzy-map headers rather than
demanding an exact schema.

Usage:
    python seed/import_phamacore.py data/stock_export.csv
    python seed/import_phamacore.py data/stock_export.csv --dry-run
"""
import argparse
import csv
import os
import re
import sys
import uuid
from datetime import date

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ["DATABASE_URL"]
PID = os.environ["PHARMACY_ID"]

# candidate header names -> our field
HEADER_MAP = {
    "legacy_code": ["itemcode", "item code", "code", "item_code", "productcode"],
    "name": ["itemname", "item name", "description", "name", "product", "productname"],
    "qty": ["instore", "qtyonhand", "qty on hand", "quantity", "stock", "onhand",
            "closingqty", "balance"],
    "cost_price": ["avgcost", "cost", "costprice", "cost price", "lastcost", "buyingprice"],
    "sell_price": ["sellprice", "sell price", "price", "retail", "sellingprice",
                   "avg. sell price", "avgsellprice"],
    "reorder": ["rorderlvl", "reorder", "reorderlevel", "reorder level", "minqty",
                "minimum"],
    "expiry": ["expiry", "expirydate", "expiry date", "exp"],
    "batch": ["batch", "batchno", "batch no", "lot"],
}


def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (h or "").strip().lower())


def build_index(headers: list[str]) -> dict:
    idx = {}
    normed = [norm_header(h) for h in headers]
    for field, candidates in HEADER_MAP.items():
        for c in candidates:
            if c in normed:
                idx[field] = normed.index(c)
                break
    return idx


def parse_qty(raw: str) -> int:
    """phAMACore prints quantities as '5W0P' (5 whole packs, 0 pieces) or plain numbers.

    Without knowing pack_size at import time we take whole packs at face value and
    treat pack_size as 1 unless the product name reveals it (e.g. 'TABS 30S' -> 30).
    """
    if raw is None:
        return 0
    s = str(raw).strip().upper().replace(",", "")
    m = re.match(r"^(-?\d+)\s*W\s*(\d+)?\s*P?$", s)
    if m:
        return int(m.group(1))
    m = re.match(r"^-?\d+(\.\d+)?$", s)
    return int(float(s)) if m else 0


def guess_pack_size(name: str) -> int:
    m = re.search(r"(\d{1,4})\s*S\b", (name or "").upper())
    if m:
        n = int(m.group(1))
        if 1 <= n <= 1000:
            return n
    return 1


def parse_money(raw) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(str(raw).replace(",", "").replace("KES", "").strip())
    except ValueError:
        return None


def read_rows(path: str) -> tuple[list[str], list[list[str]]]:
    if path.lower().endswith((".xlsx", ".xls")):
        try:
            import openpyxl
        except ImportError:
            sys.exit("pip install openpyxl to read Excel files, or export as CSV")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = [[("" if c is None else str(c)) for c in r] for r in ws.iter_rows(values_only=True)]
        return rows[0], rows[1:]
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = list(csv.reader(f, dialect))
    return reader[0], reader[1:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-opening-stock", action="store_true",
                    help="import the catalogue only, no opening batches")
    args = ap.parse_args()

    headers, rows = read_rows(args.path)
    idx = build_index(headers)
    print(f"Headers detected: {headers}")
    print(f"Mapped fields: {sorted(idx)}")
    if "name" not in idx:
        sys.exit("Could not find a product-name column. Rename it to 'ItemName' and retry.")

    def cell(row, field, default=""):
        i = idx.get(field)
        if i is None or i >= len(row):
            return default
        return row[i]

    parsed, skipped = [], 0
    for row in rows:
        name = str(cell(row, "name")).strip()
        if not name or name.lower() in ("itemname", "description", "total", "grand total"):
            skipped += 1
            continue
        pack = guess_pack_size(name)
        whole = parse_qty(cell(row, "qty"))
        parsed.append({
            "legacy_code": str(cell(row, "legacy_code")).strip()
                           or f"IMP-{uuid.uuid4().hex[:8]}",
            "name": name[:200],
            "pack_size": pack,
            "qty_pieces": whole * pack,
            "cost_price": parse_money(cell(row, "cost_price")),
            "sell_price": parse_money(cell(row, "sell_price")),
            "reorder": parse_qty(cell(row, "reorder")) * pack,
            "batch": str(cell(row, "batch")).strip() or None,
        })

    print(f"\nParsed {len(parsed)} products, skipped {skipped} rows.")
    print("Sample:")
    for p in parsed[:5]:
        print(f"  {p['legacy_code']:<14} {p['name'][:44]:<46} "
              f"pack={p['pack_size']:<4} pcs={p['qty_pieces']}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    inserted = batched = 0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for p in parsed:
                cur.execute(
                    """insert into products (pharmacy_id, legacy_code, name, pack_size,
                              cost_price, sell_price, reorder_level_pieces)
                       values (%s,%s,%s,%s,%s,%s,%s)
                       on conflict (pharmacy_id, legacy_code) do update
                         set name = excluded.name,
                             pack_size = excluded.pack_size,
                             cost_price = coalesce(excluded.cost_price, products.cost_price),
                             sell_price = coalesce(excluded.sell_price, products.sell_price),
                             reorder_level_pieces = excluded.reorder_level_pieces
                       returning id""",
                    (PID, p["legacy_code"], p["name"], p["pack_size"], p["cost_price"],
                     p["sell_price"], p["reorder"]),
                )
                product_id = cur.fetchone()["id"]
                inserted += 1

                if args.no_opening_stock or p["qty_pieces"] <= 0:
                    continue

                # Opening balance batch. expiry_date NULL on purpose — the legacy export
                # does not carry reliable expiry data, and inventing dates would poison
                # the expiry engine. Real expiries arrive with the first GRN.
                cur.execute(
                    """insert into batches (pharmacy_id, product_id, batch_no, expiry_date,
                              qty_pieces, cost_price, confidence)
                       values (%s,%s,%s,null,0,%s,1.0)
                       on conflict (pharmacy_id, product_id, batch_no, expiry_date)
                         do nothing
                       returning id""",
                    (PID, product_id, p["batch"] or "OPENING", p["cost_price"]),
                )
                got = cur.fetchone()
                if not got:
                    cur.execute(
                        """select id from batches where pharmacy_id=%s and product_id=%s
                             and batch_no=%s and expiry_date is null""",
                        (PID, product_id, p["batch"] or "OPENING"),
                    )
                    got = cur.fetchone()
                batch_id = got["id"]

                cur.execute(
                    """select coalesce(sum(delta_pieces),0) as n from stock_movements
                        where batch_id=%s and reason='opening'""",
                    (batch_id,),
                )
                if cur.fetchone()["n"] == 0:
                    cur.execute(
                        """insert into stock_movements (pharmacy_id, batch_id, delta_pieces,
                                  reason, note)
                           values (%s,%s,%s,'opening',%s)""",
                        (PID, batch_id, p["qty_pieces"],
                         f"phAMACore import {date.today()}"),
                    )
                    cur.execute("update batches set qty_pieces = qty_pieces + %s where id=%s",
                                (p["qty_pieces"], batch_id))
                    batched += 1

    print(f"\n✅ {inserted} products upserted, {batched} opening batches created.")
    print("Opening batches have NO expiry date on purpose — real expiries come from the "
          "first supplier invoice you photograph.")


if __name__ == "__main__":
    main()
