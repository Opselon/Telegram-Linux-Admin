"""Pytest configuration for ensuring the project package is importable."""

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest


@pytest.fixture(autouse=True)
def _reset_security_state():
    """Keeps global security state from leaking between tests.

    ``permissions``, ``rate_limiter`` and ``audit`` are process-wide singletons;
    without a reset a whitelist enforced by one test would silently break the
    next one (and vice versa).
    """
    from src.access_control import permissions, rate_limiter
    from src.audit import audit

    permissions.set_mode("open")
    permissions.update(whitelisted=(), admins=())
    rate_limiter.reset()
    audit.set_enabled(True)
    yield
