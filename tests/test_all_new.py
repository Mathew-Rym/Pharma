"""`ALL NEW` — the step between "the system reads invoices" and "the pharmacy has stock".

grn.approve() refuses the whole GRN while any line is unmatched, and tells the user to reply
`<n> NEW` for each. On an established pharmacy that is right: a handful of new items among
fifty known ones deserves a decision each.

On a NEW pharmacy it is a dead end. There are no products, so every line is unmatched. A
real pharmacy hit this: 232 extracted invoice lines, and products / batches /
stock_movements all zero, because finishing would have meant ~58 separate WhatsApp
messages. Gemini had read the invoices correctly; the pipeline simply stopped one step
before stock, and a customer asking for a medicine got "No product matching".

So `ALL NEW` creates a product for every unmatched line at once. It is not a shortcut around
a safety gate -- nothing about approval, counting or the POM gate changes -- it is the bulk
form of a decision the user was already being asked to make line by line.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")

SRC = os.path.join(os.path.dirname(__file__), "..", "api", "grn.py")


# ============================================================ recognised, and offered
@pytest.mark.parametrize("text", ["ALL NEW", "all new", "  All New  ", "ALLNEW"])
def test_all_new_is_recognised_however_it_is_typed(text):
    import grn
    assert grn._is_all_new(text) is True


@pytest.mark.parametrize("text", ["7 NEW", "NEW", "ALL", "OK", "", "ALL NEWS"])
def test_it_does_not_swallow_neighbouring_commands(text):
    """`7 NEW` must keep working -- it is the right tool on an established pharmacy, and a
    greedy match here would take the per-line decision away."""
    import grn
    assert grn._is_all_new(text) is False


def test_the_refusal_offers_all_new_when_several_lines_are_unmatched():
    """The message a new pharmacy actually sees. Naming only `<n> NEW` is what produced 232
    stranded lines: correct advice that nobody could act on fifty-eight times."""
    src = open(SRC).read()
    block = src[src.index("def approve("):src.index("def approve(") + 1800]
    assert "ALL NEW" in block, "approve() must offer ALL NEW when lines are unmatched"


def test_help_lists_it():
    src = open(SRC).read()
    assert "*ALL NEW*" in src, "the correction menu must list ALL NEW"


# ============================================================ it must not weaken anything
def test_it_does_not_touch_approval_or_the_pom_gate():
    """ALL NEW links products. It must not approve, receive, or bypass the physical count.
    Creating a product is a catalogue action; moving stock is not."""
    import ast
    import inspect

    import grn

    # Strip the docstring and comments first. This function's own docstring explains what
    # approve() does, and a naive substring search matches the explanation that exists to
    # stop the boundary being crossed -- the same trap that made an earlier structural test
    # fail forever and invited whoever inherited it to delete the comment.
    tree = ast.parse(inspect.getsource(grn._link_all_unmatched).lstrip())
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]                      # drop the docstring node
    code = ast.unparse(fn)

    for forbidden in ("insert into batches", "insert into stock_movements",
                      "status='approved'", "approve("):
        assert forbidden not in code, f"_link_all_unmatched must not do: {forbidden}"


def test_approve_still_refuses_while_anything_is_unmatched():
    """The gate itself is unchanged: ALL NEW is a way to satisfy it, not to skip it."""
    src = open(SRC).read()
    block = src[src.index("def approve("):]
    assert "unmatched = [l for l in lines if not l[\"product_id\"]]" in block
    assert "Cannot receive yet" in block


# ============================================================ against the database
@pytest.fixture
def grn_with_unmatched():
    """A GRN whose lines match nothing -- i.e. a newly registered pharmacy's first invoice."""
    import json

    from db import ex, q1
    from tenancy import pid
    mark = os.urandom(3).hex().upper()
    g = q1("""insert into grns (pharmacy_id, invoice_no, status, images, raw_extract)
              values (%s,%s,'needs_review','[]','{}') returning id""",
           (pid(), f"ALLNEW-{mark}"))
    gid = str(g["id"])
    for i, (desc, code, qty, price) in enumerate([
            ("AMOXIL 500MG CAPS 21S", f"AMX-{mark}", 21, 340),
            ("PANADOL 500MG TABS 24S", f"PAR-{mark}", 24, 90),
            ("OMEZOL 20MG CAPS 30S", None, 30, 260)], start=1):
        ex("""insert into grn_lines (grn_id, line_no, raw_code, raw_description,
                                    qty_invoiced_pieces, unit_price, confidence, flags)
              values (%s,%s,%s,%s,%s,%s,0.9,'{unmatched_product}')""",
           (gid, i, code, desc, qty, price))
    yield gid
    ex("delete from stock_movements where batch_id in (select id from batches where product_id in "
       "(select id from products where legacy_code like %s))", (f"%{mark}%",))
    ex("delete from batches where product_id in (select id from products where legacy_code like %s)",
       (f"%{mark}%",))
    ex("delete from grn_lines where grn_id=%s", (gid,))
    ex("delete from grns where id=%s", (gid,))
    ex("delete from products where legacy_code like %s or name like %s", (f"%{mark}%", f"%{mark}%"))


