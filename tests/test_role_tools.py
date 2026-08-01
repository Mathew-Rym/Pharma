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

    from reports import ROLE_TOOLS
    schema = open(os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")).read()
    m = re.search(r"role\s+text not null check \(role in \(([^)]+)\)\)", schema)
    allowed = {v.strip().strip("'") for v in m.group(1).split(",")}
    assert set(ROLE_TOOLS) == allowed


def test_every_named_tool_actually_exists():
    """A typo in the policy silently grants nothing, or silently grants everything if the
    lookup is inverted. Either way it fails quietly."""
    from reports import ROLE_TOOLS, TOOLS
    real = {t["name"] for t in TOOLS}
    for role, names in ROLE_TOOLS.items():
        assert names <= real, f"{role} names tools that do not exist: {names - real}"


def test_access_only_widens_as_rank_increases():
    """attendant ⊆ pharmacist ⊆ manager ⊆ owner. A gap would mean a promotion could take
    away a tool someone relied on -- the same silent privilege loss as the JOIN/OWNER
    demotion bug."""
    from reports import ROLE_TOOLS
    for lower, higher in (("attendant", "pharmacist"), ("pharmacist", "manager"),
                          ("manager", "owner")):
        assert ROLE_TOOLS[lower] <= ROLE_TOOLS[higher], f"{lower} has tools {higher} lacks"


def test_the_owner_can_use_everything():
    from reports import ROLE_TOOLS, TOOLS
    assert ROLE_TOOLS["owner"] == {t["name"] for t in TOOLS}


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


# ============================================================ the shortcuts
def test_the_deterministic_shortcuts_are_gated_by_the_same_policy():
    """`TODAY` called get_sales_summary with no role check and no model involved, so tool
    scoping applied to the LLM path alone would have left the cheapest bypass wide open.

    Structural, because the alternative is driving every shortcut through a live database.
    """
    src = open(os.path.join(os.path.dirname(__file__), "..", "api", "router.py")).read()
    body = src[src.index("deterministic shortcuts"):src.index("approvals with a PIN")]
    calls = body.count("run_tool(")
    guarded = body.count("_guard(")
    assert guarded >= calls, (
        f"{calls} run_tool call(s) in the shortcut block but only {guarded} guarded")


def test_the_staff_agent_gets_a_filtered_tool_list():
    src = open(os.path.join(os.path.dirname(__file__), "..", "api", "router.py")).read()
    assert "_agent_reply(phone, text, STAFF_SYSTEM, TOOLS)" not in src, (
        "the staff agent must receive tools filtered by role, not the full list")
