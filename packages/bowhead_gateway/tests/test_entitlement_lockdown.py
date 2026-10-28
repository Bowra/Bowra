"""Entitlement dev-stub/header lockdown tests (§7.3).

§7.3: assert_prod_auth_safe refuses local + DEV_USER_FALLBACK=1; resolve_user_id_sync ignores the
      X-Bowhead-User-Id header in local (non-dev) mode (SEC-8) but honors it in saas / dev.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

# ─── §7.3a — assert_prod_auth_safe startup guard ────────────────────────────────────────────────


def test_assert_prod_auth_safe_local_with_fallback_raises(monkeypatch):
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")  # auth_mode dev → the prod check would pass
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    with pytest.raises(RuntimeError, match="BOWHEAD_DEPLOYMENT_MODE=local"):
        assert_prod_auth_safe()


def test_assert_prod_auth_safe_local_no_fallback_ok(monkeypatch):
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    assert_prod_auth_safe()  # must not raise


def test_assert_prod_auth_safe_prod_fallback_still_raises(monkeypatch):
    """Pre-existing guard preserved."""
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    with pytest.raises(RuntimeError, match="BOWHEAD_AUTH_MODE=prod"):
        assert_prod_auth_safe()


def test_assert_prod_auth_safe_saas_dev_fallback_ok(monkeypatch):
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    assert_prod_auth_safe()  # saas + dev → allowed


# ─── X-Bowhead-User-Id header lockdown (via a real route calling resolve_user_id_sync) ──────────────
# Model is now auth_mode-driven (single source: _is_dev_auth_mode), NOT deployment_mode. The legacy
# §7.3 deployment_mode gate let saas honor a caller-asserted identity header — a spoof vector on the
# internet-facing 116 box. The header (like the anonymous dev_default_user fallback) is now honored
# ONLY in auth_mode='dev'; any non-dev deployment 401s. deployment_mode no longer affects this.


def _whoami_app(auth_mode: str):
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    a = FastAPI()

    class _S:
        dev_default_user = "dev_user"

    _S.auth_mode = auth_mode
    a.state.settings = _S()

    @a.get("/_t/whoami")
    def _whoami(request: Request) -> dict:
        return {"uid": resolve_user_id_sync(request)}

    return TestClient(a)


def test_header_honored_in_dev_auth_mode():
    """dev auth_mode: legacy X-Bowhead-User-Id iframe-loader header is honored."""
    c = _whoami_app("dev")
    r = c.get("/_t/whoami", headers={"X-Bowhead-User-Id": "dev_tester"})
    assert r.json()["uid"] == "dev_tester"


def test_header_rejected_in_prod_auth_mode():
    """prod auth_mode: the spoofable X-Bowhead-User-Id header is NOT honored → 401 (was served as the
    asserted identity on saas before — the closed spoof vector). No anonymous dev_default_user either."""
    c = _whoami_app("prod")
    r = c.get("/_t/whoami", headers={"X-Bowhead-User-Id": "spoofed_user"})
    assert r.status_code == 401


def test_anon_rejected_in_prod_auth_mode():
    """prod auth_mode + no header at all → 401 (116 免密登录 fix; was served as dev_default_user)."""
    c = _whoami_app("prod")
    r = c.get("/_t/whoami")
    assert r.status_code == 401
