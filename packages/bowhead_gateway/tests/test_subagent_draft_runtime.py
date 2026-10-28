from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_subagent_draft_hmac_key_uses_active_jwt_derived_key(monkeypatch) -> None:
    from bowhead_gateway.routes.auth import derive_jwt_secret_keys
    from bowhead_gateway.subagent_draft_runtime import subagent_draft_hmac_key

    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", "active=current-key,previous=previous-key")

    expected = bytes.fromhex(derive_jwt_secret_keys("subagent-draft-hmac")[0][1])
    assert subagent_draft_hmac_key() == expected


def _chat_request(**state):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _draft_runtime_state():
    return {
        "user_store": SimpleNamespace(),
        "subagent_draft_store": SimpleNamespace(),
        "subagent_draft_credit_accountant": SimpleNamespace(),
    }


def test_chat_turn_gets_draft_runtime_by_default(monkeypatch) -> None:
    from bowhead_core.subagent_draft_service import SubagentDraftService
    from bowhead_gateway.routes.chat import _subagent_draft_turn_overrides

    from bowhead_gateway import _entitlement_gate

    async def _licensed(_request, feature: str) -> bool:
        return feature == "mcp"

    monkeypatch.setattr(_entitlement_gate, "is_feature_licensed_request", _licensed)
    request = _chat_request(**_draft_runtime_state())

    overrides = asyncio.run(_subagent_draft_turn_overrides(request, team_id="team_a", team_router=object()))

    assert isinstance(overrides["subagent_draft_service"], SubagentDraftService)
    assert overrides["subagent_draft_mcp_entitled"] is True


def test_chat_turn_skips_draft_runtime_without_team_model(monkeypatch) -> None:
    from bowhead_gateway.routes.chat import _subagent_draft_turn_overrides

    request = _chat_request(**_draft_runtime_state())
    assert asyncio.run(_subagent_draft_turn_overrides(request, team_id="team_a", team_router=None)) == {}


def test_chat_turn_keeps_running_when_draft_runtime_is_unavailable(monkeypatch) -> None:
    from bowhead_gateway.routes.chat import _subagent_draft_turn_overrides

    request = _chat_request(user_store=None)

    assert asyncio.run(_subagent_draft_turn_overrides(request, team_id="team_a", team_router=object())) == {}


def _send_impl() -> ast.AsyncFunctionDef:
    """Static wiring guard (same approach as test_byo_no_clone_regression.py)."""
    import bowhead_gateway.routes.chat as chat_routes

    tree = ast.parse(Path(chat_routes.__file__).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_impl")


def test_send_impl_merges_draft_runtime_into_turn_overrides() -> None:
    fn = _send_impl()
    merged = [
        call
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "update"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "_byo_overrides"
        and call.args
        and isinstance(call.args[0], ast.Await)
        and isinstance(call.args[0].value, ast.Call)
        and isinstance(call.args[0].value.func, ast.Name)
        and call.args[0].value.func.id == "_subagent_draft_turn_overrides"
        and any(k.arg == "team_router" and isinstance(k.value, ast.Name) and k.value.id == "team_router" for k in call.args[0].value.keywords)
    ]
    assert len(merged) == 1
    applied = [
        call
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_dc_replace"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "turn_req"
        and any(k.arg is None and isinstance(k.value, ast.Name) and k.value.id == "_byo_overrides" for k in call.keywords)
    ]
    assert len(applied) == 1
