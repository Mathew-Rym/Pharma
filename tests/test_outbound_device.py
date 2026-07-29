"""Outbound must be addressed to the pharmacy that owns the conversation.

Before this, `wa.py` stamped every send with one hardcoded X-Device-Id read from the
environment. With a single paired number that is invisible. With two, Pharmacy B's
customer receives their reply from Pharmacy A's number, and no amount of correct inbound
tenant resolution fixes it.

The trap these tests exist to catch: resolving the outbound device from request or
contextvar state. Sending happens off the request path in at least four places --
jobs.py reorder alerts, the Rx approval SLA escalation, the wa.py retry path and the
daily report push -- and by then there is no inbound message and no context. An
implementation that reads ambient state passes every webhook-driven test and crosses
wires the first time a cron job fires.

So the assertions here are about what is written on the record, never about what the
sender happened to have in scope.
"""
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


RECIPIENTS = ("254700000009", "254713755274")


@pytest.fixture(autouse=True)
def gate_one_off(monkeypatch):
    """These tests are about which device a message leaves by, not about Gate 1.

    With a WA_ALLOWLIST set in .env, Gate 1 refuses these fixture numbers before compose
    reaches the device logic -- so the result would depend on the developer's environment.
    """
    # Patch the object safety.py actually reads. `from config import settings`
    # here can hand back a different Settings instance than safety bound at
    # import time, in which case patching it has no effect and Gate 1 stays on --
    # which is why these tests passed alone and failed in the full suite.
    import safety
    monkeypatch.setattr(safety.settings, "WA_ALLOWLIST", "")


@pytest.fixture
def two_tenants():
    """Two pharmacies, each bound to its own GOWA slot and JID.

    Each also has the test recipients as customers WITH inbound history, because
    compose() now runs the anti-ban gates: a recipient who is a stranger, or who never
    messaged first, is refused. These tests are about which device a message leaves by,
    so the conversation has to already exist for them to reach that code at all.
    """
    from db import ex, q1
    mark = secrets.token_hex(3).lower()
    made = {}
    for side in ("a", "b"):
        row = q1("""insert into pharmacies (name, wa_number, wa_jid, gowa_device_id, kind)
                    values (%s,%s,%s,%s,'tenant') returning id""",
                 (f"OUT-{side.upper()}-{mark}", f"2547{side*2}000{mark[:3]}",
                  f"2547{side*2}000{mark[:3]}@s.whatsapp.net", f"slot-{side}-{mark}"))
        pid = str(row["id"])
        for phone in RECIPIENTS:
            ex("insert into customers (pharmacy_id, phone) values (%s,%s)", (pid, phone))
            ex("""insert into inbound_history (pharmacy_id, phone)
                  values (%s,%s) on conflict do nothing""", (pid, phone))
        made[side] = {"pid": pid,
                      "slot": f"slot-{side}-{mark}",
                      "jid": f"2547{side*2}000{mark[:3]}@s.whatsapp.net"}
    yield made
    for side in ("a", "b"):
        pid = made[side]["pid"]
        for t in ("inbound_history", "wa_messages", "customers"):
            ex(f"delete from {t} where pharmacy_id=%s", (pid,))
        ex("delete from pharmacies where id=%s", (pid,))


@db
def test_compose_persists_the_device_on_the_outbound_row(two_tenants):
    """The device is decided when the message is composed, and written down.

    Would fail if compose resolved the device at send time from ambient context, or
    left the column null for the sender to fill in from a setting.
    """
    import wa
    from db import q1

    b = two_tenants["b"]
    row_id = wa.compose(b["pid"], "254700000009", "text", "stock alert")

    row = q1("""select pharmacy_id, gowa_device_id, expected_wa_jid, status, direction
                  from wa_messages where id=%s""", (row_id,))
    assert str(row["pharmacy_id"]) == b["pid"]
    assert row["gowa_device_id"] == b["slot"], "device must be B's slot, not a default"
    assert row["expected_wa_jid"] == b["jid"], "the JID to verify against at send time"
    assert row["status"] == "queued"
    assert row["direction"] == "out"


@db
def test_compose_normalises_the_destination(two_tenants):
    """Normalisation moved from send_text into compose; keep it covered.

    A local 07xx number must be stored as 2547xx or the transport silently fails.
    """
    import wa
    from db import q1

    row_id = wa.compose(two_tenants["a"]["pid"], "0713755274", "text", "hi")
    assert q1("select to_phone from wa_messages where id=%s",
              (row_id,))["to_phone"] == "254713755274"


@db
def test_compose_refuses_a_pharmacy_with_no_paired_device(two_tenants):
    """An unpaired pharmacy must fail loudly, never silently pick a device.

    Would fail if compose left gowa_device_id null for the sender to fill in from
    settings.GOWA_DEVICE_ID -- which is exactly how a reply leaves by the wrong number.
    """
    import wa
    from db import q1

    unpaired = q1("""insert into pharmacies (name, kind) values ('OUT-UNPAIRED','tenant')
                     returning id""")
    try:
        with pytest.raises(wa.UnroutableMessage):
            wa.compose(str(unpaired["id"]), "254700000009", "text", "hello")
        assert q1("""select count(*) as n from wa_messages where pharmacy_id=%s""",
                  (str(unpaired["id"]),))["n"] == 0, "must not leave a queued row behind"
    finally:
        from db import ex
        ex("delete from wa_messages where pharmacy_id=%s", (str(unpaired["id"]),))
        ex("delete from pharmacies where id=%s", (str(unpaired["id"]),))


@db
def test_deliver_refuses_when_the_slot_is_not_the_expected_handset(two_tenants):
    """Outbound is addressed by slot label, so the label must be proven before use.

    No mock: these fixture slots genuinely do not exist in GOWA, so the guard has real
    cause to refuse. Delete a slot and recreate one reusing the name and it points at a
    different handset while every log line still looks right -- this is what turns that
    from silent misdelivery into a visible failure.

    Would fail if deliver trusted gowa_device_id without checking the live JID.
    """
    import wa
    from db import q1

    b = two_tenants["b"]
    row_id = wa.compose(b["pid"], "254700000009", "text", "hi")

    assert wa.deliver(row_id) is False

    row = q1("select status, last_error from wa_messages where id=%s", (row_id,))
    assert row["status"] == "refused"
    assert "jid" in (row["last_error"] or "").lower()
