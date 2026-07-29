"""Shared test setup.

Production binds a tenant per unit of work: the router does it after resolving an inbound
message, and the jobs loop does it once per pharmacy. Tests are not going through either
path, so without this they call tenant-scoped code with nothing bound and raise NoTenant --
which is `pid()` behaving exactly as designed, and not what those tests are checking.

So tests run inside a tenant scope by default, mirroring the router. A test that is
specifically about the absence of a tenant clears it explicitly (see test_tenancy.py);
nesting works, so a test that binds its own pharmacy just shadows this one.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


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
