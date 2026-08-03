"""Shared test setup.

Two jobs.

**Bind a tenant.** Production binds one per unit of work: the router does it after
resolving an inbound message, and the jobs loop once per pharmacy. Tests go through
neither, so without this they call tenant-scoped code with nothing bound and raise
NoTenant -- `pid()` behaving exactly as designed, and not what those tests are checking. A
test specifically about the absence of a tenant clears it explicitly (see test_tenancy.py);
nesting works, so a test that binds its own pharmacy just shadows this one.

**Own the pharmacy it binds.** That used to be `settings.PHARMACY_ID`, which on this
machine names a REAL pharmacy holding real products, batches and stock_movements from a
client discovery session. Every full run wrote into it, and cleaning up afterwards was
manual and imperfect: one leaked suppliers row made resolve_by_sender return a pharmacy for
a phone number belonging to nobody. Tests now create their own and delete it.

Creation happens at MODULE IMPORT rather than in a fixture, deliberately. `config` builds
its `settings` object at import time, so `settings.PHARMACY_ID` is fixed the moment any
test module imports anything from `api/`. A session fixture runs after collection -- too
late. Setting os.environ here, before pytest imports a single test module, means both
`settings.PHARMACY_ID` and the tests that read `os.environ["PHARMACY_ID"]` directly see the
throwaway pharmacy, with no change needed in any of them.

Which is also why this file uses raw psycopg instead of `api.db`: importing db imports
config, freezing PHARMACY_ID before we have set it.

An explicit PHARMACY_ID in the environment always wins, so pointing a run at a chosen
pharmacy on purpose still works.
"""
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

_OWNED_PHARMACY: str | None = None
_MARK = secrets.token_hex(4)


def _create_throwaway() -> str | None:
    """Make a pharmacy for this run and return its id, or None if there is no database."""
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg
    except ImportError:
        return None
    try:
        # prepare_threshold=None: DATABASE_URL may be Supabase's transaction pooler (6543),
        # where prepared statements are unsupported. Same reason api/db.py sets it.
        with psycopg.connect(dsn, connect_timeout=20, prepare_threshold=None) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """insert into pharmacies
                         (name, kind, status, wa_number, wa_jid, gowa_device_id, timezone)
                       values (%s,'tenant','active',%s,%s,%s,'Africa/Nairobi')
                       returning id""",
                    (f"PYTEST-{_MARK}", f"254700{_MARK[:6]}",
                     # A unique but fake JID and slot, so the row satisfies tenancy.LIVE_SQL
                     # and wa.compose() will build messages for it. Nothing is ever sent: no
                     # GOWA slot by this name exists, so deliver() refuses -- the correct
                     # outcome for a test pharmacy, and one less way for a test to reach
                     # WhatsApp by accident.
                     f"254700{_MARK[:6]}@s.whatsapp.net", f"pytest-{_MARK}"))
                pid = str(cur.fetchone()[0])

                # A staff member, because several tests do
                #     select * from staff where pharmacy_id = %s limit 1
                # and then dereference the result -- test_v2's supplier-link test and three
                # in test_vision_count all call grn.approve(grn_id, staff, staff["phone"]).
                # Against the old shared pharmacy that row happened to exist, which is why
                # this was never noticed; it is also the likeliest explanation for the
                # intermittent 'NoneType' object is not subscriptable seen in full runs,
                # where something transiently removed it. A test pharmacy with no staff is
                # not a realistic pharmacy, so it gets one.
                #
                # Role 'owner': the highest rank, so role-scoped tool access (357ec44) never
                # denies a test that is not about permissions.
                cur.execute(
                    """insert into staff (pharmacy_id, name, phone, role, is_active)
                       values (%s,%s,%s,'owner',true)""",
                    (pid, f"PYTEST Owner {_MARK[:4]}", f"254799{_MARK[:6]}"))
            conn.commit()
        return pid
    except Exception as e:                     # unreachable DB -> the DB tests skip anyway
        print(f"conftest: could not create a test pharmacy ({e}); "
              f"database tests will skip", file=sys.stderr)
        return None


