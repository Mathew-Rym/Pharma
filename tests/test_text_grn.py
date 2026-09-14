"""Typed-in receiving (RECEIVE -> lines -> DONE), the no-vision fallback.

The photo path is the product's wedge, but it needs a vision model and quota. When
neither is available, staff must still be able to receive stock: type the invoice
lines as text, get the same review/approve gate, same ledger, same flags.

These tests cover the pure parser: every classification decision it makes (qty vs
price vs expiry vs batch) is one a mistyped character could flip, and a wrong guess
moves real stock.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from grn import parse_invoice_text, _parse_invoice_text_traced


def test_pipe_format_full_line():
    (l,) = parse_invoice_text(
        "Amoxil 500mg | B123 | 08/2027 | 5W0P | 540")
    assert l["description"] == "Amoxil 500mg"
    assert l["batch_no"] == "B123"
    assert l["expiry_raw"] == "08/2027"
    assert (l["qty_whole"], l["qty_pieces"]) == (5, 0)
    assert l["unit_price"] == 540.0
    assert l["line_total"] == 2700.0


def test_comma_format_works_too():
    (l,) = parse_invoice_text("Panadol 500mg, 01/2028, 10W, 160")
    assert l["description"] == "Panadol 500mg"
    assert l["expiry_raw"] == "01/2028"
    assert (l["qty_whole"], l["qty_pieces"]) == (10, 0)
    assert l["unit_price"] == 160.0
    assert l["batch_no"] is None


def test_semicolon_and_lowercase_wp():
    (l,) = parse_invoice_text("Omezol 20mg ; OMZ9 ; 2027-08 ; 3w10p ; 420.50")
    assert (l["qty_whole"], l["qty_pieces"]) == (3, 10)
    assert l["expiry_raw"] == "2027-08"
    assert l["batch_no"] == "OMZ9"
    assert l["unit_price"] == 420.50


def test_bare_integer_qty_is_packs_not_pieces():
    # parse_wp's convention: a bare number is WHOLE packs (the phAMACore reading).
    # A piece count must be spelled out as 0W<n>P.
    (l,) = parse_invoice_text("Hedex 500mg | HX1 | 06/2028 | 2 | 95")
    assert (l["qty_whole"], l["qty_pieces"]) == (2, 0)


def test_multiple_lines_in_one_message():
    ls = parse_invoice_text(
        "Amoxil 500mg | B123 | 08/2027 | 5W0P | 540\n"
        "\n"
        "Panadol 500mg | P456 | 01/2028 | 10W | 160\n"
    )
    assert [l["description"] for l in ls] == ["Amoxil 500mg", "Panadol 500mg"]
    assert [l["line_no"] for l in ls] == [1, 2]


def test_name_alone_is_rejected_not_guessed():
    # A single field carries no quantity. Guessing one moves phantom stock.
    assert parse_invoice_text("Amoxil 500mg") == []


def test_garbage_text_is_rejected():
    assert parse_invoice_text("habari yako") == []
    assert parse_invoice_text("") == []


def test_qty_without_expiry_still_parses():
    # Expiry flags as missing downstream; the line must not be dropped for it.
    (l,) = parse_invoice_text("Brufen 400mg | BF7 | 20W | 260")
    assert l["expiry_raw"] is None
    assert (l["qty_whole"], l["qty_pieces"]) == (20, 0)


def test_price_with_comma_decimal():
    (l,) = parse_invoice_text("Zyrtec 10mg | ZY2 | 12/2027 | 4W | 80,50")
    assert l["unit_price"] == 80.5


def test_batch_that_looks_like_nothing_else_lands_in_batch():
    (l,) = parse_invoice_text("Glucophage 500mg | MET/GLP-22 | 08/2027 | 6W | 480")
    assert l["batch_no"] == "MET/GLP-22"


# ------------------------------------------------------------ bulk paste
def test_bulk_paste_of_many_lines_in_one_message():
    text = "\n".join(f"Med {i} 50mg | B{i} | 08/2027 | {i}W | 100" for i in range(1, 41))
    ls = parse_invoice_text(text)
    assert len(ls) == 40
    assert [l["line_no"] for l in ls] == list(range(1, 41))
    assert ls[39]["description"] == "Med 40 50mg"


def test_tab_separated_excel_paste():
    # Copy-paste out of a spreadsheet: tab-separated columns, one row per line.
    (l,) = parse_invoice_text("Amoxil 500mg\tB123\t08/2027\t2W0P\t540")
    assert l["description"] == "Amoxil 500mg"
    assert (l["qty_whole"], l["qty_pieces"]) == (2, 0)
    assert l["unit_price"] == 540.0


def test_rejected_lines_are_reported_not_swallowed():
    # The point of a whole invoice pasted at once: if 2 of 30 lines fail to parse,
    # the typist must be TOLD which, or that stock quietly never enters the system.
    text = ("Amoxil 500mg | B123 | 08/2027 | 5W0P | 540\n"
            "just a product name with no data\n"
            "Panadol 500mg | P456 | 01/2028 | 10W | 160")
    lines, rejected = _parse_invoice_text_traced(text)
    assert len(lines) == 2
    assert rejected == ["just a product name with no data"]


def test_mixed_good_and_garbage_keeps_line_numbers_continuous():
    text = ("Amoxil 500mg | B123 | 08/2027 | 5W0P | 540\n"
            "habari??\n"
            "Panadol 500mg | P456 | 01/2028 | 10W | 160")
    lines, rejected = _parse_invoice_text_traced(text)
    assert [l["line_no"] for l in lines] == [1, 2]   # numbered by PARSED order
    assert len(rejected) == 1
