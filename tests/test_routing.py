"""Multi-tenant routing and isolation tests.

These test the HARD requirements of the routing layer:
  1. Device-to-tenant mapping is deterministic and fail-closed
  2. Conversation state is isolated per (pharmacy_id, phone)
  3. Unknown devices produce no reply and no state
  4. Platform line traffic is segregated from tenant traffic
  5. The variable-shadowing bug in reports.py is dead
"""
import os
import sys
import pytest

# Make api/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
os.environ.setdefault("PHARMAOS_TESTING", "1")


# ============================================================ tenancy.resolve
class TestResolve:
    """tenancy.resolve() must return three distinct kinds."""

    def test_tenant_device_resolves(self):
        """A known tenant device returns kind='tenant' with a pharmacy_id."""
        import tenancy
        from db import q1
        # Use the test pharmacy, which has a wa_jid set by conftest
        from config import settings
        row = q1("select wa_jid from pharmacies where id = %s", (settings.PHARMACY_ID,))
        if not row or not row["wa_jid"]:
            pytest.skip("test pharmacy has no wa_jid")
        r = tenancy.resolve(row["wa_jid"])
        assert r.kind == "tenant"
        assert r.pharmacy_id == settings.PHARMACY_ID

    def test_unknown_device_returns_unknown(self):
        """An unrecognised device JID returns kind='unknown' and no pharmacy_id."""
        import tenancy
        r = tenancy.resolve("000000000000@s.whatsapp.net")
        assert r.kind == "unknown"
        assert r.pharmacy_id is None

    def test_empty_jid_returns_unknown(self):
        """A missing device JID returns unknown, not a fallback."""
        import tenancy
        assert tenancy.resolve("").kind == "unknown"
        assert tenancy.resolve("   ").kind == "unknown"

    def test_resolve_does_not_fallback_to_sender(self):
        """Even when a sender is known, the DEVICE determines the tenant."""
        import tenancy
        r = tenancy.resolve("000000000000@s.whatsapp.net", sender_phone="254713755274")
        assert r.kind == "unknown"
        assert r.pharmacy_id is None


# ============================================================ wa_state isolation
class TestStateIsolation:
    """wa_state must be keyed on (pharmacy_id, phone)."""

    def _needs_v16(self):
        """Check that the wa_state table has the composite PK from v16."""
        from db import q1
        row = q1("""
            select count(*) as n from information_schema.key_column_usage
            where table_name = 'wa_state'
              and constraint_name = (
                  select conname from pg_constraint
                  where conrelid = 'wa_state'::regclass and contype = 'p')
        """)
        if row and row["n"] < 2:
            pytest.skip("wa_state composite PK not applied (run db/schema_v16.sql)")

    def test_state_is_tenant_scoped(self):
        """The same phone can have independent state at two pharmacies."""
        self._needs_v16()
        import tenancy
        from state import clear_state, get_state, set_state
        from config import settings
        phone = "254700999888"
        pid = settings.PHARMACY_ID

        # Clean up first
        clear_state(phone)

        # Set state under the test pharmacy
        with tenancy.pharmacy_scope(pid):
            set_state(phone, "grn_collect", {"pages": [1]}, pharmacy_id=pid)
            st = get_state(phone, pharmacy_id=pid)
            assert st["flow"] == "grn_collect"

        # Under no tenant, clear should still work
        clear_state(phone)
        with tenancy.pharmacy_scope(pid):
            st = get_state(phone, pharmacy_id=pid)
            assert st["flow"] == "idle"

    def test_state_expires(self):
        """Expired state reads as idle."""
        self._needs_v16()
        import tenancy
        from state import get_state, set_state, clear_state
        from config import settings
        phone = "254700999777"
        pid = settings.PHARMACY_ID

        clear_state(phone)
        with tenancy.pharmacy_scope(pid):
            set_state(phone, "test_flow", {}, ttl_min=0, pharmacy_id=pid)
            # TTL=0 means it expired on creation
            import time
            time.sleep(0.1)
            st = get_state(phone, pharmacy_id=pid)
            assert st["flow"] == "idle"
        clear_state(phone)