if not os.getenv("PHARMACY_ID"):
    _OWNED_PHARMACY = _create_throwaway()
    if _OWNED_PHARMACY:
        os.environ["PHARMACY_ID"] = _OWNED_PHARMACY


# ---------------------------------------------------------------- external dependencies
#
# Some tests need a real Supabase project (signed URLs, storage buckets, Auth) or a running
# GOWA. Against pharma-test-pg with placeholder Supabase credentials they fail for reasons
# that say nothing about the code. Skipping them EXPLICITLY, with a reason, is honest;
# leaving them failing trains everyone to ignore red, which is how the standing
# "one pre-existing failure" habit started.
_PLACEHOLDER_SUPABASE = (os.getenv("SUPABASE_URL") or "").startswith("https://test.")


def _gowa_reachable() -> bool:
    try:
        import httpx
        base = (os.getenv("GOWA_URL") or "http://localhost:3001").rstrip("/")
        httpx.get(f"{base}/app/info", timeout=2,
                  auth=(os.getenv("GOWA_USER") or "pharmaos", os.getenv("GOWA_PASS") or ""))
        return True
    except Exception:
        return False


needs_supabase = pytest.mark.skipif(
    _PLACEHOLDER_SUPABASE,
    reason="needs a real Supabase project (storage buckets / Auth), not placeholder creds")

needs_gowa = pytest.mark.skipif(
    not _gowa_reachable(),
    reason="needs a running GOWA gateway; this test deliberately uses no mock")


def _teardown(pid: str) -> None:
    """Delete the pharmacy and everything hanging off it, in FK order.

    The order is not cosmetic. Hand-cleaning one leaked supplier previously hit two foreign
    keys in sequence -- products.preferred_supplier_id, then grns.supplier_id -- because
    suppliers is referenced from tables that do not look related to it. So break those
    references first, then delete children before parents.

    staff, customers and wa_state declare ON DELETE CASCADE and would go with the pharmacy
    anyway. They are listed explicitly because relying on a cascade nobody has verified is
    how residue survives.
    """
    import psycopg
    stmts = [
        # break references INTO suppliers before deleting suppliers
        "update products set preferred_supplier_id = null where pharmacy_id = %s",
        "delete from grn_lines where grn_id in (select id from grns where pharmacy_id = %s)",
        "delete from grns where pharmacy_id = %s",
        "delete from stock_movements where pharmacy_id = %s",
        "delete from batches where pharmacy_id = %s",
        "delete from products where pharmacy_id = %s",
        "delete from suppliers where pharmacy_id = %s",
        "delete from staff_role_changes where pharmacy_id = %s",
        "delete from onboarding_contacts where pharmacy_id = %s",
        "delete from inbound_history where pharmacy_id = %s",
        "delete from wa_messages where pharmacy_id = %s",
        "delete from wa_state where pharmacy_id = %s",
        "delete from customers where pharmacy_id = %s",
        "delete from staff where pharmacy_id = %s",
        "delete from pharmacies where id = %s",
    ]
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=20,
                         prepare_threshold=None) as conn:
        for s in stmts:
            try:
                with conn.cursor() as cur:
                    cur.execute(s, (pid,))
                conn.commit()
            except Exception as e:
                # Keep going. A table absent in this schema version, or a row already gone,
                # must not strand the pharmacy row itself -- that would leave exactly the
                # residue this fixture exists to prevent.
                conn.rollback()
                print(f"conftest teardown: {' '.join(s.split()[:3])} -> {e}",
                      file=sys.stderr)


@pytest.fixture(scope="session", autouse=True)
def _own_test_pharmacy():
    yield
    if _OWNED_PHARMACY:
        _teardown(_OWNED_PHARMACY)


@pytest.fixture(autouse=True)
def _default_tenant():
    try:
        import tenancy
        from config import settings
    except Exception:                      # config unavailable (no .env) -> nothing to bind
        yield
        return
    with tenancy.pharmacy_scope(settings.PHARMACY_ID):
        yield
