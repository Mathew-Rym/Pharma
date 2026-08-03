"""Distributor stock-sheet upload handler.

A distributor sends a CSV (or Excel) file to the pharmacy's WhatsApp number.
We parse it deterministically — no LLM — and create/update products and batches.

Expected CSV columns (case-insensitive, order-insensitive):
  product_code, product_name, batch_no, expiry_date, qty, unit_price

All columns except product_name and qty are optional but recommended.
Expiry formats accepted: MM/YYYY, YYYY-MM-DD, MM-YY, Jan-28, etc.
"""
import csv
import io
import logging
from datetime import date

from db import apply_movement, ex, ex1, q, q1, download, tx
from utils import parse_expiry, norm_phone
from wa import reply_text

log = logging.getLogger(__name__)

# Flexible column name aliases so distributors don't have to match our exact names
_COL_ALIASES = {
    "product_code": ["product_code", "code", "item_code", "sku", "part_no", "item no"],
    "product_name": ["product_name", "name", "description", "product", "item", "drug"],
    "batch_no":     ["batch_no", "batch", "batch_number", "lot", "lot_no"],
    "expiry_date":  ["expiry_date", "expiry", "exp_date", "exp", "expiry date", "best before"],
    "qty":          ["qty", "quantity", "units", "packs", "pieces", "stock"],
    "unit_price":   ["unit_price", "price", "cost", "cost_price", "buying_price"],
}


def _norm_header(h: str) -> str:
    return h.strip().lower().replace(" ", "_")


def _map_headers(headers: list[str]) -> dict[str, int]:
    """Return {canonical_name: column_index} for recognized columns."""
    normed = [_norm_header(h) for h in headers]
    result = {}
    for canon, aliases in _COL_ALIASES.items():
        for alias in aliases:
            alias_n = _norm_header(alias)
            if alias_n in normed:
                result[canon] = normed.index(alias_n)
                break
    return result


def _parse_qty(raw: str) -> int | None:
    """'5W' / '5' / '5.0' / '120P' -> integer pieces."""
    raw = (raw or "").strip().upper()
    # strip trailing unit letters
    digits = "".join(c for c in raw if c.isdigit() or c == ".")
    try:
        return int(float(digits)) if digits else None
    except ValueError:
        return None


