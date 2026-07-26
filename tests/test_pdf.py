"""PDF tests.

The bug these exist to prevent already shipped once: fpdf2's core Helvetica is
latin-1 only, `build_report_pdf` had an en-dash in its title and em-dashes in two
headings, and so "report for july" raised FPDFUnicodeEncodingException instead of
returning a document. It failed at render time, in front of the owner, not at import.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


# ============================================================ the sanitiser
@pytest.mark.parametrize("raw,expected", [
    ("a — b", "a - b"),          # em dash: crashed build_report_pdf's headings
    ("a – b", "a - b"),          # en dash: crashed its title
    ("• item", "- item"),
    ("Jul · 2026", "Jul - 2026"),
    ("it’s", "it's"),
    ("“quoted”", '"quoted"'),
    ("wait…", "wait..."),
    ("a b", "a b"),         # non-breaking space, arrives from spreadsheets
])
def test_typographic_characters_are_folded_not_dropped(raw, expected):
    from pdfgen import latin1
    assert latin1(raw) == expected


def test_output_is_always_latin1_encodable():
    """The property that matters: whatever goes in, the PDF font can render it."""
    from pdfgen import latin1
    for s in ["emoji 💊 in a product name", "日本語", "Ćirilica", "plain ascii",
              "—–•·’“”…", "PRENOR 25/5MG"]:
        latin1(s).encode("latin-1")      # must not raise


def test_unrenderable_characters_are_dropped_rather_than_aborting():
    """An emoji in a supplier name must cost you the emoji, not the whole document."""
    from pdfgen import latin1
    out = latin1("Pharma 💊 Ltd")
    assert "Pharma" in out and "Ltd" in out


def test_doc_routes_every_string_through_the_sanitiser():
    """Overriding normalize_text is what makes cell/multi_cell/table all safe. If this
    regresses, each call site has to remember, and one will not."""
    from pdfgen import Doc
    doc = Doc("Pharma — Ltd", "Report – July")
    doc.add_page()
    doc.h2("Expiry watchlist — act on these")
    doc.cell(0, 5, "• bullet with ’smart quotes’ and an em—dash",
             new_x="LMARGIN", new_y="NEXT")
    doc.multi_cell(0, 5, "wrapped … text — with dashes",
                   new_x="LMARGIN", new_y="NEXT")
    doc.table(["A — col"], [["• row"]], [80])
    assert len(bytes(doc.output())) > 800


# ============================================================ report + PO documents
DB = bool(os.getenv("DATABASE_URL")) and bool(os.getenv("PHARMACY_ID"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL/PHARMACY_ID not set")


@db
def test_report_pdf_builds():
    """REGRESSION: this raised before the sanitiser existed."""
    from reports import build_report_pdf
    path, fname = build_report_pdf("month")
    assert fname.endswith(".pdf")
    assert path


@db
def test_po_pdf_carries_letterhead_and_withholds_internal_rationale():
    """A wholesaler needs a reference, a licence and a callback number. It must NOT
    receive po_lines.rationale -- that is the pharmacy's demand data and negotiating
    position ('20 pcs on hand, 7.6/day, 3d cover')."""
    import secrets

    from config import settings
    from db import ex, q1
    from reports import build_po_pdf

    pytest.importorskip("pypdf")
    from pypdf import PdfReader

    PID = settings.PHARMACY_ID
    mark = "PYTEST-" + secrets.token_hex(3).upper()
    ex("""update pharmacies set ppb_licence=coalesce(ppb_licence,'PPB/RPP/TEST')
           where id=%s""", (PID,))
    # Read back rather than assuming: coalesce keeps a licence the pharmacy already has.
    licence = q1("select ppb_licence from pharmacies where id=%s",
                 (PID,))["ppb_licence"]
    staff = q1("select id, name from staff where pharmacy_id=%s limit 1", (PID,))
    sup = q1("""insert into suppliers (pharmacy_id, name, phone, rep_name)
                values (%s,%s,'254711000111','Grace') returning id""",
             (PID, f"WHOLESALER {mark}"))
    prod = q1("""insert into products (pharmacy_id, name, legacy_code, pack_size,
                        cost_price, sell_price) values (%s,%s,%s,30,15,25) returning id""",
              (PID, f"PDF PROBE {mark}", mark))
    po = q1("""insert into purchase_orders (pharmacy_id, supplier_id, status, reason,
                    total_estimate, approved_by, approved_at)
               values (%s,%s,'sent','{}',0,%s,now()) returning id""",
            (PID, sup["id"], staff["id"]))
    ex("""insert into po_lines (po_id, product_id, qty_pieces, unit_cost, rationale)
          values (%s,%s,210,15.00,%s)""",
       (po["id"], prod["id"], "INTERNAL 20 pcs on hand, 7.6/day x1.00 seasonal"))
    try:
        path, fname = build_po_pdf(str(po["id"]))
        assert fname == f"PO-{str(po['id'])[:8].upper()}.pdf"

        from db import signed_url
        import httpx
        data = httpx.get(signed_url(settings.BUCKET_DOCS, path, 600), timeout=60).content
        out = f"/tmp/{mark}.pdf"
        open(out, "wb").write(data)
        txt = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        os.unlink(out)

        assert licence in txt                             # letterhead
        assert str(po["id"])[:8].upper() in txt           # pickable reference
        assert "Grace" in txt                             # who to address
        assert "BATCH" in txt.upper()                     # Loop A needs it on the invoice
        assert str(settings.MIN_SHELF_LIFE_DAYS) in txt   # shelf-life terms
        assert (staff["name"] or "") in txt               # attribution
        assert "DRAFT" not in txt.upper()
        assert "seasonal" not in txt.lower(), "internal rationale leaked to the supplier"
        assert "7.6/day" not in txt, "internal demand data leaked to the supplier"
    finally:
        ex("delete from po_lines where po_id=%s", (po["id"],))
        ex("delete from purchase_orders where id=%s", (po["id"],))
        ex("delete from products where id=%s", (prod["id"],))
        ex("delete from suppliers where id=%s", (sup["id"],))


@db
def test_unapproved_po_is_stamped_draft():
    """A PDF that looks authorised but is not would let an unapproved order be
    forwarded to a supplier."""
    import secrets

    from config import settings
    from db import ex, q1
    from reports import build_po_pdf

    pytest.importorskip("pypdf")
    from pypdf import PdfReader

    PID = settings.PHARMACY_ID
    mark = "PYTEST-" + secrets.token_hex(3).upper()
    sup = q1("insert into suppliers (pharmacy_id, name) values (%s,%s) returning id",
             (PID, f"WHOLESALER {mark}"))
    prod = q1("""insert into products (pharmacy_id, name, legacy_code, pack_size,
                        cost_price, sell_price) values (%s,%s,%s,30,15,25) returning id""",
              (PID, f"PDF PROBE {mark}", mark))
    po = q1("""insert into purchase_orders (pharmacy_id, supplier_id, status, reason,
                    total_estimate) values (%s,%s,'awaiting_approval','{}',0)
               returning id""", (PID, sup["id"]))
    ex("""insert into po_lines (po_id, product_id, qty_pieces, unit_cost)
          values (%s,%s,60,15.00)""", (po["id"], prod["id"]))
    try:
        path, _ = build_po_pdf(str(po["id"]))
        from db import signed_url
        import httpx
        data = httpx.get(signed_url(settings.BUCKET_DOCS, path, 600), timeout=60).content
        out = f"/tmp/{mark}.pdf"
        open(out, "wb").write(data)
        txt = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        os.unlink(out)
        assert "DRAFT" in txt.upper()
    finally:
        ex("delete from po_lines where po_id=%s", (po["id"],))
        ex("delete from purchase_orders where id=%s", (po["id"],))
        ex("delete from products where id=%s", (prod["id"],))
        ex("delete from suppliers where id=%s", (sup["id"],))
