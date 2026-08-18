"""Platform gateway security tests.

Covers register.gateway_intercept() and its integration with router._greet_unknown().
All tests run without a database; every external call is monkeypatched.

Security invariants enforced here:

  1. Unknown sender + platform device   -> onboarding welcome, state created
  2. Unknown sender + tenant device     -> "contact admin", no onboarding
  3. Unknown sender + unknown device    -> no reply, fail closed
  4. Known Tenant A staff + platform    -> NOT treated as stranger; no new onboarding
  5. Tenant A staff + Tenant B device   -> Tenant B must not become their tenant
  6. Idempotency: duplicate wa_id       -> no second welcome
  7. device_kind='tenant' without pid   -> fail closed
  8. No platform row                    -> fail closed

The tests that verify cross-tenant routing (4 and 5) inspect the routing path rather
than making DB calls, consistent with the rest of this test file.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

PLATFORM_ID = "platform-pharmacy-uuid-0001"
TENANT_A_ID = "tenant-pharmacy-uuid-a001"
TENANT_B_ID = "tenant-pharmacy-uuid-b002"
STRANGER    = "254117283959"
TENANT_A_STAFF = "254720111222"


# ------------------------------------------------------------------ helpers
def _msg(text="Hello", wa_id="TEST-WA-001", device_kind="platform",
         pharmacy_id=None):
    """Build a minimal inbound dict as produced by main.webhook_gowa."""
    m = {
        "from":        STRANGER,
        "text":        text,
        "type":        "text",
        "wa_id":       wa_id,
        "device_kind": device_kind,
    }
    if pharmacy_id is not None:
        m["pharmacy_id"] = pharmacy_id
    return m


def _tenant_msg(**kw):
    kw.setdefault("device_kind", "tenant")
    kw.setdefault("pharmacy_id", TENANT_A_ID)
    return _msg(**kw)


def _unknown_device_msg(**kw):
    kw.setdefault("device_kind", "unknown")
    return _msg(**kw)


# ============================================================ Test 1 — platform welcome
class TestPlatformLine:
    """Unknown sender texting the master/platform number receives the onboarding welcome."""

    def test_welcome_is_sent(self, monkeypatch):
        import register
        says = []
        monkeypatch.setattr(register, "platform_pid",       lambda: PLATFORM_ID)
        monkeypatch.setattr(register, "q1",                 lambda *a, **kw: None)
        monkeypatch.setattr(register, "_log_inbound",       lambda *a: None)
        monkeypatch.setattr(register, "_make_contactable",  lambda *a: None)
        monkeypatch.setattr(register, "_say",
                            lambda phone, body, pid: says.append((phone, body, pid)))
        monkeypatch.setattr(register, "set_state",          lambda *a, **kw: None)
        monkeypatch.setattr("register._tenancy",
                            type("T", (), {"resolve_by_sender": staticmethod(lambda p: [])})(),
                            raising=False)

        import tenancy as _tenancy
        monkeypatch.setattr(_tenancy, "resolve_by_sender", lambda p: [])

        result = register.gateway_intercept(STRANGER, _msg())

        assert result is True
        assert says, "a reply must be sent"
        phone, body, pid = says[0]
        assert phone == STRANGER
        assert pid   == PLATFORM_ID
        assert "REGISTER" in body

    def test_state_set_to_start(self, monkeypatch):
        import register
        captured = []
        monkeypatch.setattr(register, "platform_pid",      lambda: PLATFORM_ID)
        monkeypatch.setattr(register, "q1",                lambda *a, **kw: None)
        monkeypatch.setattr(register, "_log_inbound",      lambda *a: None)
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",              lambda *a: None)
        monkeypatch.setattr(register, "set_state",
                            lambda ph, flow, ctx, **kw: captured.append(flow))

        import tenancy as _tenancy
        monkeypatch.setattr(_tenancy, "resolve_by_sender", lambda p: [])

        register.gateway_intercept(STRANGER, _msg())

        assert captured == [register.START], (
            "state must be set to START so the next message enters intercept() mid-flow")

    def test_sender_seeded_in_context(self, monkeypatch):
        """ctx['sender'] is what the own-number check compares against."""
        import register
        captured_ctx = []
        monkeypatch.setattr(register, "platform_pid",      lambda: PLATFORM_ID)
        monkeypatch.setattr(register, "q1",                lambda *a, **kw: None)
        monkeypatch.setattr(register, "_log_inbound",      lambda *a: None)
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",              lambda *a: None)
        monkeypatch.setattr(register, "set_state",
                            lambda ph, fl, ctx, **kw: captured_ctx.append(ctx))

        import tenancy as _tenancy
        monkeypatch.setattr(_tenancy, "resolve_by_sender", lambda p: [])

        register.gateway_intercept(STRANGER, _msg())

        assert captured_ctx[0].get("sender") == STRANGER

    def test_onboarding_contacts_not_customers(self, monkeypatch):
        """Gate 2 must be opened via onboarding_contacts, never via customers."""
        import register, inspect
        src = inspect.getsource(register.gateway_intercept)
        assert "into customers" not in src, (
            "gateway_intercept must not insert into customers; "
            "use _make_contactable() which writes to onboarding_contacts")
        assert "_make_contactable" in src


# ============================================================ Test 2 — tenant device
class TestTenantDevice:
    """Unknown sender texting a tenant's WhatsApp number receives 'contact admin'."""

    def test_contact_admin_sent_from_tenant_device(self, monkeypatch):
        import register
        says = []
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",
                            lambda phone, body, pid: says.append((phone, body, pid)))

        result = register.gateway_intercept(STRANGER, _tenant_msg())

        assert result is True
        assert says
        phone, body, pid = says[0]
        assert pid  == TENANT_A_ID, "reply must leave by the tenant device, not platform"
        assert "administrator" in body.lower() or "admin" in body.lower()

    def test_no_onboarding_state_created(self, monkeypatch):
        import register
        states = []
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",              lambda *a: None)
        monkeypatch.setattr(register, "set_state",
                            lambda *a, **kw: states.append(a))

        register.gateway_intercept(STRANGER, _tenant_msg())

        assert not states, "no onboarding state must be created for a tenant-device message"

    def test_no_pharmacy_created(self, monkeypatch):
        """platform_pid() must never be called for a tenant-device message."""
        import register
        platform_calls = []
        monkeypatch.setattr(register, "platform_pid",
                            lambda: platform_calls.append(1) or PLATFORM_ID)
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",              lambda *a: None)

        register.gateway_intercept(STRANGER, _tenant_msg())

        assert not platform_calls, "platform_pid must not be consulted for a tenant device"

    def test_missing_pharmacy_id_fails_closed(self, monkeypatch):
        """device_kind='tenant' with no pharmacy_id is a bug; must fail closed."""
        import register

        result = register.gateway_intercept(
            STRANGER, _msg(device_kind="tenant"))   # no pharmacy_id

        assert result is False


