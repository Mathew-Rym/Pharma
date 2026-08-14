"""The on-prem agent's tenant must come from its token, and must actually be bound.

Loop B had never run. `_agent()` looked the pharmacy up and discarded it -- a["pharmacy_id"]
was read nowhere -- and every endpoint then called pid(), which reads a ContextVar nothing
had set. So /agent/pos-sales, /agent/history and /agent/snapshot raised NoTenant and
returned 500 on every call. The agent's SQLite outbox retried to MAX_ATTEMPTS and parked the
batch. No POS sale, monthly history row or stock snapshot ever landed, so the variance
report and every demand forecast were reading empty tables and saying so quietly.

An audit called this "sound" on the strength of reading the enrolment handshake. The
handshake IS sound; it was simply never wired to the thing it authorises. These tests pin
the wiring, not the design.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

DB = bool(os.getenv("DATABASE_URL"))
db = pytest.mark.skipif(not DB, reason="DATABASE_URL not set")


@pytest.fixture
def two_agents():
    """Two pharmacies, each with an enrolled agent holding its own install token."""
    import secrets

    from db import ex, q1
    mark = secrets.token_hex(3)
    made = {}
    for side in ("a", "b"):
        p = q1("""insert into pharmacies (name, kind, status) values (%s,'tenant','active')
                  returning id""", (f"AGENT-{side}-{mark}",))
        pid_ = str(p["id"])
        tok = f"tok-{side}-{mark}"
        q1("""insert into agents (pharmacy_id, agent_token, enrolment_token, machine_name)
              values (%s,%s,%s,%s) returning id""",
           (pid_, tok, f"enrol-{side}-{mark}", f"PC-{side}"))
        made[side] = {"pid": pid_, "token": tok}
    yield made
    for side in ("a", "b"):
        d = made[side]
        for t in ("pos_sales", "agents", "products", "pharmacies"):
            col = "id" if t == "pharmacies" else "pharmacy_id"
            ex(f"delete from {t} where {col}=%s", (d["pid"],))


@db
def test_the_token_binds_a_tenant_at_all(two_agents):
    """The bug in one line: before the fix, pid() raised immediately after _agent()."""
    import tenancy
    from agent_api import _agent

    tenancy.clear_pharmacy()
    a = _agent(two_agents["a"]["token"])
    assert str(a["pharmacy_id"]) == two_agents["a"]["pid"]
    assert tenancy.pid() == two_agents["a"]["pid"], (
        "the pharmacy was looked up and then discarded -- pid() would raise NoTenant")


@db
def test_the_tenant_comes_from_the_token_not_the_caller(two_agents):
    """Agent A's token must bind pharmacy A even when everything else says B.

    This is the property the enrolment handshake exists to provide: a leaked token can only
    ever write to the pharmacy it was issued to. It is only true if the id is read from the
    database, which is what this asserts.
    """
    import tenancy
    from agent_api import _agent

    a, b = two_agents["a"], two_agents["b"]
    tenancy.set_pharmacy(b["pid"])                 # pretend B is somehow already bound
    _agent(a["token"])                             # A's token must win
    assert tenancy.pid() == a["pid"]


@db
def test_an_unknown_token_binds_nothing(two_agents):
    """A rejected token must not leave a previous tenant bound for whatever runs next."""
    import tenancy
    from fastapi import HTTPException

    from agent_api import _agent

    tenancy.clear_pharmacy()
    with pytest.raises(HTTPException) as e:
        _agent("not-a-real-token")
    assert e.value.status_code == 401
    with pytest.raises(tenancy.NoTenant):
        tenancy.pid()


@db
def test_pos_sales_lands_rows_against_the_agents_own_pharmacy(two_agents):
    """End to end through the ingest path that had never executed."""
    import asyncio
    import json

    from db import q1

    import agent_api

    a = two_agents["a"]

    class _Req:
        async def json(self):
            return {"rows": [{
                "external_id": "T-1", "sold_at": "2026-08-01T10:00:00Z",
                "legacy_code": "AMX", "description": "AMOXIL 500", "qty_pieces": 3,
                "unit_price": 20, "line_total": 60, "payment_method": "cash",
            }]}

    res = asyncio.run(agent_api.pos_sales(_Req(), a["token"]))
    assert res.get("landed") == 1, res
    row = q1("select pharmacy_id, external_id from pos_sales where external_id='T-1'")
    assert str(row["pharmacy_id"]) == a["pid"]

    # idempotent: the on-conflict clause has never actually run in production
    asyncio.run(agent_api.pos_sales(_Req(), a["token"]))
    assert q1("select count(*) n from pos_sales where external_id='T-1'")["n"] == 1


def test_every_agent_endpoint_is_async():
    """The binding uses set_pharmacy, which is only leak-free because each endpoint runs in
    its own asyncio Task. A plain `def` would run on a REUSED threadpool worker and carry
    the tenant into the next request on that worker -- the exact leak tenancy.py was written
    to prevent. If this fails, wrap that handler in pharmacy_scope instead.
    """
    import inspect

    import agent_api
    for name in ("pos_sales", "history", "snapshot", "heartbeat"):
        fn = getattr(agent_api, name)
        assert inspect.iscoroutinefunction(fn), (
            f"{name} is not async; set_pharmacy in _agent would leak between requests")
