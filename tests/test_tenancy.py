"""Tenant resolution.

Two signals, deliberately ranked:

  device JID  — which of OUR numbers received the message. The right answer once each
                pharmacy has its own paired SIM, because it cannot be spoofed by the
                sender and does not care who is texting.
  sender phone — who is texting. The only signal available while a single number serves
                more than one pharmacy, which is the Thursday reality.

The trap these tests exist to prevent is first-match-wins. The spec says a person may
deliberately exist at two pharmacies (separate history, separate balances), so "return
the first row we find" silently locks them into whichever tenant was created first and is
undetectable from the outside.
"""
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


@pytest.fixture
def pair():
    """Two tenants plus a platform row, each with its own JID and slot."""
    from db import ex, q1
    mark = secrets.token_hex(3).lower()
    made = {}
    for side in ("a", "b"):
        row = q1("""insert into pharmacies (name, wa_number, wa_jid, gowa_device_id, kind)
                    values (%s,%s,%s,%s,'tenant') returning id""",
                 (f"TEN-{side.upper()}-{mark}", f"2547{side*2}11{mark[:4]}",
                  f"2547{side*2}11{mark[:4]}@s.whatsapp.net", f"slot-{side}-{mark}"))
        made[side] = str(row["id"])
    plat = q1("""insert into pharmacies (name, wa_jid, gowa_device_id, kind)
                 values (%s,%s,%s,'platform') returning id""",
              (f"TEN-PLAT-{mark}", f"254700{mark[:6]}@s.whatsapp.net", f"slot-p-{mark}"))
    made["platform"] = str(plat["id"])
    made["jid_a"] = f"2547aa11{mark[:4]}@s.whatsapp.net"
    made["jid_platform"] = f"254700{mark[:6]}@s.whatsapp.net"
    yield made
    for key in ("a", "b", "platform"):
        for t in ("inbound_history", "wa_messages", "customers", "staff"):
            ex(f"delete from {t} where pharmacy_id=%s", (made[key],))
        ex("delete from pharmacies where id=%s", (made[key],))


@db
def test_a_known_device_resolves_to_its_tenant(pair):
    import tenancy
    r = tenancy.resolve(device_jid=pair["jid_a"], sender_phone="254799000111")
    assert r.kind == "tenant"
    assert r.pharmacy_id == pair["a"]


@db
def test_the_platform_line_is_not_a_tenant(pair):
    """Three outcomes, not two. Platform must be distinguishable from unknown, or a
    genuine routing failure and normal platform traffic look identical while debugging."""
    import tenancy
    r = tenancy.resolve(device_jid=pair["jid_platform"], sender_phone="254799000111")
    assert r.kind == "platform"
    assert r.pharmacy_id is None


@db
def test_an_unknown_device_resolves_to_nothing_rather_than_a_default(pair):
    """Never fall back to a configured tenant: that is how one pharmacy's customer ends
    up in another pharmacy's data."""
    import tenancy
    r = tenancy.resolve(device_jid="254700999999@s.whatsapp.net",
                        sender_phone="254799000111")
    assert r.kind == "unknown"
    assert r.pharmacy_id is None


@db
def test_the_sender_decides_when_one_number_serves_several_pharmacies(pair):
    """The Thursday reality: one paired SIM, two tenants. Staff identity is the only
    signal that can tell them apart."""
    import tenancy
    from db import ex

    ex("""insert into staff (pharmacy_id, name, phone, role, is_active)
          values (%s,'A Manager','254733111222','manager',true)""", (pair["a"],))
    ex("""insert into staff (pharmacy_id, name, phone, role, is_active)
          values (%s,'B Manager','254733333444','manager',true)""", (pair["b"],))

    assert tenancy.resolve_by_sender("254733111222") == [pair["a"]]
    assert tenancy.resolve_by_sender("254733333444") == [pair["b"]]


@db
def test_a_person_at_two_pharmacies_is_reported_as_ambiguous(pair):
    """First-match-wins would silently lock them into one tenant forever.

    Would fail if resolve_by_sender returned a single id: this person legitimately
    exists at both, and the caller has to ask rather than guess.
    """
    import tenancy
    from db import ex

    for pid in (pair["a"], pair["b"]):
        ex("""insert into customers (pharmacy_id, phone, name)
              values (%s,'254755666777','Shops At Both')""", (pid,))

    found = tenancy.resolve_by_sender("254755666777")
    assert len(found) == 2
    assert set(found) == {pair["a"], pair["b"]}


@db
def test_an_inactive_staff_member_does_not_resolve(pair):
    """Removing someone must revoke their access, not just hide them from lists."""
    import tenancy
    from db import ex

    ex("""insert into staff (pharmacy_id, name, phone, role, is_active)
          values (%s,'Gone','254744555666','manager',false)""", (pair["a"],))
    assert tenancy.resolve_by_sender("254744555666") == []


def test_pid_raises_when_no_tenant_is_set():
    """Never a default. The whole point of removing nine constants is removing nine
    implicit defaults; an accessor that falls back reinstates all of them at once, and
    the symptom is data written to the wrong pharmacy with clean-looking logs."""
    import tenancy
    tenancy.clear_pharmacy()
    with pytest.raises(tenancy.NoTenant):
        tenancy.pid()


def test_the_tenant_does_not_leak_to_the_next_request():
    """Workers are reused. A tenant left set would serve the next pharmacy's message
    with the previous pharmacy's data."""
    import tenancy
    tenancy.clear_pharmacy()          # opt out of conftest's default binding
    with tenancy.pharmacy_scope("11111111-1111-1111-1111-111111111111"):
        assert tenancy.pid() == "11111111-1111-1111-1111-111111111111"
    with pytest.raises(tenancy.NoTenant):
        tenancy.pid()


def test_nested_scopes_restore_the_outer_tenant():
    """The jobs loop sets a tenant per iteration; an inner scope must not clobber it."""
    import tenancy
    with tenancy.pharmacy_scope("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"):
        with tenancy.pharmacy_scope("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"):
            assert tenancy.pid().startswith("bbbb")
        assert tenancy.pid().startswith("aaaa")


def test_no_module_still_pins_itself_to_a_boot_time_tenant():
    """Structural sweep: the nine PID = settings.PHARMACY_ID constants must be gone.

    Grepping for the assignment rather than trusting nine separate edits, because one
    missed module silently keeps serving a single tenant while everything looks migrated.
    """
    import pathlib
    api = pathlib.Path(__file__).parent.parent / "api"
    offenders = []
    for p in api.glob("*.py"):
        # Only a real assignment at module level counts. Matching anywhere in the file
        # flags prose: tenancy.py's own docstring names the constant to explain what it
        # replaces, and my first version of this test failed on that.
        for line in p.read_text().splitlines():
            if line.startswith("PID = settings.PHARMACY_ID"):
                offenders.append(p.name)
                break
    assert offenders == [], f"still pinned to one tenant at import: {offenders}"


def test_the_resolver_does_not_import_config_at_all():
    """Structural: the resolver must not be able to reach a configured tenant.

    The point of this module is to replace nine boot-time `settings.PHARMACY_ID`
    constants. Reading that same constant here would reinstate all nine behind one
    function call, and the failure is invisible -- messages land in the wrong pharmacy
    while every log line looks correct.

    Checked via the import graph rather than a substring search: the docstring legitimately
    mentions PHARMACY_ID to explain what it replaces, and a text match cannot tell prose
    from a dependency.
    """
    import ast
    import inspect

    import tenancy

    tree = ast.parse(inspect.getsource(tenancy))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "config" not in imported, f"tenancy must not depend on config; imports {imported}"