# ============================================================ Test 3 — unknown device
class TestUnknownDevice:
    """Messages from a completely unrecognised device must produce no reply."""

    def test_returns_false(self, monkeypatch):
        import register
        result = register.gateway_intercept(STRANGER, _unknown_device_msg())
        assert result is False

    def test_no_welcome_sent(self, monkeypatch):
        import register
        says = []
        monkeypatch.setattr(register, "_say",
                            lambda *a: says.append(a))
        register.gateway_intercept(STRANGER, _unknown_device_msg())
        assert not says

    def test_no_onboarding_state(self, monkeypatch):
        import register
        states = []
        monkeypatch.setattr(register, "set_state",
                            lambda *a, **kw: states.append(a))
        register.gateway_intercept(STRANGER, _unknown_device_msg())
        assert not states

    def test_no_contactable_record(self, monkeypatch):
        import register
        contacts = []
        monkeypatch.setattr(register, "_make_contactable",
                            lambda *a: contacts.append(a))
        register.gateway_intercept(STRANGER, _unknown_device_msg())
        assert not contacts

    def test_platform_pid_never_called(self, monkeypatch):
        """Unknown device must not reach the platform pharmacy at all."""
        import register
        calls = []
        monkeypatch.setattr(register, "platform_pid",
                            lambda: calls.append(1) or PLATFORM_ID)
        register.gateway_intercept(STRANGER, _unknown_device_msg())
        assert not calls

    def test_old_pharmacy_id_presence_bug_cannot_return(self, monkeypatch):
        """Regression guard.

        The old implementation branched on `msg.get('pharmacy_id')` being absent to
        identify the 'platform' case.  An unknown device also has no pharmacy_id, so
        it would have silently started onboarding.  Verify the NEW code branches on
        device_kind, not pharmacy_id presence.
        """
        import register
        import inspect
        src = inspect.getsource(register.gateway_intercept)
        # The function must key on device_kind, not pharmacy_id alone
        assert 'device_kind' in src
        # It must not treat the absence of pharmacy_id as sufficient evidence of platform
        assert 'device_kind == "platform"' in src or "device_kind != \"platform\"" in src


