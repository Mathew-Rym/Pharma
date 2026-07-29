"""WhatsApp anti-ban gates, and the phone normalisation they depend on.

These gates exist to stop the pharmacy's number being banned. That makes correctness
here unusually load-bearing: a gate that lets one wrong message through is the thing it
was built to prevent, and a normaliser that invents a number sends to a stranger.
"""
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


# ------------------------------------------------------------------ normalisation
def test_a_ten_digit_number_is_never_turned_into_a_different_number():
    """The one failure mode that must not exist: silently messaging a stranger.

    A bare 10-digit string starting with 7 is not a valid Kenyan format (mobiles are
    9 significant digits, optionally with a leading 0). Dropping a digit to force it to
    fit produces a DIFFERENT, plausible-looking subscriber -- so the message reaches
    someone who never contacted the pharmacy, which is precisely what gets a number
    reported and banned.

    It must either normalise to itself or be rejected by validation. Never mutated.
    """
    from utils import is_valid_ke_mobile, norm_phone

    got = norm_phone("7137552744")
    assert got != "254137552744", "a digit was deleted, changing whose number this is"
    assert not is_valid_ke_mobile(got), "an ambiguous 10-digit input must not validate"


def test_the_real_kenyan_formats_still_normalise():
    """Guard against fixing the above by breaking the formats people actually type."""
    from utils import norm_phone
    assert norm_phone("0713755274") == "254713755274"
    assert norm_phone("713755274") == "254713755274"
    assert norm_phone("254713755274") == "254713755274"
    assert norm_phone("+254 713 755 274") == "254713755274"
    assert norm_phone("254713755274@s.whatsapp.net") == "254713755274"


# ------------------------------------------------------------------ gates
@pytest.fixture
def tenant():
    from db import ex, q1
    mark = secrets.token_hex(3).lower()
    row = q1("""insert into pharmacies (name, wa_number, wa_jid, gowa_device_id, kind)
                values (%s,'254700000001','254700000001@s.whatsapp.net',%s,'tenant')
                returning id""", (f"GATE-{mark}", f"slot-{mark}"))
    pid = str(row["id"])
    yield {"pid": pid, "slot": f"slot-{mark}"}
    for t in ("inbound_history", "wa_messages", "customers"):
        ex(f"delete from {t} where pharmacy_id=%s", (pid,))
    ex("delete from pharmacies where id=%s", (pid,))


@db
def test_gate_three_blocks_a_number_that_never_messaged_us(tenant):
    """Cold outreach is the ban risk. A known customer is still not permission to
    open a conversation -- WhatsApp cares about who spoke first."""
    import safety
    from db import ex

    # A relationship exists (so Gate 2 passes) but no inbound history.
    ex("""insert into customers (pharmacy_id, phone) values (%s,'254711222333')""",
       (tenant["pid"],))

    with pytest.raises(safety.GateBlocked) as e:
        safety.check_gates("254711222333", tenant["pid"], tenant["slot"])
    assert e.value.gate == "chat_established"


@db
def test_recording_an_inbound_message_opens_the_gate(tenant):
    """The router records every inbound, which is what makes replies legal."""
    import safety
    from db import ex

    ex("""insert into customers (pharmacy_id, phone) values (%s,'254711222444')""",
       (tenant["pid"],))
    safety.record_inbound("254711222444", tenant["pid"])

    safety.check_gates("254711222444", tenant["pid"], tenant["slot"])  # must not raise


@db
def test_a_caller_cannot_declare_its_way_past_gate_three(tenant):
    """There must be no argument that turns the gate off.

    A bypass flag is indistinguishable from no gate: every proactive sender that wants
    to send will pass it. Replies are already legal because the router records the
    inbound first, so the flag buys nothing and costs the whole protection.
    """
    import inspect

    import safety
    import wa

    assert "is_reply" not in inspect.signature(safety.check_gates).parameters
    assert "is_reply" not in inspect.signature(wa.compose).parameters


@db
def test_being_on_the_staff_list_is_not_inbound_history(tenant):
    """Membership is Gate 2. Gate 3 requires that they actually messaged us.

    The v10 backfill seeded inbound_history from customers/staff/suppliers membership,
    which fabricates consent: a manager types a colleague's number, and the system then
    believes that colleague opened a conversation. That is how a staff number receives
    unsolicited automation and reports it. Only real inbound messages count.
    """
    import safety
    from db import ex, q1

    ex("""insert into staff (pharmacy_id, name, phone, role, is_active)
          values (%s,'Never Texted','254733444555','attendant',true)""", (tenant["pid"],))

    assert q1("""select id from inbound_history
                   where pharmacy_id=%s and phone='254733444555'""",
              (tenant["pid"],)) is None, "membership must not create inbound history"

    with pytest.raises(safety.GateBlocked) as e:
        safety.check_gates("254733444555", tenant["pid"], tenant["slot"])
    assert e.value.gate == "chat_established"
    ex("delete from staff where pharmacy_id=%s", (tenant["pid"],))


@db
def test_refused_messages_do_not_count_towards_the_rate_limit(tenant, monkeypatch):
    """A message the slot/JID guard refused never reached WhatsApp.

    Counting it would let the guard doing its job exhaust the pharmacy's own quota and
    silence real replies.
    """
    import safety
    from db import ex

    monkeypatch.setattr(safety.settings, "WA_RATE_LIMIT_HOUR", 3)
    for _ in range(5):
        ex("""insert into wa_messages
                (pharmacy_id, direction, to_phone, msg_type, body,
                 gowa_device_id, status, handled)
              values (%s,'out','254711222555','text','x',%s,'refused',true)""",
           (tenant["pid"], tenant["slot"]))

    assert safety.is_rate_limited(tenant["slot"]) is False