@db
def test_all_new_links_every_unmatched_line_in_one_go(grn_with_unmatched, monkeypatch):
    import grn
    from db import q
    monkeypatch.setattr(grn, "reply_text", lambda *a, **k: None)

    before = q("""select line_no, product_id from grn_lines where grn_id=%s
                   order by line_no""", (grn_with_unmatched,))
    assert all(r["product_id"] is None for r in before), "fixture should start unmatched"

    created = grn._link_all_unmatched(grn_with_unmatched)
    assert len(created) == 3, f"expected 3 products, got {created}"

    after = q("""select line_no, product_id, flags from grn_lines where grn_id=%s
                  order by line_no""", (grn_with_unmatched,))
    assert all(r["product_id"] is not None for r in after), "a line was left unmatched"
    assert all("unmatched_product" not in (r["flags"] or []) for r in after)


@db
def test_the_products_carry_the_name_and_price_from_the_invoice(grn_with_unmatched, monkeypatch):
    """A product called "Unknown item" priced at nothing is not usable stock -- a customer
    asking for amoxil has to find it by name."""
    import grn
    from db import q1
    monkeypatch.setattr(grn, "reply_text", lambda *a, **k: None)
    grn._link_all_unmatched(grn_with_unmatched)
    p = q1("""select p.name, p.cost_price, p.pack_size from grn_lines l
                join products p on p.id = l.product_id
               where l.grn_id=%s and l.line_no=1""", (grn_with_unmatched,))
    assert "AMOXIL" in p["name"].upper()
    assert float(p["cost_price"]) == 340.0
    assert p["pack_size"] >= 1


@db
def test_it_is_idempotent(grn_with_unmatched, monkeypatch):
    """Someone will send it twice. The second must create nothing and must not duplicate
    the catalogue."""
    import grn
    from db import q1
    monkeypatch.setattr(grn, "reply_text", lambda *a, **k: None)
    first = grn._link_all_unmatched(grn_with_unmatched)
    n1 = q1("select count(*) n from products where pharmacy_id=(select pharmacy_id from grns where id=%s)",
            (grn_with_unmatched,))["n"]
    second = grn._link_all_unmatched(grn_with_unmatched)
    n2 = q1("select count(*) n from products where pharmacy_id=(select pharmacy_id from grns where id=%s)",
            (grn_with_unmatched,))["n"]
    assert len(first) == 3 and second == []
    assert n1 == n2, "a second ALL NEW duplicated products"


@db
def test_a_line_with_no_description_is_reported_not_invented(grn_with_unmatched, monkeypatch):
    """An unreadable line must not become a product called "Unknown item" that a pharmacist
    later dispenses from. Skipped and named, so a human decides."""
    import grn
    from db import ex, q1
    monkeypatch.setattr(grn, "reply_text", lambda *a, **k: None)
    ex("update grn_lines set raw_description=null where grn_id=%s and line_no=2",
       (grn_with_unmatched,))
    created = grn._link_all_unmatched(grn_with_unmatched)
    assert len(created) == 2, f"a nameless line was turned into a product: {created}"
    left = q1("""select line_no from grn_lines where grn_id=%s and product_id is null""",
              (grn_with_unmatched,))
    assert left["line_no"] == 2