# ============================================================ Test 4 — known staff + platform
class TestKnownStaffOnPlatformLine:
    """A staff member belonging to Tenant A texting the platform number must NOT be
    treated as a new unknown sender.

    The router's sender-resolution path (resolve_by_sender) should catch this before
    _greet_unknown() is called.  These tests verify the gateway's own defence: if the
    sender somehow reaches gateway_intercept() with existing tenant relationships, it
    must refuse to open onboarding.
    """

    def test_not_treated_as_stranger(self, monkeypatch):
        import register, tenancy
        # Simulate: this phone already belongs to Tenant A
        monkeypatch.setattr(tenancy, "resolve_by_sender",
                            lambda p: [TENANT_A_ID])
        monkeypatch.setattr(register, "platform_pid",  lambda: PLATFORM_ID)
        monkeypatch.setattr(register, "q1",            lambda *a, **kw: None)

        says = []
        states = []
        monkeypatch.setattr(register, "_say",      lambda *a: says.append(a))
        monkeypatch.setattr(register, "set_state", lambda *a, **kw: states.append(a))

        result = register.gateway_intercept(
            TENANT_A_STAFF, _msg(device_kind="platform"))

        assert result is False, (
            "gateway must return False for a sender already known at a tenant, "
            "allowing the router's sender-resolution path to handle them")
        assert not states, "no onboarding state must be created for a known staff member"
        assert not says,   "no welcome message must be sent to a known staff member"

    def test_tenant_identity_not_replaced_by_platform(self, monkeypatch):
        """The invariant: sender tenant != platform tenant after gateway runs."""
        import register, tenancy
        monkeypatch.setattr(tenancy, "resolve_by_sender",
                            lambda p: [TENANT_A_ID])
        monkeypatch.setattr(register, "platform_pid",      lambda: PLATFORM_ID)
        monkeypatch.setattr(register, "q1",                lambda *a, **kw: None)
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",              lambda *a: None)
        monkeypatch.setattr(register, "set_state",         lambda *a, **kw: None)

        # gateway_intercept must not have set_state with platform_id for this sender
        captured_pids = []
        monkeypatch.setattr(register, "set_state",
                            lambda ph, fl, ctx, pharmacy_id=None, **kw:
                            captured_pids.append(pharmacy_id))

        register.gateway_intercept(TENANT_A_STAFF, _msg(device_kind="platform"))

        for pid in captured_pids:
            assert pid != PLATFORM_ID, (
                f"platform ID {PLATFORM_ID} must never be assigned as the tenant "
                f"context for a sender already known at {TENANT_A_ID}")


# ============================================================ Test 5 — Tenant A staff + Tenant B device
class TestCrossTenantDeviceIsolation:
    """Staff A (belonging to Tenant A) messaging through Tenant B's WhatsApp number.

    The receiving device identifies the tenant context for the inbound session.
    Staff A is not registered with Tenant B, so they are 'unknown' to Tenant B and
    should receive the 'contact administrator' response — the same as any stranger.
    They must NOT gain access to Tenant B's inventory or data.

    This test verifies gateway_intercept's tenant-device branch does not create
    a staff row or establish membership.  The router's normal path is NOT involved here
    because the sender resolves to zero relationships on Tenant B.
    """

    def test_tenant_b_does_not_become_staff_a_tenant(self, monkeypatch):
        import register
        states = []
        says   = []

        # Staff A texting Tenant B's device: device_kind='tenant', pharmacy_id=TENANT_B
        # Staff A has no relationship with Tenant B, so they reach gateway_intercept.
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",
                            lambda ph, body, pid: says.append(pid))
        monkeypatch.setattr(register, "set_state",
                            lambda *a, **kw: states.append(a))

        result = register.gateway_intercept(
            TENANT_A_STAFF,
            _msg(device_kind="tenant", pharmacy_id=TENANT_B_ID))

        assert result is True
        # Reply goes to Tenant B (the device they texted) — correct
        assert says == [TENANT_B_ID], (
            "reply must leave by Tenant B's device (the one they texted)")
        # No onboarding state — they are not registering anything
        assert not states, "no onboarding state must be created"

    def test_no_staff_row_created_for_cross_tenant_message(self, monkeypatch):
        import register, inspect
        src = inspect.getsource(register.gateway_intercept)
        assert "into staff" not in src, (
            "gateway_intercept must NEVER insert into staff")

    def test_no_customer_row_created_for_cross_tenant_message(self, monkeypatch):
        import register, inspect
        src = inspect.getsource(register.gateway_intercept)
        assert "into customers" not in src, (
            "gateway_intercept must NEVER insert into customers")

    def test_tenant_a_inventory_not_accessible_via_tenant_b_device(self, monkeypatch):
        """The device context (Tenant B) must not be used to read Tenant A's data.

        gateway_intercept sends the 'contact admin' reply using Tenant B's pharmacy_id.
        It must not look up, query, or write anything scoped to Tenant A.
        """
        import register
        query_pids = []

        original_q1 = register.q1

        def spy_q1(sql, params=None, **kw):
            if params:
                for p in (params if isinstance(params, (list, tuple)) else [params]):
                    if isinstance(p, str) and p == TENANT_A_ID:
                        query_pids.append(p)
            return None  # simulate no DB

        monkeypatch.setattr(register, "q1", spy_q1)
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",              lambda *a: None)

        register.gateway_intercept(
            TENANT_A_STAFF,
            _msg(device_kind="tenant", pharmacy_id=TENANT_B_ID))

        assert not query_pids, (
            f"gateway_intercept queried with Tenant A's ID ({TENANT_A_ID}) "
            f"while handling a Tenant B device message — cross-tenant data access")


