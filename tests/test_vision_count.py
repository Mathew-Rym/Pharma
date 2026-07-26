"""Physical delivery verification (Loop A).

The model is stubbed here on purpose — these test the LOGIC around it, which is where
the dangerous failures live. A vision model that miscounts costs a re-photograph. Logic
that lets a machine guess become ledger truth, or that cries shortage every time a box
is out of frame, costs the pilot.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL")) and bool(os.getenv("PHARMACY_ID"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL/PHARMACY_ID not set")


@pytest.fixture
def grn(monkeypatch):
    """A 2-line GRN: 5 packs of a 21s invoiced, 6 packs of a 24s invoiced."""
    import secrets

    from config import settings
    from db import ex, q1

    PID = settings.PHARMACY_ID
    mark = "PYTEST-" + secrets.token_hex(3).upper()
    sup = q1("insert into suppliers (pharmacy_id, name) values (%s,%s) returning id",
             (PID, f"WHOLESALER {mark}"))
    made = []
    for nm, ps, inv_packs in [("AMOXIL 500MG CAPS 21S", 21, 5),
                              ("PANADOL 500MG TABS 24S", 24, 6)]:
        p = q1("""insert into products (pharmacy_id, name, legacy_code, pack_size,
                        cost_price, sell_price) values (%s,%s,%s,%s,10,18) returning id""",
               (PID, f"{nm} {mark}", f"{nm[:3]}{mark}", ps))
        made.append((p["id"], ps, inv_packs))
    g = q1("""insert into grns (pharmacy_id, supplier_id, invoice_no, status, images)
              values (%s,%s,%s,'awaiting_count','[]') returning id""",
           (PID, sup["id"], mark))
    for i, (pid_, ps, inv_packs) in enumerate(made, 1):
        ex("""insert into grn_lines (grn_id, line_no, raw_description, product_id,
                    batch_no, expiry_date, qty_invoiced_pieces, unit_price, confidence)
              values (%s,%s,%s,%s,%s,current_date + 400,%s,10.00,0.95)""",
           (g["id"], i, f"LINE {i} {mark}", pid_, f"B{i}{mark}", inv_packs * ps))

    # never call a real vision model from a test
    import grn as grnmod
    monkeypatch.setattr(grnmod, "download", lambda *a, **k: b"fake-jpeg-bytes")
    yield str(g["id"]), made, mark

    ex("delete from grn_lines where grn_id=%s", (g["id"],))
    ex("delete from grns where id=%s", (g["id"],))
    for pid_, _, _ in made:
        ex("delete from products where id=%s", (pid_,))
    ex("delete from suppliers where id=%s", (sup["id"],))


def _stub(monkeypatch, items, **extra):
    import grn as grnmod
    monkeypatch.setattr(grnmod, "count_delivery",
                        lambda *a, **k: {"items": items, **extra})


# ============================================================ the ledger boundary
@db
def test_machine_count_never_becomes_ledger_truth(grn, monkeypatch):
    """THE test in this file.

    qty_counted_pieces means "a human stands behind this". apply_vision_count must
    write only vision_* columns, so approve() still receives the INVOICE quantity
    until a person confirms otherwise. If a model guess could silently change what
    enters stock, the ledger stops being something a pharmacist can defend.
    """
    from db import q
    grn_id, _, _ = grn
    _stub(monkeypatch, [
        {"line_no": 1, "packs": 5, "loose": 0, "confidence": 1.0, "fully_visible": True},
        {"line_no": 2, "packs": 3, "loose": 0, "confidence": 1.0, "fully_visible": True},
    ])
    import grn as grnmod
    grnmod.apply_vision_count(grn_id, ["some/path.jpg"])

    rows = q("""select qty_invoiced_pieces, qty_counted_pieces, vision_packs,
                       pieces_to_receive
                  from v_grn_verification where grn_id=%s order by line_no""", (grn_id,))
    assert [r["vision_packs"] for r in rows] == [5, 3]
    for r in rows:
        assert r["qty_counted_pieces"] is None, "machine wrote the human's column"
        assert r["pieces_to_receive"] == r["qty_invoiced_pieces"]


@db
def test_confirmed_count_then_drives_the_ledger(grn, monkeypatch):
    """The existing '5:2W' reply is the confirmation step, and it must win."""
    from db import q1
    grn_id, made, _ = grn
    _stub(monkeypatch, [{"line_no": 2, "packs": 3, "loose": 0, "confidence": 1.0,
                         "fully_visible": True}])
    import grn as grnmod
    grnmod.apply_vision_count(grn_id, ["p.jpg"])
    grnmod._set_counted(grn_id, 2, (3, 0))       # pharmacist confirms 3 packs of 24
    r = q1("""select qty_counted_pieces, pieces_to_receive from v_grn_verification
               where grn_id=%s and line_no=2""", (grn_id,))
    assert r["qty_counted_pieces"] == 72
    assert r["pieces_to_receive"] == 72


# ============================================================ packs, not pieces
@db
def test_packs_are_multiplied_by_pack_size(grn, monkeypatch):
    """Vision counts CARTONS -- it cannot see tablets inside a sealed box. Storing a
    piece count here would repeat the 30x understatement that '2W0P' read as 2 pieces
    already caused once."""
    from db import q1
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 1, "packs": 5, "loose": 0, "confidence": 1.0,
                         "fully_visible": True}])
    import grn as grnmod
    grnmod.apply_vision_count(grn_id, ["p.jpg"])
    r = q1("select vision_pieces, pack_size from v_grn_verification "
           "where grn_id=%s and line_no=1", (grn_id,))
    assert r["pack_size"] == 21
    assert r["vision_pieces"] == 105, "5 packs of 21 must be 105 pieces, not 5"


@db
def test_loose_units_are_added_on_top_of_packs(grn, monkeypatch):
    from db import q1
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 1, "packs": 4, "loose": 7, "confidence": 1.0,
                         "fully_visible": True}])
    import grn as grnmod
    grnmod.apply_vision_count(grn_id, ["p.jpg"])
    assert q1("select vision_pieces from v_grn_verification where grn_id=%s and line_no=1",
              (grn_id,))["vision_pieces"] == 4 * 21 + 7


# ============================================================ false-alarm suppression
@db
def test_partially_hidden_stock_is_asked_about_not_declared_short(grn, monkeypatch):
    """A model can be certain it sees 3 packs while 3 more sit behind them. Reporting
    that as a shortage is a false alarm, and a few of those teach staff to ignore every
    count warning -- worse than not counting at all."""
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 2, "packs": 3, "loose": 0, "confidence": 1.0,
                         "fully_visible": False,
                         "note": "back of the pile is not visible"}])
    import grn as grnmod
    note = grnmod.apply_vision_count(grn_id, ["p.jpg"])
    assert "Could not count confidently" in note
    assert "do not match the invoice" not in note

    from db import q1
    flags = q1("select flags from grn_lines where grn_id=%s and line_no=2",
               (grn_id,))["flags"]
    assert "count_mismatch" not in (flags or [])


@db
def test_a_confident_full_view_shortage_is_reported(grn, monkeypatch):
    """The other side: when the model can see everything and the count differs, say so
    plainly. This is the feature."""
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 2, "packs": 3, "loose": 0, "confidence": 0.97,
                         "fully_visible": True}])
    import grn as grnmod
    note = grnmod.apply_vision_count(grn_id, ["p.jpg"])
    assert "do not match the invoice" in note

    from db import q1
    r = q1("""select vision_variance, flags from v_grn_verification v
                join grn_lines l on l.grn_id=v.grn_id and l.line_no=v.line_no
               where v.grn_id=%s and v.line_no=2""", (grn_id,))
    assert r["vision_variance"] == (3 - 6) * 24        # 3 packs short of 6
    assert "count_mismatch" in (r["flags"] or [])


@db
def test_low_confidence_is_never_reported_as_a_shortage(grn, monkeypatch):
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 2, "packs": 1, "loose": 0, "confidence": 0.3,
                         "fully_visible": True, "note": "very blurry"}])
    import grn as grnmod
    note = grnmod.apply_vision_count(grn_id, ["p.jpg"])
    assert "Could not count confidently" in note


@db
def test_missing_visibility_key_degrades_to_asking(grn, monkeypatch):
    """An older or looser model response must fail safe, not fail loud."""
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 2, "packs": 3, "loose": 0, "confidence": 1.0}])
    import grn as grnmod
    assert "Could not count confidently" in grnmod.apply_vision_count(grn_id, ["p.jpg"])


@db
def test_agreement_is_stated_so_silence_is_never_ambiguous(grn, monkeypatch):
    grn_id, _, _ = grn
    _stub(monkeypatch, [
        {"line_no": 1, "packs": 5, "loose": 0, "confidence": 1.0, "fully_visible": True},
        {"line_no": 2, "packs": 6, "loose": 0, "confidence": 1.0, "fully_visible": True},
    ])
    import grn as grnmod
    note = grnmod.apply_vision_count(grn_id, ["p.jpg"])
    assert "2 line(s) match the invoice" in note


@db
def test_photo_quality_and_unlisted_items_are_surfaced(grn, monkeypatch):
    """Unlisted items matter: stock in the delivery that is not on the invoice is
    either a supplier error or something nobody is going to get billed for."""
    grn_id, _, _ = grn
    _stub(monkeypatch,
          [{"line_no": 1, "packs": 5, "loose": 0, "confidence": 1.0,
            "fully_visible": True}],
          photo_quality="too dark", unlisted_items_seen=2)
    import grn as grnmod
    note = grnmod.apply_vision_count(grn_id, ["p.jpg"])
    assert "too dark" in note
    assert "not on this invoice" in note


@db
def test_goods_photos_are_kept_as_evidence(grn, monkeypatch):
    """A supplier disputing a short delivery three weeks later is answered by opening
    the GRN and seeing the photo."""
    from db import q1
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 1, "packs": 5, "loose": 0, "confidence": 1.0,
                         "fully_visible": True}])
    import grn as grnmod
    grnmod.apply_vision_count(grn_id, ["goods/a.jpg", "goods/b.jpg"])
    stored = str(q1("select goods_images from grns where id=%s", (grn_id,))["goods_images"])
    assert "goods/a.jpg" in stored and "goods/b.jpg" in stored


# ============================================================ never block receiving
def test_skip_is_always_available():
    """A 40-line delivery at closing time, a flat battery or a model outage must not
    stop stock being received. That is the one failure a pharmacy cannot absorb."""
    import inspect

    import grn as grnmod
    src = inspect.getsource(grnmod.handle_goods_reply)
    assert '"SKIP"' in src
    # and it must reach review rather than dead-ending
    assert "_to_review" in src


def test_vision_failure_still_reaches_the_review_step():
    import inspect

    import grn as grnmod
    src = inspect.getsource(grnmod.handle_goods_reply)
    assert "except Exception" in src and "_to_review" in src


# ============================================================ unresolved discrepancies
@db
def test_unconfirmed_discrepancy_survives_approval(grn, monkeypatch):
    """REGRESSION. Vision says 3 packs, invoice says 6, the pharmacist just replies OK.

    Receiving must still proceed -- blocking is worse -- but booking 6 as though the
    question was never raised leaves the ledger knowingly wrong with no trace. The
    disagreement has to remain answerable afterwards.
    """
    from db import q1
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 2, "packs": 3, "loose": 0, "confidence": 0.97,
                         "fully_visible": True}])
    import grn as grnmod
    grnmod.apply_vision_count(grn_id, ["p.jpg"])

    staff = q1("select * from staff where pharmacy_id=%s limit 1",
               (os.environ["PHARMACY_ID"],))
    monkeypatch.setattr(grnmod, "send_text", lambda *a, **k: None)
    grnmod.approve(grn_id, staff, staff["phone"])

    g = q1("""select status, unresolved_count_note from grns where id=%s""", (grn_id,))
    assert g["status"] == "approved", "receiving must not be blocked"
    assert g["unresolved_count_note"], "the unanswered discrepancy vanished"
    assert "never confirmed" in g["unresolved_count_note"]

    # and it is visible for a later supplier claim
    open_rows = q1("""select grn_id from v_open_receiving_discrepancies
                       where grn_id=%s""", (grn_id,))
    assert open_rows, "not surfaced in v_open_receiving_discrepancies"


@db
def test_a_confirmed_count_is_not_logged_as_unresolved(grn, monkeypatch):
    """Once a human answers, it is resolved -- it belongs in discrepancy_note, not the
    unanswered list."""
    from db import q1
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 2, "packs": 3, "loose": 0, "confidence": 0.97,
                         "fully_visible": True}])
    import grn as grnmod
    grnmod.apply_vision_count(grn_id, ["p.jpg"])
    grnmod._set_counted(grn_id, 2, (3, 0))          # pharmacist confirms

    staff = q1("select * from staff where pharmacy_id=%s limit 1",
               (os.environ["PHARMACY_ID"],))
    monkeypatch.setattr(grnmod, "send_text", lambda *a, **k: None)
    grnmod.approve(grn_id, staff, staff["phone"])

    g = q1("select discrepancy_note, unresolved_count_note from grns where id=%s",
           (grn_id,))
    assert g["unresolved_count_note"] is None
    assert g["discrepancy_note"], "a confirmed short delivery must still be recorded"


@db
def test_agreeing_count_leaves_nothing_open(grn, monkeypatch):
    from db import q1
    grn_id, _, _ = grn
    _stub(monkeypatch, [
        {"line_no": 1, "packs": 5, "loose": 0, "confidence": 1.0, "fully_visible": True},
        {"line_no": 2, "packs": 6, "loose": 0, "confidence": 1.0, "fully_visible": True},
    ])
    import grn as grnmod
    grnmod.apply_vision_count(grn_id, ["p.jpg"])
    staff = q1("select * from staff where pharmacy_id=%s limit 1",
               (os.environ["PHARMACY_ID"],))
    monkeypatch.setattr(grnmod, "send_text", lambda *a, **k: None)
    grnmod.approve(grn_id, staff, staff["phone"])
    g = q1("select unresolved_count_note from grns where id=%s", (grn_id,))
    assert g["unresolved_count_note"] is None


# ============================================================ ask for another photo
@db
def test_hidden_stock_triggers_a_request_for_another_photo(grn, monkeypatch):
    """One more photo is cheaper than a wrong count, and far cheaper than hand-counting
    40 boxes."""
    grn_id, _, _ = grn
    _stub(monkeypatch, [{"line_no": 2, "packs": 3, "loose": 0, "confidence": 1.0,
                         "fully_visible": False, "note": "back of the pile hidden"}])
    import grn as grnmod
    summary, needs_more = grnmod.apply_vision_count(grn_id, ["p.jpg"], return_flag=True)
    assert needs_more is True


@db
def test_a_clean_count_does_not_ask_for_more_photos(grn, monkeypatch):
    grn_id, _, _ = grn
    _stub(monkeypatch, [
        {"line_no": 1, "packs": 5, "loose": 0, "confidence": 1.0, "fully_visible": True},
        {"line_no": 2, "packs": 6, "loose": 0, "confidence": 1.0, "fully_visible": True},
    ])
    import grn as grnmod
    _, needs_more = grnmod.apply_vision_count(grn_id, ["p.jpg"], return_flag=True)
    assert needs_more is False


def test_the_extra_photo_request_is_asked_once_not_in_a_loop():
    """A loop that keeps demanding photos is the blocking behaviour SKIP exists to
    prevent."""
    import inspect

    import grn as grnmod
    src = inspect.getsource(grnmod.handle_goods_reply)
    assert "recounted" in src, "no guard against re-asking forever"