def _parse_price(raw: str) -> float | None:
    raw = (raw or "").strip().replace(",", "").replace("KES", "").replace("Ksh", "")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def parse_csv_bytes(data: bytes) -> list[dict]:
    """Parse raw CSV/TSV bytes into a list of row dicts with canonical keys."""
    text = data.decode("utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(text[:2000], delimiters=",\t;|")
    reader = csv.reader(io.StringIO(text), dialect)
    rows_raw = list(reader)
    if not rows_raw:
        return []
    header = rows_raw[0]
    col_map = _map_headers(header)
    if "product_name" not in col_map and "product_code" not in col_map:
        raise ValueError("Could not find a product name or code column in the file.")

    result = []
    for i, row in enumerate(rows_raw[1:], start=2):
        if not any(c.strip() for c in row):
            continue  # skip blank lines
        def get(col: str) -> str:
            idx = col_map.get(col)
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        result.append({
            "line": i,
            "code": get("product_code") or None,
            "name": get("product_name") or get("product_code") or f"Row {i}",
            "batch_no": get("batch_no") or None,
            "expiry_raw": get("expiry_date") or None,
            "qty_raw": get("qty"),
            "price_raw": get("unit_price"),
        })
    return result


def process_stock_sheet(phone: str, pharmacy_id: str, media_path: str,
                        media_bucket: str, doc_ext: str = "csv") -> None:
    """Download the file, parse it, upsert products/batches, reply with a summary.

    This is called from router._handle_staff / _handle_customer when msg_type=='document'.
    For customers we treat the file as a prescription / inquiry and tell them to text instead.
    Only staff (or the supplier number in suppliers table) get the stock-sheet path.
    """
    try:
        data = download(media_bucket, media_path)
    except Exception as e:
        log.exception("could not download stock sheet %s", media_path)
        reply_text(phone, f"I could not retrieve the file ({type(e).__name__}). "
                          "Please send it again, or paste the data as text.")
        return

    if doc_ext in ("xls", "xlsx"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                reply_text(phone, "The spreadsheet appears to be empty.")
                return
            buf = io.StringIO()
            w = csv.writer(buf)
            for row in rows:
                w.writerow([str(c) if c is not None else "" for c in row])
            data = buf.getvalue().encode()
        except ImportError:
            reply_text(phone, "Excel files need the openpyxl library. "
                              "Please save the file as CSV and resend.")
            return
        except Exception as e:
            reply_text(phone, f"Could not read the Excel file: {e}. "
                              "Try saving as CSV.")
            return

    try:
        rows = parse_csv_bytes(data)
    except ValueError as e:
        reply_text(phone, f"Could not parse the file: {e}\n\n"
                          "Expected columns: product_name, batch_no, expiry_date, qty.\n"
                          "Send as a .csv file.")
        return
    except Exception as e:
        log.exception("CSV parse error for %s", media_path)
        reply_text(phone, f"File parse failed ({type(e).__name__}). "
                          "Try saving as a plain .csv and resend.")
        return

    if not rows:
        reply_text(phone, "The file had no data rows. "
                          "Check it has a header row and at least one product.")
        return

    ok, skipped, errors = 0, 0, []

    for row in rows:
        name = row["name"]
        qty = _parse_qty(row["qty_raw"])
        if qty is None or qty <= 0:
            errors.append(f"Row {row['line']}: {name[:30]} — quantity missing or zero")
            skipped += 1
            continue

        expiry = parse_expiry(row["expiry_raw"]) if row["expiry_raw"] else None
        price = _parse_price(row["price_raw"]) if row["price_raw"] else None

        try:
            # Upsert product: match by code first, then name similarity
            prod = None
            if row["code"]:
                prod = q1("select id, pack_size from products "
                          "where pharmacy_id=%s and legacy_code=%s",
                          (pharmacy_id, row["code"]))
            if not prod:
                prod = q1("select id, pack_size from products "
                          "where pharmacy_id=%s "
                          "and similarity(name, %s) > 0.55 "
                          "order by similarity(name, %s) desc limit 1",
                          (pharmacy_id, name, name))
            if not prod:
                prod = ex1(
                    "insert into products (pharmacy_id, legacy_code, name, pack_size, cost_price) "
                    "values (%s,%s,%s,1,%s) returning id, pack_size",
                    (pharmacy_id, row["code"] or f"DS-{name[:10].upper().replace(' ','')}",
                     name[:200], price),
                )
            elif price:
                ex("update products set cost_price=%s where id=%s", (price, prod["id"]))

            pack_size = prod.get("pack_size") or 1
            qty_pieces = qty * pack_size  # treat csv qty as packs

            with tx() as cur:
                cur.execute(
                    """insert into batches
                         (pharmacy_id, product_id, batch_no, expiry_date,
                          qty_pieces, cost_price)
                       values (%s,%s,%s,%s,0,%s)
                       on conflict (pharmacy_id, product_id, batch_no, expiry_date)
                         do update set cost_price = coalesce(excluded.cost_price, batches.cost_price)
                       returning id""",
                    (pharmacy_id, prod["id"],
                     row["batch_no"] or "DIST-UPLOAD",
                     expiry,
                     price),
                )
                batch_id = cur.fetchone()["id"]
                apply_movement(cur, batch_id, qty_pieces, "grn",
                               note=f"distributor upload {media_path}")
            ok += 1
        except Exception as e:
            log.exception("row %s failed: %s", row["line"], e)
            errors.append(f"Row {row['line']}: {name[:30]} — {type(e).__name__}")
            skipped += 1

    parts = [f"📦 Stock sheet processed: *{ok}* product(s) received into stock."]
    if skipped:
        parts.append(f"⚠️ {skipped} row(s) skipped:")
        parts.extend(f"  • {e}" for e in errors[:8])
        if len(errors) > 8:
            parts.append(f"  … and {len(errors)-8} more. Check the file and resend.")
    parts.append("\nSend *LOW* to see reorder suggestions, or *HELP* for all commands.")
    reply_text(phone, "\n".join(parts))
