"""A code grants a role. It never revokes one.

Observed live on the night of the first real registration: one phone sent
`OWNER <code>` at 22:04 and became a manager, then sent `JOIN <code>` at 22:07 and was
silently demoted to attendant, because the upsert ended `do update set role =
excluded.role`. Last-code-wins is the wrong rule. The failure is quiet -- nobody notices
until something they are entitled to do stops working, and by then the cause is three days
old.

staff.role also decides who may approve a prescription-only medicine, and that approval is
logged against a PPB number. So this is not a convenience question: role is part of the
regulatory record, which is why every change is written to staff_role_changes.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


# ============================================================ the ranking itself
def test_rank_covers_exactly_the_roles_the_database_allows():
    """A rank table missing a role would make that role's precedence undefined, and the
    comparison would fall back to whatever the default is -- silently."""
    import register
    schema = open(os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")).read()
    m = re.search(r"role\s+text not null check \(role in \(([^)]+)\)\)", schema)
    assert m, "could not find the staff.role CHECK constraint"
    allowed = {v.strip().strip("'") for v in m.group(1).split(",")}
    assert set(register.ROLE_RANK) == allowed, (
        f"ROLE_RANK={set(register.ROLE_RANK)} does not match the constraint {allowed}")


def test_owner_outranks_manager_outranks_pharmacist_outranks_attendant():
    import register
    r = register.ROLE_RANK
    assert r["owner"] > r["manager"] > r["pharmacist"] > r["attendant"]


@pytest.mark.parametrize("existing,granted,expected", [
    (None,         "attendant", "attendant"),   # new person + JOIN
    (None,         "manager",   "manager"),     # new person + OWNER
    ("attendant",  "manager",   "manager"),     # upgrade
    ("manager",    "attendant", "manager"),     # JOIN must NOT demote a manager
    ("owner",      "manager",   "owner"),       # nor demote an owner
    ("owner",      "attendant", "owner"),
    ("manager",    "manager",   "manager"),     # idempotent
    ("pharmacist", "attendant", "pharmacist"),  # a pharmacist keeps clinical authority
    ("pharmacist", "manager",   "manager"),     # manager outranks pharmacist
])
def test_a_code_never_lowers_an_existing_role(existing, granted, expected):
    import register
    assert register.effective_role(existing, granted) == expected


def test_no_code_path_writes_a_role_lower_than_the_one_held():
    """Structural guard. The bug was a single SQL clause -- `do update set role =
    excluded.role` -- so an implementation that computes precedence in Python and then
    still lets the database overwrite unconditionally would pass the unit tests above and
    fail in production."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "api", "register.py")).read()
    # Comment lines are stripped first. The fix's own explanation quotes the offending
    # clause verbatim, so a naive substring search matches the commentary that exists to
    # stop the bug coming back -- which would make this test fail forever and teach whoever
    # inherits it to delete the explanation.
    code = " ".join(line for line in src.split("\n")
                    if not line.lstrip().startswith("#"))
    assert "set role = excluded.role" not in code, (
        "the upsert must not overwrite role unconditionally; use effective_role()")


# ============================================================ against the database
@pytest.fixture
def pharmacy():
    import secrets
    from db import ex, q1
    mark = secrets.token_hex(3).lower()
    row = q1("""insert into pharmacies (name, kind, status, wa_number)
                values (%s,'tenant','pending_activation',%s) returning id""",
             (f"ROLE-{mark}", f"2547rr{mark[:6]}"))
    pid = str(row["id"])
    yield pid
    for t in ("staff_role_changes", "staff", "inbound_history", "wa_messages"):
        ex(f"delete from {t} where pharmacy_id=%s", (pid,))
    ex("delete from pharmacies where id=%s", (pid,))


@db
def test_join_after_owner_does_not_demote_and_is_audited(pharmacy, monkeypatch):
    """The exact live sequence: OWNER then JOIN, three minutes apart."""
    import register
    from db import q, q1

    monkeypatch.setattr(register, "_say", lambda *a, **k: True)
    phone = "254733900111"

    register._grant_role(pharmacy, phone, "manager", mechanism="owner_code")
    assert q1("select role from staff where pharmacy_id=%s and phone=%s",
              (pharmacy, phone))["role"] == "manager"

    register._grant_role(pharmacy, phone, "attendant", mechanism="join_code")
    assert q1("select role from staff where pharmacy_id=%s and phone=%s",
              (pharmacy, phone))["role"] == "manager", "JOIN demoted a manager"

    rows = q("""select old_role, new_role, mechanism from staff_role_changes
                 where pharmacy_id=%s and phone=%s order by created_at""", (pharmacy, phone))
    assert len(rows) == 1, f"a no-op must not write an audit row: {rows}"
    assert rows[0]["old_role"] is None and rows[0]["new_role"] == "manager"
    assert rows[0]["mechanism"] == "owner_code"


@db
def test_an_upgrade_is_recorded_with_both_roles(pharmacy, monkeypatch):
    import register
    from db import q

    monkeypatch.setattr(register, "_say", lambda *a, **k: True)
    phone = "254733900222"
    register._grant_role(pharmacy, phone, "attendant", mechanism="join_code")
    register._grant_role(pharmacy, phone, "manager", mechanism="owner_code")

    rows = q("""select old_role, new_role, mechanism, actor from staff_role_changes
                 where pharmacy_id=%s and phone=%s order by created_at""", (pharmacy, phone))
    assert [(r["old_role"], r["new_role"]) for r in rows] == [
        (None, "attendant"), ("attendant", "manager")]
    assert rows[-1]["mechanism"] == "owner_code"
    assert rows[-1]["actor"] == phone


@db
def test_the_audit_trail_is_append_only_in_practice(pharmacy, monkeypatch):
    """Two grants, two rows, and the first is still readable afterwards. A history that
    gets overwritten answers the regulatory question with only the latest value."""
    import register
    from db import q

    monkeypatch.setattr(register, "_say", lambda *a, **k: True)
    phone = "254733900333"
    register._grant_role(pharmacy, phone, "attendant", mechanism="join_code")
    register._grant_role(pharmacy, phone, "pharmacist", mechanism="dashboard")
    rows = q("select new_role from staff_role_changes where pharmacy_id=%s and phone=%s "
             "order by created_at", (pharmacy, phone))
    assert [r["new_role"] for r in rows] == ["attendant", "pharmacist"]
