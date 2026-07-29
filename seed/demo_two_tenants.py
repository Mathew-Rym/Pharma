"""Seed two visibly distinct pharmacies for the isolation demo.

Run:  set -a && . ./.env && set +a && .venv/bin/python seed/demo_two_tenants.py

Idempotent: re-running resets both tenants to this exact state, so you can rehearse twice
and get identical numbers both times.

The point of the data is one moment on stage: ask both pharmacies for the same molecule
and get different prices, then ask B for something only A stocks and watch it come back
not found. Everything else is padding.

Column names here are the ones the database actually has, which are NOT the obvious
guesses: batches use `batch_no` and `qty_pieces` (not batch_number/quantity), reorder
level lives on `products.reorder_level_pieces` (not on the batch), price is `sell_price`,
and `batches.pharmacy_id` is NOT NULL and must be written explicitly even though it is
derivable from the product.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from db import ex, q, q1  # noqa: E402

A_NAME = "New Lemuma Pharmacy"
B_NAME = "Greenline Pharmacy"

# (legacy_code, name, generic_name, form, strength, pack_size, rx_only, cost, sell,
#  reorder_level_pieces, qty_pieces, expiry_days)
#
# Deliberate overlaps and gaps -- these ARE the demo:
#   NEBIVOLOL   both stock it, different price          -> isolation, visible in one reply
#   ATORVA      A only                                  -> ask B, not found
#   CETIRIZINE  B only                                   -> so B is not merely emptier
#   AMOXIL      A below reorder level, B healthy         -> alert fires for one tenant
#   OMEPRAZOLE  A expiring inside 90 days                -> expiry report has content
A_STOCK = [
    ("NLP-NEB5",  "Nebilong 5mg",      "nebivolol",   "tab", "5mg",   30, False,  520,  780, 60, 240, 400),
    ("NLP-ATO20", "Atorvachol 20mg",   "atorvastatin", "tab", "20mg", 30, True,   610,  950, 30, 180, 500),
    ("NLP-AMX5",  "Amoxil 500mg",      "amoxicillin", "cap", "500mg", 21, True,   340,  540, 90,  40, 300),
    ("NLP-PAR5",  "Panadol 500mg",     "paracetamol", "tab", "500mg", 24, False,   90,  160, 48, 480, 600),
    ("NLP-OME20", "Omezol 20mg",       "omeprazole",  "cap", "20mg",  30, False,  260,  420, 60, 120,  70),
    ("NLP-MET5",  "Glucophage 500mg",  "metformin",   "tab", "500mg", 30, True,   300,  480, 60, 300, 450),
]
B_STOCK = [
    ("GLP-NEB5",  "Nebilet 5mg",       "nebivolol",   "tab", "5mg",   28, False,  560,  890, 56, 168, 420),
    ("GLP-CET10", "Zyrtec 10mg",       "cetirizine",  "tab", "10mg",  10, False,   80,  150, 30, 200, 520),
    ("GLP-AMX5",  "Amoxycare 500mg",   "amoxicillin", "cap", "500mg", 21, True,   330,  520, 63, 315, 380),
    ("GLP-PAR5",  "Hedex 500mg",       "paracetamol", "tab", "500mg", 24, False,   95,  170, 48, 360, 640),
    ("GLP-IBU4",  "Brufen 400mg",      "ibuprofen",   "tab", "400mg", 20, False,  150,  260, 40, 140, 480),
]

# Staff are seeded WITHOUT inbound history on purpose: the anti-ban gates require that a
# person messages the bot before it can message them. Anyone who needs to RECEIVE an alert
# during the demo must text the number first -- see WHATSAPP.md.
A_STAFF = [("Owner A", "254700000101", "owner", None),
           ("Pharmacist A", "254700000102", "pharmacist", "PPB-11111"),
           ("Manager A", "254700000103", "manager", None)]
B_STAFF = [("Owner B", "254700000201", "owner", None),
           ("Pharmacist B", "254700000202", "pharmacist", "PPB-22222"),
           ("Manager B", "254700000203", "manager", None)]


def _wipe(pid: str) -> None:
    """Children first. batches -> products, and everything that points at either."""
    for t in ("stock_movements", "batches", "order_lines", "orders", "grn_lines", "grns",
              "po_lines", "purchase_orders", "prescriptions", "products",
              "inbound_history", "customers", "staff"):
        try:
            ex(f"delete from {t} where pharmacy_id=%s", (pid,))
        except Exception as e:                       # table may not carry pharmacy_id
            print(f"    (skipped {t}: {str(e).splitlines()[0][:60]})")


def _tenant(name: str) -> str:
    row = q1("select id from pharmacies where name=%s", (name,))
    if row:
        pid = str(row["id"])
        print(f"  reusing {name} ({pid})")
        _wipe(pid)
        return pid
    row = q1("""insert into pharmacies (name, mpesa_paybill, timezone, kind)
                values (%s,'4166919','Africa/Nairobi','tenant') returning id""", (name,))
    print(f"  created {name} ({row['id']})")
    return str(row["id"])


def _stock(pid: str, rows: list) -> None:
    for (code, nm, gen, form, strength, pack, rx, cost, sell, reorder,
         qty, exp_days) in rows:
        prod = q1("""insert into products
                       (pharmacy_id, legacy_code, name, generic_name, form, strength,
                        pack_size, is_prescription_only, cost_price, sell_price,
                        reorder_level_pieces)
                     values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                  (pid, code, nm, gen, form, strength, pack, rx, cost, sell, reorder))
        ex("""insert into batches
                (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces, cost_price)
              values (%s,%s,%s,%s,%s,%s)""",
           (pid, prod["id"], f"{code}-B1",
            date.today() + timedelta(days=exp_days), qty, cost))


