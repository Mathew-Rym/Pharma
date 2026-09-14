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
        # suppliers and onboarding_contacts included: a leaked suppliers row would make
        # resolve_by_sender return a deleted pharmacy on a later run, which reads as a
        # resolution bug rather than test residue.
        for t in ("inbound_history", "wa_messages", "customers", "staff", "suppliers",
                  "onboarding_contacts"):
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
    works at both, and the caller has to ask rather than guess.

    Uses STAFF, not customers. Being staff somewhere is an operational relationship and
    genuinely identifies who a number acts for; being a customer does not (see
    test_being_a_customer_is_not_an_identity below). This test's job is to keep the
    ambiguity branch -- and router._ask_which_pharmacy -- reachable at all.
    """
    import tenancy
    from db import ex

    for pid in (pair["a"], pair["b"]):
        ex("""insert into staff (pharmacy_id, name, phone, role, is_active)
              values (%s,'Works At Both','254755666777','pharmacist',true)""", (pid,))

    found = tenancy.resolve_by_sender("254755666777")
    assert len(found) == 2
    assert set(found) == {pair["a"], pair["b"]}


@db
def test_being_a_customer_is_not_an_identity(pair):
    """THE test in this file after the A1 finding.

    resolve_by_sender answers "which pharmacy does this number act FOR?". Shopping at a
    pharmacy is not acting for it, and it is many-to-many by nature -- a person who buys
    from three chemists is ordinary, not ambiguous. Reading customers here was a category
    error with a concrete cost, proven by driving the real flow:

      a stranger's FIRST message auto-creates a customers row (router._handle_customer ->
      rx.get_or_create_customer), before consent. So a pharmacy owner who registered
      through a host line and later sent that line any ordinary message resolved to TWO
      pharmacies and was answered "which one?" from then on, permanently.

    Filtering on consent_given does not fix it -- the collision just moves from "texted
    once" to "consented at two", and consenting at two pharmacies is ordinary behaviour.
    """
    import tenancy
    from db import ex

    for pid in (pair["a"], pair["b"]):
        ex("""insert into customers (pharmacy_id, phone, name)
              values (%s,'254755777888','Shops At Both')""", (pid,))

    assert tenancy.resolve_by_sender("254755777888") == [], (
        "customers must not be an identity signal; device_id names the tenant for "
        "customer traffic")


@db
def test_a_supplier_still_resolves(pair):
    """Suppliers stay in the resolver: a distributor texting about a delivery IS acting
    for a pharmacy. Note the shape though -- one distributor serving twenty pharmacies is
    twenty rows, so at scale this returns twenty candidates and asks. Asking is correct;
    guessing would not be. Recorded so it is not rediscovered as a surprise."""
    import tenancy
    from db import ex

    ex("""insert into suppliers (pharmacy_id, name, phone)
          values (%s,'Distributor','254755999000')""", (pair["a"],))
    assert tenancy.resolve_by_sender("254755999000") == [pair["a"]]


@db
def test_loyalty_points_do_not_leak_between_pharmacies(pair):
    """Loyalty is keyed (pharmacy_id, phone) and must never consult resolve_by_sender.

    Now that a customer resolves to no pharmacy at all, any balance lookup that reached
    for the resolver would read zero pharmacies and either fail or fall back. It must
    read the BOUND tenant instead -- and return that pharmacy's points only, never the
    other's and never a sum. Points are money; a sum would let someone spend A's balance
    at B.
    """
    import tenancy
    from db import ex, q1

    phone = "254756111222"
    ex("""insert into customers (pharmacy_id, phone, name, loyalty_points)
          values (%s,%s,'Both Shops',300)""", (pair["a"], phone))
    ex("""insert into customers (pharmacy_id, phone, name, loyalty_points)
          values (%s,%s,'Both Shops',25)""", (pair["b"], phone))

    def balance():
        return q1("select loyalty_points from customers where pharmacy_id=%s and phone=%s",
                  (tenancy.pid(), phone))["loyalty_points"]

    with tenancy.pharmacy_scope(pair["a"]):
        assert balance() == 300
    with tenancy.pharmacy_scope(pair["b"]):
        assert balance() == 25

    # Sanity: both rows really do exist, so the scoped reads above were selective rather
    # than accidentally correct because one row was missing.
    total = q1("select sum(loyalty_points) s from customers where phone=%s", (phone,))["s"]
    assert total == 325


@db
def test_a_customer_on_a_bound_device_still_reaches_the_customer_branch(pair, monkeypatch):
    """The regression that removing customers from the resolver could plausibly cause.

    Customers are now invisible to resolve_by_sender, so if the router consulted the
    sender BEFORE the device, every customer message would hit _greet_unknown and the
    entire ordering flow would go silent -- with nothing in the logs but "unresolved
    sender". It does not: main.webhook_gowa sets pharmacy_id from device_id, and the
    router only falls back to the sender when that is absent.

    Asserts the ORDER, not just the outcome.
    """
    import router
    from db import ex

    phone = "254756555666"
    ex("insert into customers (pharmacy_id, phone) values (%s,%s)", (pair["a"], phone))

    reached = []
    monkeypatch.setattr(router, "_handle_customer",
                        lambda ph, msg, text: reached.append(ph))
    monkeypatch.setattr(router, "_greet_unknown",
                        lambda ph: reached.append("GREETED-AS-UNKNOWN"))

    router.handle_inbound({"wa_id": f"tenancy-{secrets.token_hex(4)}", "from": phone,
                           "type": "text", "text": "do you have panadol",
                           "pharmacy_id": pair["a"]})

    assert reached == [phone], f"expected the customer branch, got {reached}"


@db
def test_gate_two_still_passes_for_a_customer_only_number(pair):
    """Gate 2 and the resolver read `customers` for DIFFERENT reasons, and only the
    resolver changed.

    Gate 2 asks "may we reply to this number?" -- and a customer we have a row for is
    exactly who we may reply to. If this regressed, every ordinary customer conversation
    would go silent while looking perfectly healthy in the logs.
    """
    import safety
    from db import ex

    phone = "254756333444"
    ex("""insert into customers (pharmacy_id, phone) values (%s,%s)""", (pair["a"], phone))

    assert safety.has_relationship(phone, pair["a"]) is True
    assert safety.has_relationship(phone, pair["b"]) is False, "and only at that pharmacy"


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