# ============================================================ Test 6 — idempotency
class TestIdempotency:
    def test_duplicate_wa_id_does_not_send_second_welcome(self, monkeypatch):
        import register
        says   = []
        states = []
        monkeypatch.setattr(register, "platform_pid",  lambda: PLATFORM_ID)
        # Simulate: wa_id already in wa_messages
        monkeypatch.setattr(register, "q1",
                            lambda sql, p=None: {"id": 1} if "wa_messages" in sql else None)
        monkeypatch.setattr(register, "_log_inbound",      lambda *a: None)
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",
                            lambda *a: says.append(a))
        monkeypatch.setattr(register, "set_state",
                            lambda *a, **kw: states.append(a))

        import tenancy as _tenancy
        monkeypatch.setattr(_tenancy, "resolve_by_sender", lambda p: [])

        result = register.gateway_intercept(STRANGER, _msg(wa_id="DUP-001"))

        assert result is True
        assert not says,   "no second welcome on duplicate delivery"
        assert not states, "no second state write on duplicate delivery"

    def test_no_wa_id_still_sends_welcome(self, monkeypatch):
        """A message without wa_id (e.g. from /dev/simulate) has no duplicate to detect."""
        import register, tenancy
        says = []
        monkeypatch.setattr(register, "platform_pid",      lambda: PLATFORM_ID)
        monkeypatch.setattr(register, "q1",                lambda *a, **kw: None)
        monkeypatch.setattr(register, "_log_inbound",      lambda *a: None)
        monkeypatch.setattr(register, "_make_contactable", lambda *a: None)
        monkeypatch.setattr(register, "_say",
                            lambda *a: says.append(a))
        monkeypatch.setattr(register, "set_state",         lambda *a, **kw: None)
        monkeypatch.setattr(tenancy, "resolve_by_sender",  lambda p: [])

        msg = {"from": STRANGER, "text": "Hello", "type": "text",
               "device_kind": "platform"}   # no wa_id
        result = register.gateway_intercept(STRANGER, msg)

        assert result is True
        assert says


# ============================================================ router integration
class TestRouterIntegration:
    def test_greet_unknown_delegates_to_gateway_intercept(self, monkeypatch):
        import router, register
        intercepted = []
        monkeypatch.setattr(register, "gateway_intercept",
                            lambda ph, msg: intercepted.append((ph, msg)) or True)
        msg = _msg()
        router._greet_unknown(STRANGER, msg)
        assert intercepted == [(STRANGER, msg)]

    def test_greet_unknown_logs_when_gateway_returns_false(self, monkeypatch):
        import router, register
        log_msgs = []
        monkeypatch.setattr(register, "gateway_intercept", lambda ph, msg: False)
        orig = router.log.info
        try:
            router.log.info = lambda fmt, *a: log_msgs.append(fmt % a)
            router._greet_unknown(STRANGER, _msg())
        finally:
            router.log.info = orig
        assert any("not replying" in m for m in log_msgs)

    def test_device_kind_is_present_in_msg_from_main(self):
        """main.webhook_gowa must stamp device_kind onto every inbound dict.

        Inspects the source rather than running the endpoint to stay off the database.
        """
        import inspect, main
        src = inspect.getsource(main.webhook_gowa)
        assert "device_kind" in src, (
            "main.webhook_gowa must set inbound['device_kind'] so gateway_intercept "
            "can distinguish platform from unknown devices")
        # Must use tenancy.resolve, not just the shim
        assert "tenancy.resolve" in src or "dev_res" in src, (
            "main.webhook_gowa must call tenancy.resolve() directly to get device_kind")
