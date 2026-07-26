"""Onboarding tests.

`staff.phone` is the whole access-control model: WhatsApp answers a number that is in
this table and ignores one that is not. So the failure mode these tests exist to
prevent is not a crash — it is a staff member being added successfully in the
dashboard and then silently getting no reply forever, with no error anywhere.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))


# ============================================================ the silent-failure risk
@pytest.mark.parametrize("raw", [
    "0713755274",
    "+254713755274",
    "254713755274",
    "+254 713 755 274",
    "0713 755 274",
    "713755274",
    "0110123456",          # the 01xx range Safaricom added later
])
def test_dashboard_and_api_normalise_phones_identically(raw):
    """THE test in this file.

    The dashboard writes staff.phone; api/utils.norm_phone() looks it up on every
    inbound message. If the two ever disagree, the number is stored in a form the
    router will never match: the staff member is 'added', and simply never gets a
    reply. Nothing logs an error, because as far as the router is concerned they are
    just not staff.
    """
    from onboarding import norm_phone as dash_norm
    from utils import norm_phone as api_norm
    assert dash_norm(raw) == api_norm(raw), raw


def test_normalised_form_is_what_whatsapp_reports():
    from onboarding import norm_phone
    assert norm_phone("0713755274") == "254713755274"
    assert norm_phone("") == ""
    assert norm_phone(None) == ""


@pytest.mark.parametrize("raw,ok", [
    ("0713755274", True),
    ("+254713755274", True),
    ("0110123456", True),        # 01xx mobile
    ("254613755274", False),     # 06xx is not a mobile range
    ("0713", False),
    ("not a phone", False),
    ("", False),
])
def test_only_plausible_kenyan_mobiles_are_accepted(raw, ok):
    """A typo'd number is worse than a rejected one: it looks added and never works."""
    from onboarding import _valid, norm_phone
    assert _valid(norm_phone(raw)) is ok, raw


def test_every_staff_role_has_help_text():
    """The person onboarding a pharmacy is deciding who can approve a prescription.
    An unexplained dropdown is how an attendant ends up as a pharmacist."""
    from onboarding import ROLE_HELP, ROLES
    assert set(ROLES) == set(ROLE_HELP)
    for role, text in ROLE_HELP.items():
        assert len(text) > 30, role


def test_roles_match_the_database_constraint():
    """staff.role has a CHECK constraint; an option the DB rejects would only fail at
    the moment someone tries to save."""
    import re
    schema = open(os.path.join(os.path.dirname(__file__), "..", "db",
                               "schema.sql")).read()
    m = re.search(r"role\s+text not null check \(role in \(([^)]+)\)\)", schema)
    assert m, "could not find the staff.role constraint"
    allowed = {v.strip().strip("'") for v in m.group(1).split(",")}
    from onboarding import ROLES
    assert set(ROLES) == allowed


# ============================================================ manual ingest wiring
def test_manual_ingest_routes_each_export_shape():
    from manual_ingest import ingest
    assert "Unrecognised" in ingest("nonsense", [], "some-pid")


def test_manual_ingest_does_not_reimplement_the_ledger():
    """Loop B's correctness (FEFO, pack-vs-piece, idempotency) lives in
    apply_pos_sales(). A second import path that writes stock_movements itself would
    silently diverge from it — that is exactly the class of bug already fixed once.
    """
    src = open(os.path.join(os.path.dirname(__file__), "..", "dashboard",
                            "manual_ingest.py")).read()
    assert "apply_pos_sales" in src
    assert "stock_movements" not in src, (
        "manual_ingest must not write the ledger directly; route through "
        "apply_pos_sales() so FEFO and unit resolution stay in one place")
