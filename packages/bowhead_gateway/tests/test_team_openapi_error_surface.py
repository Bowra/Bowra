"""What CI can write a retry policy against.

An integration client decides "retry" or "give up" from the status code, so the classification has
to carry that meaning: a busy database is not the same answer as a malformed request, and a dead
key is not the same answer as a key missing a scope. These pin the distinctions that used to
collapse into a single 422 or a single 403.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.agent_version_automation import AgentVersionAutomationError
from bowhead_gateway.edition_extensions import GatewayExtensions
from bowhead_gateway.routes.team_openapi import (
    _automation_http,
    authenticated_openapi_credential,
    openapi_credential,
)
from fastapi import HTTPException


class _Store:
    def __init__(self, credential: dict | None) -> None:
        self.credential = credential
        self.logs: list[dict] = []

    async def authenticate(self, token: str) -> dict | None:
        return self.credential

    async def log(self, **kwargs: object) -> None:
        self.logs.append(dict(kwargs))


def _request(store: _Store, *, header: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(openapi_store=store, deployment_mode="local", gateway_extensions=GatewayExtensions())),
        headers={"authorization": header} if header else {},
        method="GET",
        url=SimpleNamespace(path="/api/openapi/v1/agents/osa_example/version-context"),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("header", [None, "Bearer bhk_dead", "Token bhk_live", "Bearer "])
async def test_a_token_that_does_not_authenticate_is_401(monkeypatch, header) -> None:
    """Missing, malformed, revoked and expired all mean "fix the credential", not "fix the scopes"."""
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    store = _Store(credential=None)

    with pytest.raises(HTTPException) as denied:
        await openapi_credential(_request(store, header=header), "agent_versions:read")

    assert denied.value.status_code == 401
    assert denied.value.detail == {"code": "openapi_token_invalid"}
    assert denied.value.headers == {"WWW-Authenticate": "Bearer"}
    assert store.logs == [], "an unauthenticated caller has no key to attribute a log line to"


@pytest.mark.anyio
async def test_both_credential_doors_answer_a_dead_token_identically(monkeypatch) -> None:
    """One condition, one answer — the scoped and the metadata entry point must not diverge."""
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    store = _Store(credential=None)

    with pytest.raises(HTTPException) as scoped:
        await openapi_credential(_request(store, header="Bearer bhk_dead"), "agent_versions:read")
    with pytest.raises(HTTPException) as metadata:
        await authenticated_openapi_credential(_request(store, header="Bearer bhk_dead"))

    assert (scoped.value.status_code, scoped.value.detail) == (metadata.value.status_code, metadata.value.detail)
    assert scoped.value.headers == metadata.value.headers


@pytest.mark.anyio
async def test_an_authenticated_token_missing_the_scope_is_403(monkeypatch) -> None:
    """Positive control for the 401 above: a live key with the wrong scopes is a different answer."""
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    store = _Store(credential={"id": "oak_1", "team_id": "team_1", "scopes": ["users:read"]})

    with pytest.raises(HTTPException) as denied:
        await openapi_credential(_request(store, header="Bearer bhk_live"), "agent_versions:read")

    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "openapi_scope_denied"}
    assert store.logs and store.logs[0]["status_code"] == 403


@pytest.mark.anyio
async def test_a_write_scope_still_satisfies_the_matching_read(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    store = _Store(credential={"id": "oak_1", "team_id": "team_1", "scopes": ["agent_versions:write"]})

    credential = await openapi_credential(_request(store, header="Bearer bhk_live"), "agent_versions:read")

    assert credential["id"] == "oak_1"


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (sqlite3.OperationalError("database is locked"), 503),
        (aiosqlite.OperationalError("database is locked"), 503),
        (ValueError("workspace_id"), 500),
        (AgentVersionAutomationError("agent_automation_grant_denied"), 403),
        (AgentVersionAutomationError("agent_eval_comparison_forbidden"), 403),
        (AgentVersionAutomationError("agent_owner_required"), 403),
        (AgentVersionAutomationError("agent_release_candidate_not_found"), 404),
        (AgentVersionAutomationError("agent_eval_control_version_conflict"), 409),
        (AgentVersionAutomationError("agent_eval_unavailable"), 503),
        (AgentVersionAutomationError("agent_eval_comparison_create_failed"), 500),
        (AgentVersionAutomationError("agent_automation_context_invalid"), 422),
    ],
)
def test_automation_errors_carry_their_retry_semantics(exc: Exception, status: int) -> None:
    """A transient database failure must not be indistinguishable from a permanently bad request.

    Both used to land on 422, and an authorization refusal did too — so "retry" and "give up" and
    "you are not allowed" were the same signal. 422 is now reserved for what it means: the request
    was understood and is wrong.
    """
    assert _automation_http(exc).status_code == status


def test_conflicts_carry_the_value_needed_to_retry() -> None:
    # A bare 409 sent the 10-14 CI integrator to the Owner's browser session for the current policy id.
    from bowhead_core.agent_revision_store import AgentRevisionConflictError

    exc = AgentRevisionConflictError("agent_eval_policy_version_conflict", details={"current_policy_revision_id": "aepr_1"})
    response = _automation_http(exc)
    assert response.status_code == 409
    assert response.detail == {"code": "agent_eval_policy_version_conflict", "current_policy_revision_id": "aepr_1"}


def test_blocked_preparation_names_the_reasons_and_blocked_dependencies() -> None:
    # A bare agent_dependency_blocked left the pipeline unable to say which Skill or Tool blocks it.
    from bowhead_core.agent_revision_store import AgentPublicationBlockedError

    item = {"kind": "tool", "name": "python_sandbox", "identity": "python_sandbox", "reason_codes": ["precise_skill_scope_required"]}
    exc = AgentPublicationBlockedError("agent_dependency_blocked", reason_codes=("precise_skill_scope_required",), blocked_items=(item,))
    response = _automation_http(exc)
    assert response.status_code == 422
    assert response.detail == {
        "code": "agent_dependency_blocked",
        "reason_codes": ["precise_skill_scope_required"],
        "blocked_items": [item],
    }


def test_owner_version_errors_keep_their_details() -> None:
    from bowhead_core.agent_revision_store import AgentPublicationBlockedError, AgentRevisionConflictError
    from bowhead_gateway.routes.agents import _raise_version_http
    from fastapi import HTTPException

    active = AgentRevisionConflictError("agent_release_candidate_already_active", details={"active_candidate_id": "arc_1"})
    with pytest.raises(HTTPException) as raised:
        _raise_version_http(active)
    assert raised.value.status_code == 409
    assert raised.value.detail == {"error": {"code": "agent_release_candidate_already_active", "details": {"active_candidate_id": "arc_1"}}}
    item = {"kind": "tool", "name": "python_sandbox", "reason_codes": ["precise_skill_scope_required"]}
    blocked = AgentPublicationBlockedError("agent_dependency_blocked", reason_codes=("precise_skill_scope_required",), blocked_items=(item,))
    with pytest.raises(HTTPException) as raised:
        _raise_version_http(blocked)
    assert raised.value.detail["error"]["details"] == {"reason_codes": ["precise_skill_scope_required"], "blocked_items": [item]}
