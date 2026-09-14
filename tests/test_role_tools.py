"""Which staff role may run which tool.

The customer/staff boundary was enforced -- CUSTOMER_TOOLS is filtered to get_stock. Within
staff it was not: router passed the whole TOOLS list to every role, so an attendant could
ask "how did we do today" and get the day's takings. The deterministic shortcuts were worse,
because they never involved a model at all: `TODAY` called get_sales_summary directly with
no check of any kind.

Two things these tests deliberately pin down:

  * denial is by ROLE, and says so. Pretending a tool does not exist teaches staff the
    system is broken; naming the role needed teaches them who to ask.
  * the shortcuts and the LLM path use the SAME policy. Two lists that must agree is how
    the customer/staff split stayed correct while the staff split silently did not.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

ROLES = ("attendant", "pharmacist", "manager", "owner")


def test_every_database_role_has_a_tool_policy():
    """A role with no entry would fall through to whatever the default is. Given the
    default used to be "all tools", an unmapped role is an open door."""
    import re

    from reports import ROLE_CAPS
    schema = open(os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")).read()
    m = re.search(r"role\s+text not null check \(role in \(([^)]+)\)\)", schema)
    allowed = {v.strip().strip("'") for v in m.group(1).split(",")}
    assert set(ROLE_CAPS) == allowed


def test_every_named_tool_actually_exists():
    """A typo in the policy silently grants nothing, or silently grants everything if the
    lookup is inverted. Either way it fails quietly."""
    from reports import _EXTRA_CAPS, ROLE_CAPS, TOOLS
    real = {t["name"] for t in TOOLS} | _EXTRA_CAPS
    for role, names in ROLE_CAPS.items():
        assert names <= real, f"{role} names capabilities that do not exist: {names - real}"


def test_access_only_widens_as_rank_increases():
    """attendant ⊆ pharmacist ⊆ manager ⊆ owner. A gap would mean a promotion could take
    away a tool someone relied on -- the same silent privilege loss as the JOIN/OWNER
    demotion bug."""
    from reports import ROLE_CAPS
    for lower, higher in (("attendant", "pharmacist"), ("pharmacist", "manager"),
                          ("manager", "owner")):
        assert ROLE_CAPS[lower] <= ROLE_CAPS[higher], f"{lower} has tools {higher} lacks"


def test_the_owner_can_use_everything():
    from reports import _EXTRA_CAPS, ROLE_CAPS, TOOLS
    assert ROLE_CAPS["owner"] == {t["name"] for t in TOOLS} | _EXTRA_CAPS


@pytest.mark.parametrize("role,tool,allowed", [
    ("attendant",  "get_stock",               True),
    ("attendant",  "get_sales_summary",       False),   # the day's takings
    ("attendant",  "generate_report_pdf",     False),
    ("attendant",  "get_expiry_risk",         False),
    ("pharmacist", "get_expiry_risk",         True),
    ("pharmacist", "get_sales_summary",       False),
    ("manager",    "get_sales_summary",       True),
    ("manager",    "get_reorder_suggestions", True),
    ("owner",      "get_sales_summary",       True),
])
def test_may_use(role, tool, allowed):
    from reports import may_use
    assert may_use(role, tool) is allowed


def test_an_unknown_role_is_denied_rather_than_defaulted():
    """Fail closed. A role that is not in the policy -- a typo, a new role added to the
    constraint but not here -- must not inherit the old "everything" default."""
    from reports import may_use
    assert may_use("regional_director", "get_sales_summary") is False
    assert may_use(None, "get_stock") is False


def test_denial_names_the_role_needed():
    from reports import denial_message
    msg = denial_message("attendant", "get_sales_summary")
    assert "manager" in msg.lower(), f"does not say who can: {msg!r}"
    assert "attendant" in msg.lower()


# ============================================================ the keyword commands
def test_every_advertised_command_has_a_policy():
    """Counting run_tool call sites was NOT enough, and gave false confidence.

    The first version of this test compared run_tool calls against guarded calls inside the
    shortcut block. It passed while PO, VARIANCE, WHY, SYNC, PC and PROBE sat completely
    ungated, because none of them go through run_tool -- they call create_draft_pos(),
    reconciliation_summary(), queue_command() and so on directly. PO drafts purchase orders
    and routes them for approval; an attendant could run it.

    So the assertion is now on the enumerated COMMANDS, not on how they happen to be
    implemented. Adding a command to the help without deciding who may run it fails here.
    """
    from reports import ROLE_CAPS, STAFF_COMMANDS
    for cmd, cap in STAFF_COMMANDS.items():
        holders = [r for r, caps in ROLE_CAPS.items() if cap in caps]
        assert holders, f"{cmd} maps to capability {cap!r} that no role has"


def test_the_help_text_and_the_command_policy_describe_the_same_system():
    """_staff_help used to take `role` and ignore it, so every role was shown commands it
    would be refused. That disagreement is how PO stayed ungated behind a help entry
    nobody cross-checked."""
    from reports import ROLE_CAPS, STAFF_COMMANDS
    from router import _HELP_LINES

    helped = {cap for cap, _ in _HELP_LINES}
    known = set().union(*ROLE_CAPS.values())
    # Not an equality: the help also shows natural-language examples ("who supplies
    # prenor") that reach a tool through the model rather than through a keyword branch.
    # Both must name a REAL capability, and every keyword command must be advertised --
    # an unadvertised command is one nobody cross-checks, which is how PO stayed open.
    assert helped <= known, f"help advertises capabilities that do not exist: {helped - known}"
    policed = set(STAFF_COMMANDS.values())
    assert policed <= helped, f"keyword commands missing from the help: {policed - helped}"


def test_every_keyword_branch_in_the_staff_handler_is_gated():
    """Structural backstop over the source, so a new `if up == "X"` that calls a function
    directly -- the exact shape that slipped through -- cannot land unguarded."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "api", "router.py")).read()
    body = src[src.index("deterministic shortcuts"):src.index("everything else: let the model")]
    import re
    # Each branch that reacts to a keyword must mention may_use or _guard before the next.
    branches = re.split(r"(?m)^    if up[ .]", body)[1:]
    ungated = [b.splitlines()[0].strip()[:48] for b in branches
               if "_guard(" not in b and "may_use(" not in b]
    assert not ungated, f"keyword branches with no role check: {ungated}"


def test_the_help_shown_to_an_attendant_omits_what_they_cannot_run():
    from router import _staff_help
    att, mgr = _staff_help("attendant"), _staff_help("manager")
    assert "*PO*" not in att and "*PO*" in mgr
    assert "*TODAY*" not in att and "*TODAY*" in mgr
    assert "*VARIANCE*" not in att and "*VARIANCE*" in mgr
    assert "*LOW*" in att, "an attendant must still be told what they CAN do"
    assert "who supplies" in att, "supplier contact is attendant-level on purpose"


def test_the_staff_agent_gets_a_filtered_tool_list():
    src = open(os.path.join(os.path.dirname(__file__), "..", "api", "router.py")).read()
    assert "_agent_reply(phone, text, STAFF_SYSTEM, TOOLS)" not in src, (
        "the staff agent must receive tools filtered by role, not the full list")