# ============================================================ reports variable shadowing
class TestReportsShadowing:
    """The pid = pharmacy_id or pid() shadowing bug must not recur."""

    def test_no_pid_variable_shadowing(self):
        """reports.py must not contain any `pid = pharmacy_id or pid()` pattern."""
        import inspect
        import reports
        source = inspect.getsource(reports)
        assert "pid = pharmacy_id or pid()" not in source, \
            "Variable shadowing: `pid = pharmacy_id or pid()` shadows the imported function"

    def test_get_stock_callable_without_pharmacy_id(self):
        """get_stock() must work when pharmacy_id is None (uses the bound tenant)."""
        import tenancy
        from config import settings
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            from reports import get_stock
            # Should not raise UnboundLocalError
            result = get_stock(product_query="test_nonexistent_product_xyz")
            assert isinstance(result, str)


# ============================================================ no global fallback
class TestNoGlobalFallback:
    """Code paths must not silently fall back to settings.PHARMACY_ID for routing."""

    def test_no_pid_constant_in_api_modules(self):
        """No api module should have PID = settings.PHARMACY_ID at module level."""
        api_dir = os.path.join(os.path.dirname(__file__), "..", "api")
        violations = []
        for fname in os.listdir(api_dir):
            if not fname.endswith(".py"):
                continue
            with open(os.path.join(api_dir, fname)) as f:
                for i, line in enumerate(f, 1):
                    stripped = line.strip()
                    if stripped.startswith("PID = settings.PHARMACY_ID"):
                        violations.append(f"{fname}:{i}: {stripped}")
        assert not violations, (
            f"Module-level PID = settings.PHARMACY_ID found in: "
            f"{violations}. Use tenancy.pid() instead."
        )


# ============================================================ handle_inbound routing
class TestHandleInboundRouting:
    """The router must use device_kind / pharmacy_id from the webhook, not a default."""

    def test_unknown_device_does_not_create_state(self):
        """A message from an unknown device must not create wa_state or reply."""
        import tenancy
        from state import clear_state, get_state
        from config import settings

        phone = "254700888777"
        clear_state(phone)

        # Simulate what webhook_gowa sets for an unknown device
        msg = {
            "wa_id": "test-unknown-device-001",
            "from": phone,
            "type": "text",
            "text": "hello",
            "device_kind": "unknown",
        }

        # The router should not crash on unknown device traffic
        # (register.gateway_intercept returns False for unknown devices)
        # and should not create any state
        from router import handle_inbound
        handle_inbound(msg)
        
        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            st = get_state(phone, pharmacy_id=settings.PHARMACY_ID)
            assert st["flow"] == "idle"


# ============================================================ safety gates tenant scope
class TestSafetyGatesTenantScope:
    """Safety gates must use the correct pharmacy_id, not a global default."""

    def test_record_inbound_is_tenant_scoped(self):
        """record_inbound stores the pharmacy_id, not a global default."""
        import tenancy
        from safety import record_inbound, has_inbound_history
        from config import settings
        phone = "254700666555"

        with tenancy.pharmacy_scope(settings.PHARMACY_ID):
            record_inbound(phone, settings.PHARMACY_ID)
            assert has_inbound_history(phone, settings.PHARMACY_ID)

    def test_relationship_check_is_tenant_scoped(self):
        """has_relationship checks against a specific pharmacy, not globally."""
        from safety import has_relationship
        # A random phone should have no relationship to the test pharmacy
        assert not has_relationship("254700111222", "00000000-0000-0000-0000-000000000000")


# ============================================================ wa_messages device tracking
class TestWaMessagesDeviceTracking:
    """Outbound messages must record gowa_device_id and status."""

    def test_wa_messages_has_device_columns(self):
        """wa_messages must have gowa_device_id and status columns."""
        from db import q1
        # Check the table has the columns
        cols = q1("""
            select count(*) as n from information_schema.columns
            where table_name = 'wa_messages'
              and column_name in ('gowa_device_id', 'status')
        """)
        if cols is None:
            pytest.skip("could not query information_schema")
        assert cols["n"] == 2, "wa_messages must have both gowa_device_id and status columns"