def _staff(pid: str, rows: list) -> None:
    for nm, phone, role, ppb in rows:
        # ppb_reg_no, not ppb_number. And role is constrained to
        # owner|manager|pharmacist|attendant -- no cashier or storekeeper, despite the
        # build spec listing six roles.
        ex("""insert into staff (pharmacy_id, name, phone, role, ppb_reg_no, is_active)
              values (%s,%s,%s,%s,%s,true)""", (pid, nm, phone, role, ppb))


def main() -> None:
    print("Seeding two tenants\n")
    a, b = _tenant(A_NAME), _tenant(B_NAME)
    _stock(a, A_STOCK); _staff(a, A_STAFF)
    _stock(b, B_STOCK); _staff(b, B_STAFF)

    print("\nThe isolation moment\n")
    for gen in ("nebivolol", "atorvastatin", "cetirizine"):
        line = f"  {gen:14}"
        for pid, label in ((a, "A"), (b, "B")):
            r = q1("""select p.name, p.sell_price, coalesce(sum(b.qty_pieces),0) qty
                        from products p left join batches b on b.product_id = p.id
                       where p.pharmacy_id = %s and p.generic_name = %s
                       group by p.name, p.sell_price""", (pid, gen))
            line += (f"  {label}: {r['name']} KES {r['sell_price']:.0f} ({r['qty']} pc)"
                     if r else f"  {label}: not stocked")
        print(line)

    print("\nBelow reorder level (the alert fires for one tenant only)\n")
    for pid, label in ((a, "A"), (b, "B")):
        rows = q("""select p.name, coalesce(sum(b.qty_pieces),0) qty,
                           p.reorder_level_pieces lvl
                      from products p left join batches b on b.product_id = p.id
                     where p.pharmacy_id = %s
                     group by p.name, p.reorder_level_pieces
                    having coalesce(sum(b.qty_pieces),0) <= p.reorder_level_pieces""",
                 (pid,))
        print(f"  {label}: " + (", ".join(f"{r['name']} {r['qty']}/{r['lvl']}"
                                          for r in rows) or "none"))

    print("\nExpiring within 90 days\n")
    for pid, label in ((a, "A"), (b, "B")):
        rows = q("""select p.name, b.expiry_date from products p
                      join batches b on b.product_id = p.id
                     where p.pharmacy_id = %s
                       and b.expiry_date <= current_date + 90""", (pid,))
        print(f"  {label}: " + (", ".join(f"{r['name']} {r['expiry_date']}"
                                          for r in rows) or "none"))

    print(f"\nA = {a}\nB = {b}\n")
    print("Staff cannot RECEIVE messages until they text the bot first (anti-ban Gate 3).")
    print("Check with: ./run.sh safety\n")


if __name__ == "__main__":
    main()
