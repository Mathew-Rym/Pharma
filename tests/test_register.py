"""Self-service registration over WhatsApp.

The whole point of this flow is that a pharmacy owner who has never met us can text a
number and end up with a working bot. That makes it the one code path where an unknown
phone gets a reply, so it is also the easiest place to accidentally undo the anti-ban
work: every gate exists to stop us messaging strangers, and this flow deliberately talks
to one.

The conversation itself is a pure function -- `_step()` takes a flow, a message and the
answers so far, and returns the next flow, the updated answers and what to say. No
database, no network, no LLM. That is a deliberate design choice and these tests are what
hold it in place: registration must keep working when Gemini is down, because a pharmacy
that cannot be created is a pharmacy that cannot pay us.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

SRC = os.path.join(os.path.dirname(__file__), "..", "api", "register.py")


# ============================================================ what counts as registration
@pytest.mark.parametrize("text,expected", [
    ("REGISTER", True),
    ("register", True),
    ("  Register  ", True),
    ("register my pharmacy", True),
    ("REGISTER.", True),
    ("I registered yesterday", False),      # not the first word
    ("registered", False),                  # a different word that merely starts the same
    ("deregister", False),
    ("", False),
])
def test_register_is_recognised_by_first_word_only(text, expected):
    """`startswith` would match "registered", which is an ordinary English word a customer
    can plausibly send to a pharmacy ("registered nurse", "I registered yesterday"). That
    would hijack their message into an onboarding flow they never asked for."""
    import register
    assert register.wants_registration(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("JOIN ABCD2345", ("join", "ABCD2345")),
    ("join abcd2345", ("join", "ABCD2345")),
    ("OWNER ABCD2345", ("owner", "ABCD2345")),
    ("  owner   abcd2345  ", ("owner", "ABCD2345")),
    ("JOIN", None),                         # no code -> not a redemption
    ("JOIN the queue", None),               # not a code shape
    ("owner of the shop", None),
])
def test_join_and_owner_need_a_code_shaped_argument(text, expected):
    import register
    assert register.parse_redemption(text) == expected


# ============================================================ the codes we hand out
def test_codes_avoid_characters_people_misread():
    """These get read aloud down a phone line and typed on a handset. 0/O and 1/I/L are
    the classic transcription failures, and a wrong code is indistinguishable from a
    broken system to the person typing it."""
    import register
    for _ in range(200):
        code = register.new_code()
        assert len(code) == 8
        assert re.fullmatch(r"[A-Z0-9]{8}", code), code
        assert not (set(code) & set("O0I1L")), code


def test_codes_are_not_all_the_same():
    import register
    assert len({register.new_code() for _ in range(50)}) > 40


# ============================================================ the conversation
def _walk(answers):
    """Drive _step() from the top, feeding each answer in turn. Returns every reply."""
    import register
    flow, ctx, replies = register.START, {}, []
    for a in answers:
        flow, ctx, reply = register._step(flow, a, ctx)
        replies.append(reply)
    return flow, ctx, replies


GOOD = ["Jane Wambui", "Greenline Pharmacy", "PPB/12345/2024", "Kisumu", "0712345678"]


def test_a_clean_run_reaches_confirmation_with_every_answer_kept():
    flow, ctx, _ = _walk(GOOD)
    assert flow == "reg_confirm"
    assert ctx["owner_name"] == "Jane Wambui"
    assert ctx["name"] == "Greenline Pharmacy"
    assert ctx["licence"] == "PPB/12345/2024"
    assert ctx["town"] == "Kisumu"
    assert ctx["wa"] == "254712345678", "the handset number must be stored normalised"


def test_confirmation_repeats_everything_before_anything_is_created():
    """The owner is about to have a pharmacy created in their name and a code sent to a
    handset. If the summary omits a field, a typo in it is only discovered later, by which
    point the pharmacy row exists and the licence number is wrong in our records."""
    _, _, replies = _walk(GOOD)
    summary = replies[-1]
    for value in ("Jane Wambui", "Greenline Pharmacy", "PPB/12345/2024", "Kisumu"):
        assert value in summary, value
    assert "712345678" in summary


def test_yes_at_the_end_asks_the_caller_to_provision():
    import register
    flow, ctx, _ = _walk(GOOD)
    nxt, ctx2, _ = register._step(flow, "YES", ctx)
    assert nxt == register.PROVISION
    assert ctx2 == ctx, "provisioning must not silently rewrite the confirmed answers"


def test_edit_restarts_rather_than_guessing_which_field_was_wrong():
    import register
    flow, ctx, _ = _walk(GOOD)
    nxt, ctx2, reply = register._step(flow, "EDIT", ctx)
    assert nxt == register.START
    assert ctx2 == {}
    assert reply


def test_an_unclear_answer_at_confirmation_does_not_create_anything():
    import register
    flow, ctx, _ = _walk(GOOD)
    nxt, _, reply = register._step(flow, "maybe", ctx)
    assert nxt == "reg_confirm", "must re-ask, never fall through to creating"
    assert "YES" in reply


# ============================================================ rejecting bad answers
@pytest.mark.parametrize("flow,bad", [
    ("reg_owner_name", "J"),
    ("reg_name", "Rx"),
    ("reg_licence", "12"),
    ("reg_town", "K"),
    ("reg_wa", "0712"),
    ("reg_wa", "not a phone"),
    ("reg_wa", "254613755274"),        # 06xx is a landline range, not a mobile
])
def test_a_bad_answer_re_asks_the_same_question(flow, bad):
    """Advancing on a bad answer is the expensive failure: a mistyped handset number sends
    the pairing code to a stranger, who is then invited to link a device to our system."""
    import register
    nxt, ctx, reply = register._step(flow, bad, {})
    assert nxt == flow, f"{flow} advanced on {bad!r}"
    assert ctx == {}
    assert reply


def test_the_pharmacy_name_is_not_silently_truncated_into_something_wrong():
    import register
    nxt, _, _ = register._step("reg_name", "A" * 400, {})
    assert nxt == "reg_name", "an absurd name is a mis-send, not a name to trim"


# ============================================================ the anti-ban boundary
def test_the_pharmacy_handset_is_never_the_senders_own_number():
    """A pharmacy's wa_jid is the handset the bot runs on. Binding it to the owner's
    personal phone would put every customer conversation into their private WhatsApp and
    make a ban take out their own messaging. The flow asks for the handset explicitly, so
    the sender's number must never be reused as the answer."""
    src = open(SRC).read()
    assert not re.search(r"wa_jid\s*=\s*[^%\s]*phone", src), \
        "wa_jid must come from GOWA after pairing, never from the sender"


