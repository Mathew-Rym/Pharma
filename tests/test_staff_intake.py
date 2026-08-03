"""A staff photo needs declared intent, not a guess.

Every staff image used to become a supplier invoice page unconditionally. A pharmacist
photographing a walk-in's prescription had no way to say so: it went to the invoice
extractor, and if that found line-shaped text it would move stock. There is no undo for
that, and nothing in the reply would have looked wrong.

The two things a staff photo can be move stock in OPPOSITE directions -- an invoice adds
it, a prescription dispenses it -- which is why guessing was the wrong shape of answer.

RECEIVE is a deterministic keyword. No model sits in this path, for the same reason
register.py is a pure function: a classifier being 95% right is 1-in-20 deliveries filed
as something else.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


@pytest.fixture
def staff_bot(monkeypatch):
    """Drive _handle_staff with state and replies captured, and grn stubbed."""
    import grn
    import router
    state = {"flow": "idle", "context": {}}
    replies, grn_calls = [], []

    monkeypatch.setattr(router, "reply_text", lambda p, b: replies.append(b))
    monkeypatch.setattr(router, "get_state", lambda p: dict(state))
    monkeypatch.setattr(router, "set_state",
                        lambda p, f, c, **k: state.update({"flow": f, "context": c}))
    monkeypatch.setattr(router, "clear_state",
                        lambda p: state.update({"flow": "idle", "context": {}}))
    monkeypatch.setattr(grn, "add_page",
                        lambda p, path: grn_calls.append(("add_page", path)))
    monkeypatch.setattr(grn, "add_goods_photo",
                        lambda p, path: grn_calls.append(("add_goods_photo", path)))

    def send(text="", kind="text", path=None, role="manager"):
        msg = {"type": kind, "media_path": path, "wa_id": "t1"}
        router._handle_staff("254700000001", {"role": role, "id": "s1"}, msg, text)
        return replies, grn_calls

    return {"send": send, "state": state, "replies": replies, "grn": grn_calls}


# ============================================================ the guess is gone
def test_a_bare_photo_with_no_active_flow_asks_instead_of_filing_it(staff_bot):
    """THE test. This photo used to become invoice page 1 with no way to object."""
    replies, grn_calls = staff_bot["send"](kind="image", path="2026/08/rx.jpg")
    assert grn_calls == [], "the photo was filed without being asked about"
    assert len(replies) == 1
    body = replies[0]
    assert "RECEIVE" in body, "must say how to declare it IS an invoice"
    assert "prescription" in body.lower(), "must address the other thing it could be"
    assert "haven't filed it" in body.lower() or "not filed" in body.lower()


def test_receive_starts_the_invoice_flow(staff_bot):
    replies, _ = staff_bot["send"]("RECEIVE")
    assert staff_bot["state"]["flow"] == "grn_collect"
    assert staff_bot["state"]["context"] == {"pages": []}
    assert "invoice" in replies[-1].lower() and "DONE" in replies[-1]


def test_receiving_is_accepted_too(staff_bot):
    """People type the natural word, not the command word."""
    staff_bot["send"]("RECEIVING")
    assert staff_bot["state"]["flow"] == "grn_collect"


def test_a_photo_after_receive_attaches_as_an_invoice_page(staff_bot):
    staff_bot["send"]("RECEIVE")
    _, grn_calls = staff_bot["send"](kind="image", path="2026/08/inv1.jpg")
    assert grn_calls == [("add_page", "2026/08/inv1.jpg")]


def test_a_photo_mid_count_is_the_goods_not_another_invoice_page(staff_bot):
    """Order matters: after the invoice is read, a photo is the delivery being counted.
    Getting this wrong sends a photo of the counter to the invoice extractor."""
    staff_bot["state"].update({"flow": "grn_goods", "context": {}})
    _, grn_calls = staff_bot["send"](kind="image", path="2026/08/goods.jpg")
    assert grn_calls == [("add_goods_photo", "2026/08/goods.jpg")]


def test_receive_is_available_to_an_attendant(staff_bot):
    """Receiving a delivery is an attendant's core job. Gating it would recreate the
    problem role scoping was meant to solve, not fix one."""
    from reports import may_use
    assert may_use("attendant", "receive_goods") is True
    staff_bot["send"]("RECEIVE", role="attendant")
    assert staff_bot["state"]["flow"] == "grn_collect"


def test_receive_appears_in_every_role_help(staff_bot):
    from router import _staff_help
    for role in ("attendant", "pharmacist", "manager", "owner"):
        assert "*RECEIVE*" in _staff_help(role), f"{role} is not told RECEIVE exists"


def test_no_model_is_consulted_to_decide_what_a_photo_is():
    """Structural. A classifier here would be wrong occasionally, and 'occasionally' means
    a delivery filed as a prescription with no undo."""
    import inspect

    import router
    src = inspect.getsource(router._handle_staff)
    head = src[:src.index("if up == \"HELP\"")]
    for banned in ("chat(", "_agent_reply", "llm"):
        assert banned not in head, f"{banned} appears in the photo-intent path"