def test_a_new_pharmacy_starts_unpaired_and_pending():
    """Created is not the same as live. A pharmacy with no session must be distinguishable
    from a broken one, or every unpaired tenant looks like an outage."""
    import register
    cols = register._new_pharmacy_columns({
        "owner_name": "Jane", "name": "Greenline", "licence": "PPB/1/2",
        "town": "Kisumu", "wa": "254712345678"}, owner_phone="254700000001")
    assert cols["status"] == "pending_activation"
    assert cols["kind"] == "tenant"
    assert "wa_jid" not in cols, "the JID is unknown until the handset pairs"
    assert cols["owner_phone"] == "254700000001"
    assert cols["wa_number"] == "254712345678"


def test_onboarding_contacts_are_not_customers():
    """Found by driving the real flow, and cheap to reintroduce.

    Replying to a stranger needs a Gate 2 relationship, and a `customers` row is the
    obvious way to grant one. It is wrong: tenancy.resolve_by_sender() reads customers, so
    the registering owner stays pinned to the answering pharmacy forever and every later
    message of theirs is met with "you're registered at more than one pharmacy, which
    one?" -- and a real pharmacy's customer list fills up with people who were registering
    somewhere else entirely.
    """
    src = open(SRC).read()
    assert "into customers" not in src, (
        "grant Gate 2 access through onboarding_contacts; a customers row is permanent "
        "and resolve_by_sender() reads it")
    assert "onboarding_contacts" in src


def test_gate_two_scopes_onboarding_access_to_one_pharmacy_and_expires_it():
    """An open-ended, pharmacy-agnostic exception is not a gate. An earlier draft added
    `select 1 from wa_state where phone = %s` -- no pharmacy filter, no expiry -- which
    made a single stale row enough to message that number from every tenant."""
    safety = open(os.path.join(os.path.dirname(__file__), "..", "api",
                               "safety.py")).read()
    clause = re.search(r"from onboarding_contacts(.|\n)*?\"\"\"", safety)
    assert clause, "Gate 2 does not consult onboarding_contacts"
    assert "pharmacy_id = %s" in clause.group(0)
    assert "created_at >" in clause.group(0)


def test_registration_does_not_depend_on_the_model():
    """Gemini answering slowly must not be able to stop a pharmacy signing up."""
    src = open(SRC).read()
    assert not re.search(r"^(from llm import|import llm)", src, re.M)
