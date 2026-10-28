"""Shared Developer API credentials, scope guards and Agent automation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_automation_context import (
    AgentAutomationContextStore,
    AutomationAuthority,
    AutomationPreparation,
    accepted_proposal_conn,
    comparison_intent_hash,
)
from bowhead_core.agent_change_proposal_store import (
    PROPOSAL_EXACT_PATHS,
    PROPOSAL_PATH_PREFIXES,
    AgentChangeProposalStore,
    AgentProposalBaseline,
)
from bowhead_core.agent_eval_comparison import AgentEvalComparisonStore
from bowhead_core.agent_version_automation import AgentVersionAutomationError, AgentVersionAutomationStore
from bowhead_core.agent_version_flags import agent_version_automation_enabled
from bowhead_core.agent_version_service import release_check_outcome_conn, release_tab_path
from bowhead_core.openapi_store import OpenApiStore
from bowhead_core.team_openapi_flags import team_openapi_enabled
from bowhead_core.user_store import User
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import PlainTextResponse

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.product_identity import product_name
from bowhead_gateway.public_url import PublicEntryRequiredError, team_public_app_url
from bowhead_gateway.routes.auth import _require_team_role_dep

manage_router = APIRouter(prefix="/api/v1/team-openapi", tags=["team-openapi"])
api_router = APIRouter(prefix="/api/openapi/v1", tags=["openapi"])
_ADMIN = _require_team_role_dep("owner", "team_admin")
_AGENT_SCOPES = frozenset({"agent_versions:read", "agent_versions:write"})


def available_scopes(request: Request) -> frozenset[str]:
    """Return installed scopes without rewriting persisted credential grants."""
    return _AGENT_SCOPES | request.app.state.gateway_extensions.organization_api_scopes


def _validate_scopes(request: Request, scopes: list[str]) -> None:
    allowed = available_scopes(request)
    if not scopes or not set(scopes).issubset(allowed):
        raise HTTPException(422, {"code": "invalid_scopes", "allowed": sorted(allowed)})


class KeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    scopes: list[str] = Field(..., min_length=1)
    expires_at: datetime | None = None
    permanent: bool = False


class KeyUpdate(BaseModel):
    scopes: list[str] = Field(..., min_length=1)
    expires_at: datetime | None = None
    permanent: bool = False


class AutomationContextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(min_length=1, max_length=200)
    repository: str = Field(min_length=1, max_length=240)
    ref: str = Field(min_length=1, max_length=240)


class AutomationProposalBody(AutomationContextBody):
    commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    base_release_id: str = Field(min_length=1, max_length=200)
    working_copy_id: str = Field(min_length=1, max_length=200)
    working_copy_row_version: int = Field(ge=1)
    patch_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    atomic_groups: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    suggested_cases: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    rationale: str = Field(default="", max_length=4000)


class AutomationPrepareBody(AutomationContextBody):
    expected_evaluation_context_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    working_copy_id: str = Field(min_length=1, max_length=200)
    working_copy_row_version: int = Field(ge=1)
    expected_eval_policy_revision_id: str | None = Field(default=None, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=200)
    release_note: str | None = Field(default=None, max_length=2000)


class AutomationComparisonBody(AutomationContextBody):
    candidate_row_version: int = Field(ge=1)
    expected_control_row_version: int = Field(ge=1)
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1, max_length=200)
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AutomationRunBody(AutomationContextBody):
    release_note: str | None = Field(default=None, max_length=2000)


class AutomationComparisonCancelBody(AutomationContextBody):
    expected_execution_row_version: int = Field(ge=1)


def get_openapi_store(request: Request) -> OpenApiStore:
    store = getattr(request.app.state, "openapi_store", None)
    if store is None:
        raise HTTPException(503, {"code": "openapi_unavailable"})
    return store


def _local(request: Request) -> None:
    """Guard every endpoint on the surface actually being served by this deployment.

    Reads the same predicate the router mount uses, so an unmounted surface and a guarded one can
    never disagree. Kept as a per-endpoint check too: the mount decision is made once at startup,
    and a 404 here is the honest answer for a deployment that does not serve team credentials.
    """
    if not team_openapi_enabled():
        raise HTTPException(404, {"code": "not_found"})


def _expiry(body: KeyCreate) -> str | None:
    if body.permanent:
        return None
    value = body.expires_at or datetime.now(UTC) + timedelta(days=30)
    if value <= datetime.now(UTC):
        raise HTTPException(422, {"code": "invalid_expiry"})
    return value.astimezone(UTC).isoformat()


def _updated_expiry(body: KeyUpdate) -> str | None:
    if body.permanent:
        return None
    if body.expires_at is None or body.expires_at <= datetime.now(UTC):
        raise HTTPException(422, {"code": "invalid_expiry"})
    return body.expires_at.astimezone(UTC).isoformat()


@manage_router.get("/keys")
async def list_keys(request: Request, caller: User = Depends(_ADMIN)) -> dict:
    _local(request)
    return {"items": await get_openapi_store(request).list_keys(caller.team_id)}


@manage_router.post("/keys", status_code=201, dependencies=[Depends(require_not_readonly)])
async def create_key(body: KeyCreate, request: Request, caller: User = Depends(_ADMIN)) -> dict:
    _local(request)
    _validate_scopes(request, body.scopes)
    try:
        key, token = await get_openapi_store(request).create_key(team_id=caller.team_id, name=body.name, scopes=body.scopes, expires_at=_expiry(body))
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid_scopes", "allowed": sorted(available_scopes(request))}) from exc
    return {"key": key, "token": token}  # one-time response only


@manage_router.patch("/keys/{key_id}", dependencies=[Depends(require_not_readonly)])
async def update_key(key_id: str, body: KeyUpdate, request: Request, caller: User = Depends(_ADMIN)) -> dict:
    _local(request)
    _validate_scopes(request, body.scopes)
    try:
        return await get_openapi_store(request).update_key(
            team_id=caller.team_id,
            key_id=key_id,
            scopes=body.scopes,
            expires_at=_updated_expiry(body),
        )
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid_scopes", "allowed": sorted(available_scopes(request))}) from exc
    except KeyError as exc:
        raise HTTPException(404, {"code": "key_not_found"}) from exc


@manage_router.post("/keys/{key_id}/disable", dependencies=[Depends(require_not_readonly)])
async def disable_key(key_id: str, request: Request, caller: User = Depends(_ADMIN)) -> dict:
    _local(request)
    try:
        return await get_openapi_store(request).set_status(caller.team_id, key_id, "disabled")
    except KeyError as exc:
        raise HTTPException(404, {"code": "key_not_found"}) from exc


@manage_router.post("/keys/{key_id}/enable", dependencies=[Depends(require_not_readonly)])
async def enable_key(key_id: str, request: Request, caller: User = Depends(_ADMIN)) -> dict:
    _local(request)
    try:
        return await get_openapi_store(request).set_status(caller.team_id, key_id, "active")
    except KeyError as exc:
        raise HTTPException(404, {"code": "key_not_found"}) from exc


@manage_router.post("/keys/{key_id}/rotate", dependencies=[Depends(require_not_readonly)])
async def rotate_key(key_id: str, request: Request, caller: User = Depends(_ADMIN)) -> dict:
    _local(request)
    try:
        key, token = await get_openapi_store(request).rotate(caller.team_id, key_id)
        return {"key": key, "token": token}
    except KeyError as exc:
        raise HTTPException(404, {"code": "key_not_found"}) from exc


@manage_router.delete("/keys/{key_id}", dependencies=[Depends(require_not_readonly)])
async def delete_key(key_id: str, request: Request, caller: User = Depends(_ADMIN)) -> dict:
    _local(request)
    try:
        await get_openapi_store(request).set_status(caller.team_id, key_id, "deleted")
    except KeyError as exc:
        raise HTTPException(404, {"code": "key_not_found"}) from exc
    return {"ok": True}


@manage_router.get("/logs")
async def logs(request: Request, caller: User = Depends(_ADMIN)) -> dict:
    _local(request)
    return {"items": await get_openapi_store(request).list_logs(caller.team_id)}


@manage_router.get("/documentation")
async def documentation(request: Request, caller: User = Depends(_ADMIN)) -> dict:
    _local(request)
    document = {
        "base_path": "/api/openapi/v1",
        "scopes": sorted(available_scopes(request)),
        "endpoints": [
            {
                "method": "GET",
                "path": "/scopes",
                "scope": "authentication only",
                "summary": "Validate the access key; return granted scopes and current app version",
            },
            {
                "method": "POST",
                "path": "/agents/{agent_id}/version-proposals",
                "scope": "agent_versions:write + proposal:create grant",
                "summary": "Create an immutable Git-backed Agent change proposal for Owner review",
                "path_params": "agent_id",
                "body": "grant_id, repository, ref, commit_sha, base_release_id, working_copy_id, working_copy_row_version, patch_hash, operations, atomic_groups, suggested_cases?, rationale?; Idempotency-Key header",
                "notes": _PROPOSAL_NOTES,
            },
            {
                "method": "GET",
                "path": "/agents/{agent_id}/version-context",
                "scope": "agent_versions:read + gate:read grant",
                "summary": "Read the working-copy identity a proposal must be opened against; null while the Owner has no draft",
                "path_params": "agent_id",
                "body": "query: grant_id, repository, ref",
            },
            {
                "method": "GET",
                "path": "/agents/{agent_id}/version-proposals/{proposal_id}",
                "scope": "agent_versions:read + gate:read grant",
                "summary": "Read proposal status and, once the Owner accepts, the exact working-copy identity prepare requires",
                "path_params": "agent_id, proposal_id",
                "body": "query: grant_id, repository, ref",
            },
            {
                "method": "POST",
                "path": "/agents/{agent_id}/version-proposals/{proposal_id}/run",
                "scope": "agent_versions:write + candidate:prepare and eval:run grant",
                "summary": "Prepare an Owner-accepted Git proposal and start its release comparison in one call; the server reads every version",
                "path_params": "agent_id, proposal_id",
                "body": "grant_id, repository, ref, release_note?; Idempotency-Key header (up to 160 characters)",
                "notes": _RUN_NOTES,
            },
            {
                "method": "POST",
                "path": "/agents/{agent_id}/version-proposals/{proposal_id}/prepare",
                "scope": "agent_versions:write + candidate:prepare grant",
                "summary": "Prepare the exact working copy produced by an Owner-accepted Git proposal",
                "path_params": "agent_id, proposal_id",
                "body": "grant_id, repository, ref, working_copy_id, working_copy_row_version, expected_eval_policy_revision_id?, workspace_id, release_note?; Idempotency-Key header",
                "notes": _PREPARE_NOTES,
            },
            {
                "method": "POST",
                "path": "/agents/{agent_id}/release-candidates/{candidate_id}/comparisons",
                "scope": "agent_versions:write + eval:run grant",
                "summary": "Start a full release comparison for an exact prepared candidate",
                "path_params": "agent_id, candidate_id",
                "body": "grant_id, repository, ref, candidate_row_version, expected_control_row_version, candidate_hash, plan_id, plan_hash; Idempotency-Key header",
                "notes": _COMPARISON_NOTES,
            },
            {
                "method": "POST",
                "path": "/agents/{agent_id}/release-candidates/{candidate_id}/comparisons/{execution_id}/cancel",
                "scope": "agent_versions:write + eval:run grant",
                "summary": "Cancel a comparison created by the same exact automation grant",
                "path_params": "agent_id, candidate_id, execution_id",
                "body": "grant_id, repository, ref, expected_execution_row_version",
            },
            {
                "method": "GET",
                "path": "/agents/{agent_id}/release-candidates/{candidate_id}/gate",
                "scope": "agent_versions:read + gate:read grant",
                "summary": "Read opaque candidate, Plan, Snapshot, readiness, reason codes, failure summary, and next-action identities",
                "path_params": "agent_id, candidate_id",
                "body": "grant_id, repository, ref query parameters",
                "notes": _GATE_NOTES,
            },
        ],
    }
    if not agent_version_automation_enabled():
        # Don't advertise what this deployment will answer 404 to. An integrator reads this list
        # (and the public reference.md generated from it) to decide what to build against.
        document["endpoints"] = [endpoint for endpoint in document["endpoints"] if not _is_agent_version_endpoint(str(endpoint["path"]))]
    for endpoint in document["endpoints"]:
        endpoint["response_example"] = _response_example(endpoint["method"], endpoint["path"])
    # Keep private documentation with its installed handler module.
    document["endpoints"][1:1] = [dict(endpoint) for endpoint in request.app.state.gateway_extensions.organization_api_reference]
    return document


# Rules a CI author cannot infer from field names. Each sentence states what the server checks.
_PROPOSAL_NOTES = (
    '`operations` is a list of 1-200 JSON Patch style objects: `{"op": "add" | "replace", "path": <JSON Pointer>, "value": <any JSON>}` '
    'or `{"op": "remove", "path": <JSON Pointer>}`; `remove` must not carry `value`. Escape `/` as `~1` and `~` as `~0` inside a path segment.',
    "Allowed paths: "
    + ", ".join(f"`{path}`" for path in sorted(PROPOSAL_EXACT_PATHS))
    + ", and any path below "
    + ", ".join(f"`{prefix}`" for prefix in PROPOSAL_PATH_PREFIXES)
    + ". Any other path returns `403 agent_change_proposal_path_forbidden`. When the Owner accepts, `add` creates missing parent "
    "objects, while `replace` and `remove` need the field to exist.",
    '`atomic_groups` is a list of 1-100 objects `{"id": <unique non-empty string>, "operation_indexes": [<0-based index>, ...], '
    '"depends_on": [<group id>, ...]}`. Every operation index belongs to exactly one group, `depends_on` names other groups only and must '
    "not form a cycle; otherwise the request returns `422 agent_change_groups_invalid`. The Owner accepts whole groups, and a group can be "
    "accepted only together with the groups it depends on.",
    '`suggested_cases` (optional, up to 100) holds `{"task", "failure_mode", "covers_group_ids": [<group id>, ...], "id"?, "case_spec"?}`; '
    "the Owner decides whether to add each case to an evaluation suite.",
    '`patch_hash` is `"sha256:" + lowercase hex SHA-256` of the UTF-8 bytes of `operations` serialized as JSON with object keys sorted, '
    "no whitespace (separators `,` and `:`) and non-ASCII characters written as-is (Python: `json.dumps(operations, sort_keys=True, "
    'separators=(",", ":"), ensure_ascii=False)`). The server hashes `operations` after parsing them, so numbers are written the '
    "way Python writes them (`1.50` becomes `1.5`, `1e2` becomes `100.0`); hash the parsed values, as the reference client does, or send "
    "integers and short decimals. Send finite numbers only. A different hash returns `422 agent_change_patch_hash_mismatch`.",
    "Take `working_copy_id`, `working_copy_row_version` and `base_release_id` from `GET /agents/{agent_id}/version-context` (`working_copy`). "
    "Repeat a request with the same `Idempotency-Key` and the same body to recover a lost response; a different body with that key returns "
    "`409 agent_change_proposal_idempotency_conflict`.",
    "A runnable reference client and an example payload are in the repository under `examples/agent-ci/` and `scripts/agent-ci/run.py`.",
)
_RUN_NOTES = (
    "The usual call after the Owner accepts a proposal. The server takes the accepted working copy, the grant's evaluation Space and the "
    "current evaluation Policy itself, prepares the candidate, then starts the release comparison on that candidate's plan. The response "
    "holds `preparation` (as `.../prepare` returns it) and `comparison` (as `.../comparisons` returns it); poll the gate with "
    "`preparation.candidate_id` for the result.",
    "Repeat the call with the same `Idempotency-Key` to recover a lost response or to finish after an interruption: a preparation or "
    "comparison that already happened is returned, not repeated. The two steps record the operation keys `<key>:prepare` and `<key>:run`.",
    "Refusals are those of the two steps: `409 agent_automation_proposal_not_accepted` before the Owner accepts or after the draft "
    "changed again, `409 agent_release_candidate_already_active` with `active_candidate_id` and `next_step`, `409 agent_eval_plan_behind` "
    "when cases were saved after an earlier preparation, and `403 agent_automation_grant_denied` naming a missing action.",
    "Use `.../prepare` and `.../comparisons` separately only when the pipeline must pin the exact Policy and context it read.",
)
_PREPARE_NOTES = (
    "Read `workspace_id` and `expected_eval_policy_revision_id` from `GET /agents/{agent_id}/version-context`: "
    "`evaluation_context.workspace_id` and `evaluation_context.policy.revision_id`. Send `evaluation_context.context_hash` as "
    "`expected_evaluation_context_hash` to detect evaluation changes made after that read.",
    "`working_copy_id` and `working_copy_row_version` must equal `accepted_result.working_copy_id` and "
    "`accepted_result.working_copy_row_version` from `GET /agents/{agent_id}/version-proposals/{proposal_id}` after the Owner accepts.",
    "If the evaluation Policy changed, the response is `409 agent_eval_policy_version_conflict` with `current_policy_revision_id`; "
    "confirm the change was intended, then read `version-context` again and retry with its new `policy.revision_id` and `context_hash` "
    "(the context hash covers the Policy, so resending only the new revision returns `409 agent_automation_context_conflict`).",
    "If another candidate is still prepared or pending review, the response is `409 agent_release_candidate_already_active` with "
    "`active_candidate_id`, `active_candidate_state` and `next_step`: continue with that candidate, or ask the Owner to choose "
    "Prepare from latest draft on the Release tab, which replaces it.",
)
_COMPARISON_NOTES = (
    "If test cases were saved after the candidate was prepared, its test plan is behind them and the response is "
    "`409 agent_eval_plan_behind` with `candidate_id`, `plan_id`, `plan_policy_revision_id` and `current_policy_revision_id`. "
    "The pipeline can't update the plan: report the refusal to the Owner. When the Owner runs the comparison on the Agent's "
    "Release tab, the plan is updated from the latest saved cases first; read the gate again for the result.",
)
_GATE_NOTES = (
    "`failure_summary` is `null` until a comparison has produced a snapshot. Afterwards it counts failing required cases: "
    "`required_failed` = `introduced` (passed on Live, fails in this candidate) + `also_failing_on_live` + `not_run_on_live` "
    "(the case's suite did not run on Live). It contains no case names, inputs or answers.",
    "`failure_summary.owner_url` links to the Agent's Release tab through the Team's public entry. It is `null` when no public "
    "address is available; direct people to the Agent by its id instead.",
    "`failure_summary.unmet_points` lists up to 20 `{case_key, point_id}` pairs for expected points a failing case did not meet. "
    "It never contains point text or quotes from answers; the Owner reads those on the Release tab.",
    "`comparison_overview` is `null` until a snapshot exists. Afterwards it gives `cases`, `candidate_pass_rate`, `live_pass_rate` "
    "(each `null` when no case on that side reached a verdict), `newly_failing` and `newly_passing`.",
)


def _is_agent_version_endpoint(path: str) -> bool:
    return path.startswith("/agents/")


def _response_example(method: str, path: str) -> dict[str, Any]:
    examples = {
        ("GET", "/scopes"): {"scopes": ["agent_versions:read"], "version": "<app-version>"},
        ("POST", "/agents/{agent_id}/version-proposals"): {
            "proposal_id": "acp_example",
            "status": "open",
            "row_version": 1,
            "patch_hash": f"sha256:{'1' * 64}",
        },
        ("POST", "/agents/{agent_id}/version-proposals/{proposal_id}/run"): {
            "kind": "agent_version_run",
            "proposal_id": "acp_example",
            "preparation": {"candidate_id": "arc_example", "plan_id": "aep_example"},
            "comparison": {"execution_id": "aec_example", "status": "queued", "candidate_id": "arc_example", "plan_id": "aep_example"},
            "schema_version": 1,
        },
        ("POST", "/agents/{agent_id}/version-proposals/{proposal_id}/prepare"): {
            "candidate_id": "arc_example",
            "candidate_hash": f"sha256:{'2' * 64}",
            "plan_id": "aep_example",
            "plan_hash": f"sha256:{'3' * 64}",
        },
        ("POST", "/agents/{agent_id}/release-candidates/{candidate_id}/comparisons"): {
            "execution_id": "aec_example",
            "status": "queued",
            "candidate_id": "arc_example",
            "plan_id": "aep_example",
        },
        ("POST", "/agents/{agent_id}/release-candidates/{candidate_id}/comparisons/{execution_id}/cancel"): {
            "execution_id": "aec_example",
            "status": "cancelled",
            "candidate_id": "arc_example",
        },
        ("GET", "/agents/{agent_id}/version-context"): {
            "agent_id": "osa_example",
            "working_copy": {
                "working_copy_id": "awc_example",
                "working_copy_row_version": 4,
                "base_release_id": "asr_example",
            },
            "live_release_id": "asr_example",
            "schema_version": 1,
        },
        ("GET", "/agents/{agent_id}/version-proposals/{proposal_id}"): {
            "agent_id": "osa_example",
            "proposal_id": "acp_example",
            "status": "accepted",
            "row_version": 2,
            "patch_hash": f"sha256:{'4' * 64}",
            "accepted_result": {
                "working_copy_id": "awc_example",
                "working_copy_row_version": 5,
                "decided_at": "2026-09-17T00:00:00+00:00",
            },
            "schema_version": 1,
        },
        ("GET", "/agents/{agent_id}/release-candidates/{candidate_id}/gate"): {
            "agent_id": "osa_example",
            "candidate_identity": {"candidate_id": "arc_example", "candidate_hash": f"sha256:{'2' * 64}"},
            "active_plan_identity": {"id": "aep_example", "hash": f"sha256:{'3' * 64}"},
            "active_snapshot_identity": None,
            "readiness": "missing",
            "stale_subject": None,
            "reason_codes": [],
            "primary_action": {"kind": "run_release_comparison", "enabled": True},
            "comparison_overview": None,
            "failure_summary": None,
            "schema_version": 1,
        },
    }
    return examples[(method, path)]


def _reference_markdown(document: dict[str, Any]) -> str:
    lines = [
        f"# {product_name()} Team API reference",
        "",
        "## Base path",
        "",
        "`<service-public-address>/api/openapi/v1`",
        "",
        "Append each endpoint path below to this base path. For example, use `<service-public-address>/api/openapi/v1/scopes` for `GET /scopes`.",
        "",
        "## Authentication",
        "",
        "```http",
        "Authorization: Bearer <token>",
        "```",
        "",
        "> A write scope includes the matching read scope.",
        "",
    ]
    for endpoint in document["endpoints"]:
        lines.extend(
            [
                f"## {endpoint['method']} `{endpoint['path']}`",
                "",
                endpoint["summary"],
                "",
                f"- **Required scope:** `{endpoint['scope']}`",
                f"- **Path parameters:** {endpoint.get('path_params', 'None')}",
                f"- **Request fields:** `{endpoint['body']}`" if endpoint.get("body") else "- **Request fields:** None",
                *(["- **Rules:**", *[f"  - {note}" for note in endpoint.get("notes", ())]] if endpoint.get("notes") else []),
                "- **Response example:**",
                "```json",
                json.dumps(endpoint["response_example"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


@api_router.get("/reference.md", include_in_schema=False)
async def reference_markdown(request: Request) -> PlainTextResponse:
    """Public local-only Markdown reference; it never includes team data or credentials."""

    document = await documentation(request, caller=None)  # type: ignore[arg-type]
    return PlainTextResponse(
        _reference_markdown(document),
        headers={"Content-Disposition": 'attachment; filename="bowhead-team-api-reference.md"'},
        media_type="text/markdown",
    )


async def openapi_credential(request: Request, scope: str) -> dict:
    _local(request)
    header = request.headers.get("authorization", "")
    credential = await get_openapi_store(request).authenticate(header.removeprefix("Bearer ").strip()) if header.startswith("Bearer ") else None
    if credential is None:
        # A missing, malformed, revoked or expired token is an authentication failure. Reporting it
        # as 403 openapi_scope_denied — which is what fell out of computing an empty scope set —
        # told an integrator to go fix their scopes when the answer is "rotate the key".
        # Same code and challenge header as `authenticated_openapi_credential`: one condition,
        # one answer, whichever door the caller came through.
        raise HTTPException(401, {"code": "openapi_token_invalid"}, headers={"WWW-Authenticate": "Bearer"})
    allowed = set(credential["scopes"])
    resource = scope.split(":", 1)[0]
    if scope not in available_scopes(request) or (scope not in allowed and not (scope.endswith(":read") and f"{resource}:write" in allowed)):
        await get_openapi_store(request).log(
            credential=credential,
            method=request.method,
            path=request.url.path,
            status_code=403,
            request_summary="scope:denied",
            response_summary="required_scope:" + scope,
        )
        raise HTTPException(403, {"code": "openapi_scope_denied"})
    return credential


async def authenticated_openapi_credential(request: Request) -> dict:
    """Authenticate a token for metadata endpoints without requiring a business scope."""

    _local(request)
    header = request.headers.get("authorization", "")
    credential = await get_openapi_store(request).authenticate(header.removeprefix("Bearer ").strip()) if header.startswith("Bearer ") else None
    if credential is None:
        raise HTTPException(401, {"code": "openapi_token_invalid"}, headers={"WWW-Authenticate": "Bearer"})
    return credential


def require_scope(scope: str):
    async def dep(request: Request) -> dict:
        return await openapi_credential(request, scope)

    return dep


@api_router.get("/scopes")
async def get_scopes(request: Request, credential: dict = Depends(authenticated_openapi_credential)) -> dict:
    scopes = sorted(set(credential["scopes"]) & available_scopes(request))
    version = os.environ.get("BOWHEAD_VERSION", "").strip() or "dev"
    await get_openapi_store(request).log(
        credential=credential,
        method="GET",
        path=request.url.path,
        status_code=200,
        request_summary="scope:inspect",
        response_summary=f"scopes:{len(scopes)}",
    )
    return {"scopes": scopes, "version": version}


def _require_agent_automation() -> None:
    if not agent_version_automation_enabled():
        raise HTTPException(404, {"code": "not_found"})


def _automation_http(exc: Exception) -> HTTPException:
    # An exception with no `code` is not a business outcome — it is an unexpected failure, and a
    # transient one (a busy SQLite writer) must not be reported as 422, which tells CI the request
    # itself was wrong and will never succeed. Classify by exception type before falling through to
    # the code table, so a retry policy written against these statuses is actually correct.
    if isinstance(exc, sqlite3.OperationalError | aiosqlite.OperationalError):
        return HTTPException(503, {"code": "agent_version_automation_unavailable"})
    if not hasattr(exc, "code"):
        return HTTPException(500, {"code": "agent_version_automation_failed"})
    code = str(getattr(exc, "code", "agent_version_automation_failed"))
    if code in {
        "agent_automation_grant_denied",
        "agent_automation_origin_mismatch",
        "agent_automation_workspace_mismatch",
        "agent_automation_workspace_unavailable",
    }:
        status = 403
    elif code.endswith("_not_found"):
        status = 404
    elif code.endswith(("_forbidden", "_denied")) or code == "agent_owner_required":
        # Authorization outcomes are 403, not "your request was malformed".
        status = 403
    elif (
        "conflict" in code
        or "stale" in code
        or "already_active" in code
        or code in {"agent_automation_proposal_not_accepted", "agent_eval_plan_behind"}
    ):
        # agent_eval_plan_behind: the candidate's plan predates the latest saved test cases; the Owner updates it.
        status = 409
    elif "unavailable" in code or "disabled" in code:
        status = 503
    elif code.endswith(("_failed", "_error")):
        # Server-side failures the caller cannot fix by changing the request.
        status = 500
    else:
        status = 422
    details = getattr(exc, "details", None)
    extra: dict[str, Any] = {str(key): str(value) for key, value in details.items() if key != "code"} if isinstance(details, dict) else {}
    # A blocked preparation names why and which Skill or Tool blocks it, as on the Owner surface.
    reason_codes = tuple(getattr(exc, "reason_codes", ()) or ())
    if reason_codes:
        extra["reason_codes"] = [str(item) for item in reason_codes]
    blocked_items = tuple(getattr(exc, "blocked_items", ()) or ())
    if blocked_items:
        extra["blocked_items"] = [dict(item) for item in blocked_items]
    return HTTPException(status, {"code": code, **extra})


async def _upgrade_platform_contract(db_path: str, *, team_id: str, agent_id: str, owner_id: str) -> None:
    """Bring the platform contract and policy current before CI prepares, in their own transaction.

    A pipeline holding the pre-upgrade policy id then gets a 409 naming the current one. Failure here
    is not the pipeline's to fix: preparation reports any real problem itself.
    """
    from bowhead_core.agent_eval_policy_store import AgentEvalPolicyError
    from bowhead_core.agent_eval_store import AgentEvalStore, AgentEvalStoreError

    try:
        await AgentEvalStore(db_path).ensure_platform_contract_suite(team_id=team_id, agent_id=agent_id, actor_user_id=owner_id)
    except (AgentEvalStoreError, AgentEvalPolicyError):
        return


async def _automation_actor(
    *,
    request: Request,
    credential: dict[str, Any],
    agent_id: str,
    body: AutomationContextBody,
    scope: str,
) -> tuple[AgentVersionAutomationStore, dict[str, Any]]:
    _require_agent_automation()
    automation = AgentVersionAutomationStore(request.app.state.user_store.db_path)
    try:
        actor = await automation.authorize(
            team_id=credential["team_id"],
            agent_id=agent_id,
            grant_id=body.grant_id,
            principal_id=credential["id"],
            required_scope=scope,
            repository=body.repository,
            ref=body.ref,
        )
    except AgentVersionAutomationError as exc:
        raise _automation_http(exc) from exc
    return automation, actor


def _authority(credential: dict[str, Any], body: AutomationContextBody, actor: dict[str, Any]) -> AutomationAuthority:
    return AutomationAuthority(
        principal_id=credential["id"],
        grant_id=body.grant_id,
        repository=body.repository,
        ref=body.ref,
        effective_owner_user_id=actor["effective_owner_user_id"],
    )


async def _log_agent_version_call(
    request: Request,
    *,
    credential: dict[str, Any],
    status_code: int,
    action: str,
    outcome: str,
) -> None:
    await get_openapi_store(request).log(
        credential=credential,
        method=request.method,
        path=request.url.path,
        status_code=status_code,
        request_summary=f"agent_version:{action}",
        response_summary=outcome,
    )


@api_router.get("/agents/{agent_id}/version-context")
async def read_automation_agent_version_context(
    agent_id: str,
    grant_id: str,
    repository: str,
    ref: str,
    request: Request,
    credential: dict = Depends(require_scope("agent_versions:read")),
) -> dict[str, Any]:
    """Read a consistent, content-free context without materializing drafts or Policies."""
    body = AutomationContextBody(grant_id=grant_id, repository=repository, ref=ref)
    _, actor = await _automation_actor(request=request, credential=credential, agent_id=agent_id, body=body, scope="gate:read")
    try:
        result = await AgentAutomationContextStore(request.app.state.user_store.db_path).read_context(
            team_id=credential["team_id"], agent_id=agent_id, authority=_authority(credential, body, actor)
        )
    except Exception as exc:
        raise _automation_http(exc) from exc
    await _log_agent_version_call(request, credential=credential, status_code=200, action="version_context_read", outcome="ok")
    return result


@api_router.post("/agents/{agent_id}/version-proposals", status_code=201)
async def create_automation_agent_proposal(
    agent_id: str,
    body: AutomationProposalBody,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    credential: dict = Depends(require_scope("agent_versions:write")),
) -> dict[str, Any]:
    _automation, actor = await _automation_actor(
        request=request,
        credential=credential,
        agent_id=agent_id,
        body=body,
        scope="proposal:create",
    )
    try:
        # Canonical JSON has no NaN or Infinity; reject them here instead of failing later in storage.
        patch_json = json.dumps(body.operations, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    except ValueError:
        raise HTTPException(422, {"code": "agent_change_proposal_invalid_patch", "reason": "non_finite_number"}) from None
    actual_patch_hash = "sha256:" + hashlib.sha256(patch_json.encode()).hexdigest()
    if actual_patch_hash != body.patch_hash:
        raise HTTPException(422, {"code": "agent_change_patch_hash_mismatch"})
    user_store = request.app.state.user_store
    request_json = json.dumps(body.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    request_hash = "sha256:" + hashlib.sha256(request_json.encode()).hexdigest()
    idempotency_digest = hashlib.sha256(f"{credential['id']}\0{idempotency_key}".encode()).hexdigest()
    source_ref = f"git:idempotency:{idempotency_digest}"
    async with aiosqlite.connect(user_store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id,status,row_version,patch_hash,created_by_id,provenance_json
            FROM agent_change_proposal
            WHERE team_id=? AND agent_id=? AND source_type='git' AND source_ref=?
            ORDER BY created_at,id LIMIT 1""",
            (credential["team_id"], agent_id, source_ref),
        ) as cursor:
            replay = await cursor.fetchone()
        if replay is not None:
            provenance = json.loads(str(replay["provenance_json"]))
            if (
                str(replay["created_by_id"]) != str(credential["id"])
                or provenance.get("automation_grant_id") != body.grant_id
                or provenance.get("request_hash") != request_hash
            ):
                raise HTTPException(409, {"code": "agent_change_proposal_idempotency_conflict"})
            await _log_agent_version_call(request, credential=credential, status_code=201, action="proposal_create", outcome="replayed")
            return {
                "proposal_id": str(replay["id"]),
                "status": str(replay["status"]),
                "row_version": int(replay["row_version"]),
                "patch_hash": str(replay["patch_hash"]),
            }
        async with db.execute(
            """SELECT base_release_id,row_version FROM team_subagent_working_copy
            WHERE team_id=? AND subagent_id=? AND id=?""",
            (credential["team_id"], agent_id, body.working_copy_id),
        ) as cursor:
            working = await cursor.fetchone()
    if (
        working is None
        or str(working["base_release_id"] or "") != body.base_release_id
        or int(working["row_version"]) != body.working_copy_row_version
    ):
        raise HTTPException(409, {"code": "agent_working_copy_version_conflict"})
    try:
        proposal = await AgentChangeProposalStore(user_store.db_path).create(
            team_id=credential["team_id"],
            agent_id=agent_id,
            effective_owner_user_id=actor["effective_owner_user_id"],
            source_type="git",
            source_ref=source_ref,
            operations=body.operations,
            atomic_groups=body.atomic_groups,
            suggested_cases=body.suggested_cases,
            rationale=body.rationale,
            created_by_kind="automation",
            created_by_id=credential["id"],
            expected_baseline=AgentProposalBaseline(body.working_copy_id, body.working_copy_row_version, body.base_release_id),
            automation=_authority(credential, body, actor),
            provenance={
                "automation_grant_id": body.grant_id,
                "commit_sha": body.commit_sha.lower(),
                "idempotency_key_hash": "sha256:" + hashlib.sha256(idempotency_key.encode()).hexdigest(),
                "ref": body.ref,
                "repository": body.repository,
                "request_hash": request_hash,
            },
        )
    except Exception as exc:
        raise _automation_http(exc) from exc
    await _log_agent_version_call(request, credential=credential, status_code=201, action="proposal_create", outcome="created")
    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "row_version": proposal.row_version,
        "patch_hash": body.patch_hash,
    }


@api_router.get("/agents/{agent_id}/version-proposals/{proposal_id}")
async def read_automation_agent_proposal(
    agent_id: str,
    proposal_id: str,
    grant_id: str,
    repository: str,
    ref: str,
    request: Request,
    credential: dict = Depends(require_scope("agent_versions:read")),
) -> dict[str, Any]:
    """Proposal status, and once accepted, the exact working-copy identity `prepare` demands.

    `prepare` refuses anything but an exact echo of the accept decision's
    `result_working_copy_id` / `result_working_copy_version`. Those were written and read back for
    verification but never returned to anyone, so the value CI had to echo was unobtainable.
    Scoped to proposals this same principal created under this same grant — the same pair
    `prepare` itself enforces, so a read can never reveal more than the follow-up call accepts.
    """
    body = AutomationContextBody(grant_id=grant_id, repository=repository, ref=ref)
    await _automation_actor(
        request=request,
        credential=credential,
        agent_id=agent_id,
        body=body,
        scope="gate:read",
    )
    user_store = request.app.state.user_store
    async with aiosqlite.connect(user_store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT proposal.status,proposal.row_version,proposal.patch_hash,proposal.created_by_id,
                   json_extract(proposal.provenance_json,'$.automation_grant_id') AS proposal_grant_id,
                   decision.result_working_copy_id,decision.result_working_copy_version,
                   decision.created_at AS decided_at
            FROM agent_change_proposal proposal
            LEFT JOIN agent_change_proposal_decision decision
              ON decision.proposal_id=proposal.id AND decision.decision_kind='patch_accept'
            WHERE proposal.team_id=? AND proposal.agent_id=? AND proposal.id=? AND proposal.source_type='git'
            ORDER BY decision.created_at DESC,decision.id DESC LIMIT 1
            """,
            (credential["team_id"], agent_id, proposal_id),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None or str(row["created_by_id"]) != str(credential["id"]) or str(row["proposal_grant_id"] or "") != grant_id:
        raise HTTPException(404, {"code": "agent_change_proposal_not_found"})
    accepted = row["result_working_copy_id"] is not None
    await _log_agent_version_call(request, credential=credential, status_code=200, action="proposal_read", outcome=str(row["status"]))
    return {
        "agent_id": agent_id,
        "proposal_id": proposal_id,
        "status": str(row["status"]),
        "row_version": int(row["row_version"]),
        "patch_hash": str(row["patch_hash"]),
        # Present only once the Owner has accepted; this is exactly what `prepare` must be given.
        "accepted_result": (
            None
            if not accepted
            else {
                "working_copy_id": str(row["result_working_copy_id"]),
                "working_copy_row_version": int(row["result_working_copy_version"]),
                "decided_at": str(row["decided_at"]),
            }
        ),
        "schema_version": 1,
    }


@api_router.post("/agents/{agent_id}/version-proposals/{proposal_id}/prepare")
async def prepare_automation_agent_candidate(
    agent_id: str,
    proposal_id: str,
    body: AutomationPrepareBody,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    credential: dict = Depends(require_scope("agent_versions:write")),
) -> dict[str, Any]:
    _automation, actor = await _automation_actor(
        request=request,
        credential=credential,
        agent_id=agent_id,
        body=body,
        scope="candidate:prepare",
    )
    user_store = request.app.state.user_store
    owner = await user_store.get_user(actor["effective_owner_user_id"])
    if owner is None:
        raise HTTPException(403, {"code": "agent_automation_grant_denied"})
    # Preserve the inexpensive rejection before constructing publication dependencies;
    # the command repeats these checks inside its write transaction.
    try:
        async with open_shared_ctx(user_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            accepted = await accepted_proposal_conn(
                db, team_id=credential["team_id"], agent_id=agent_id, authority=_authority(credential, body, actor), proposal_id=proposal_id
            )
            if (
                accepted["status"] != "accepted"
                or accepted["result_working_copy_id"] != body.working_copy_id
                or accepted["result_working_copy_version"] != body.working_copy_row_version
            ):
                raise AgentVersionAutomationError("agent_automation_proposal_not_accepted")
    except Exception as exc:
        raise _automation_http(exc) from exc
    from bowhead_gateway.routes.agents import _preparation_dict, _revision_store, record_working_copy_semantic_evidence

    await record_working_copy_semantic_evidence(
        request,
        user=owner,
        store=user_store,
        agent_id=agent_id,
        working_copy_id=body.working_copy_id,
        working_copy_row_version=body.working_copy_row_version,
    )
    await _upgrade_platform_contract(user_store.db_path, team_id=credential["team_id"], agent_id=agent_id, owner_id=owner.id)
    try:
        prepared = await (await _revision_store(request, user=owner, store=user_store)).prepare_working_copy(
            team_id=credential["team_id"],
            agent_id=agent_id,
            actor_user_id=owner.id,
            expected_working_copy_id=body.working_copy_id,
            expected_working_copy_version=body.working_copy_row_version,
            expected_eval_policy_revision_id=body.expected_eval_policy_revision_id,
            workspace_id=body.workspace_id,
            idempotency_key=f"automation:{credential['id']}:{idempotency_key}",
            release_note=body.release_note,
            automation=AutomationPreparation(
                authority=_authority(credential, body, actor),
                proposal_id=proposal_id,
                expected_context_hash=body.expected_evaluation_context_hash,
                policy_expectation_provided="expected_eval_policy_revision_id" in body.model_fields_set,
            ),
        )
    except Exception as exc:
        raise _automation_http(exc) from exc
    await _log_agent_version_call(request, credential=credential, status_code=200, action="candidate_prepare", outcome="prepared")
    return _preparation_dict(prepared)


@api_router.post("/agents/{agent_id}/version-proposals/{proposal_id}/run", status_code=202)
async def run_automation_agent_proposal(
    agent_id: str,
    proposal_id: str,
    body: AutomationRunBody,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=160),
    credential: dict = Depends(require_scope("agent_versions:write")),
) -> dict[str, Any]:
    """Prepare an accepted proposal and start its release comparison, with every version read by the server.

    The steps are the same commands as `.../prepare` and `.../comparisons` (same grant actions, checks,
    audit and idempotency), so nothing here can do more than those two calls together. The pipeline sends
    no working-copy, Policy, context, candidate or plan identity: the server reads the accepted result and
    the current evaluation context, then the prepared candidate and its plan, in that order.
    """
    context = AutomationContextBody(grant_id=body.grant_id, repository=body.repository, ref=body.ref)
    _automation, actor = await _automation_actor(request=request, credential=credential, agent_id=agent_id, body=context, scope="candidate:prepare")
    # Check the second action up front too, so a grant without it refuses before anything is prepared.
    await _automation_actor(request=request, credential=credential, agent_id=agent_id, body=context, scope="eval:run")
    user_store = request.app.state.user_store
    authority = _authority(credential, context, actor)
    try:
        async with open_shared_ctx(user_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            accepted = await accepted_proposal_conn(
                db, team_id=credential["team_id"], agent_id=agent_id, authority=authority, proposal_id=proposal_id
            )
        if accepted["status"] != "accepted" or accepted["result_working_copy_id"] is None:
            raise AgentVersionAutomationError("agent_automation_proposal_not_accepted")
    except Exception as exc:
        raise _automation_http(exc) from exc
    # The platform contract may add a Policy revision; read the context after it, as `.../prepare` checks it.
    await _upgrade_platform_contract(user_store.db_path, team_id=credential["team_id"], agent_id=agent_id, owner_id=actor["effective_owner_user_id"])
    try:
        current = await AgentAutomationContextStore(user_store.db_path).read_context(
            team_id=credential["team_id"], agent_id=agent_id, authority=authority
        )
    except Exception as exc:
        raise _automation_http(exc) from exc
    evaluation = current.get("evaluation_context")
    if current["preparation_blockers"] or not evaluation:
        raise _automation_http(
            AgentVersionAutomationError(
                "agent_automation_context_conflict",
                details={"preparation_blockers": ",".join(current["preparation_blockers"])} if current["preparation_blockers"] else None,
            )
        )
    preparation = await prepare_automation_agent_candidate(
        agent_id,
        proposal_id,
        AutomationPrepareBody(
            **context.model_dump(),
            expected_evaluation_context_hash=evaluation["context_hash"],
            working_copy_id=str(accepted["result_working_copy_id"]),
            working_copy_row_version=int(accepted["result_working_copy_version"]),
            expected_eval_policy_revision_id=evaluation["policy"]["revision_id"],
            workspace_id=str(evaluation["workspace_id"]),
            release_note=body.release_note,
        ),
        request,
        f"{idempotency_key}:prepare",
        credential,
    )
    comparison = await start_automation_agent_comparison(
        agent_id,
        str(preparation["candidate_id"]),
        AutomationComparisonBody(
            **context.model_dump(),
            candidate_row_version=int(preparation["candidate_row_version"]),
            expected_control_row_version=int(preparation["control_row_version"]),
            candidate_hash=str(preparation["candidate_hash"]),
            plan_id=str(preparation["plan_id"]),
            plan_hash=str(preparation["plan_hash"]),
        ),
        request,
        f"{idempotency_key}:run",
        credential,
    )
    return {"kind": "agent_version_run", "proposal_id": proposal_id, "preparation": preparation, "comparison": comparison, "schema_version": 1}


@api_router.post("/agents/{agent_id}/release-candidates/{candidate_id}/comparisons", status_code=202)
async def start_automation_agent_comparison(
    agent_id: str,
    candidate_id: str,
    body: AutomationComparisonBody,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    credential: dict = Depends(require_scope("agent_versions:write")),
) -> dict[str, Any]:
    _automation, actor = await _automation_actor(
        request=request,
        credential=credential,
        agent_id=agent_id,
        body=body,
        scope="eval:run",
    )
    user_store = request.app.state.user_store
    owner = await user_store.get_user(actor["effective_owner_user_id"])
    if owner is None:
        raise HTTPException(403, {"code": "agent_automation_grant_denied"})
    from bowhead_gateway.routes.agents import (
        _comparison_dict,
        _comparison_model_hashes,
        _require_eval_runtime,
        _schedule_comparison,
    )

    authority = _authority(credential, body, actor)
    comparison = AgentEvalComparisonStore(user_store.db_path)
    key = f"automation:{credential['id']}:{idempotency_key}"
    intent = comparison_intent_hash(
        team_id=credential["team_id"],
        agent_id=agent_id,
        authority=authority,
        candidate_id=candidate_id,
        candidate_hash=body.candidate_hash,
        candidate_row_version=body.candidate_row_version,
        plan_id=body.plan_id,
        plan_hash=body.plan_hash,
        expected_control_row_version=body.expected_control_row_version,
    )

    async def replay():
        return await comparison.replay_automation(
            team_id=credential["team_id"],
            agent_id=agent_id,
            candidate_id=candidate_id,
            automation=authority,
            idempotency_key=key,
            client_intent_hash=intent,
        )

    try:
        execution = await replay()
        if execution is None:
            try:
                runtime = _require_eval_runtime(request)
                model_hashes, judge_hash = await _comparison_model_hashes(
                    runtime=runtime, db_path=user_store.db_path, team_id=credential["team_id"], candidate_id=candidate_id, plan_id=body.plan_id
                )
            except Exception:
                execution = await replay()
                if execution is None:
                    raise
            if execution is None:
                execution = await comparison.start_release(
                    team_id=credential["team_id"],
                    agent_id=agent_id,
                    candidate_id=candidate_id,
                    actor_user_id=owner.id,
                    candidate_row_version=body.candidate_row_version,
                    expected_control_row_version=body.expected_control_row_version,
                    candidate_hash=body.candidate_hash,
                    plan_id=body.plan_id,
                    plan_hash=body.plan_hash,
                    model_config_hashes=model_hashes,
                    idempotency_key=key,
                    judge_model_fingerprint=judge_hash,
                    actor_kind="automation",
                    actor_ref=credential["id"],
                    automation_grant_id=body.grant_id,
                    automation=authority,
                )
                await _schedule_comparison(runtime, execution)
                execution = await comparison.get(execution.id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _automation_http(exc) from exc
    await _log_agent_version_call(request, credential=credential, status_code=202, action="comparison_start", outcome=execution.status)
    return _comparison_dict(execution)


@api_router.post("/agents/{agent_id}/release-candidates/{candidate_id}/comparisons/{execution_id}/cancel")
async def cancel_automation_agent_comparison(
    agent_id: str,
    candidate_id: str,
    execution_id: str,
    body: AutomationComparisonCancelBody,
    request: Request,
    credential: dict = Depends(require_scope("agent_versions:write")),
) -> dict[str, Any]:
    _automation, actor = await _automation_actor(
        request=request,
        credential=credential,
        agent_id=agent_id,
        body=body,
        scope="eval:run",
    )
    try:
        execution = await AgentEvalComparisonStore(request.app.state.user_store.db_path).cancel(
            team_id=credential["team_id"],
            agent_id=agent_id,
            candidate_id=candidate_id,
            execution_id=execution_id,
            actor_user_id=actor["effective_owner_user_id"],
            expected_execution_row_version=body.expected_execution_row_version,
            actor_kind="automation",
            automation_grant_id=body.grant_id,
            automation=_authority(credential, body, actor),
        )
    except Exception as exc:
        raise _automation_http(exc) from exc
    await _log_agent_version_call(request, credential=credential, status_code=200, action="comparison_cancel", outcome="cancelled")
    from bowhead_gateway.routes.agents import _comparison_dict

    return _comparison_dict(execution)


@api_router.get("/agents/{agent_id}/release-candidates/{candidate_id}/gate")
async def read_automation_agent_gate(
    agent_id: str,
    candidate_id: str,
    grant_id: str,
    repository: str,
    ref: str,
    request: Request,
    credential: dict = Depends(require_scope("agent_versions:read")),
) -> dict[str, Any]:
    body = AutomationContextBody(grant_id=grant_id, repository=repository, ref=ref)
    _automation, actor = await _automation_actor(
        request=request,
        credential=credential,
        agent_id=agent_id,
        body=body,
        scope="gate:read",
    )
    user_store = request.app.state.user_store
    owner = await user_store.get_user(actor["effective_owner_user_id"])
    if owner is None:
        raise HTTPException(403, {"code": "agent_automation_grant_denied"})
    from bowhead_gateway.routes.agents import _version_service

    try:
        workbench = await (await _version_service(request, user=owner, store=user_store)).workbench(
            team_id=credential["team_id"],
            agent_id=agent_id,
            actor_user_id=owner.id,
        )
    except Exception as exc:
        raise _automation_http(exc) from exc
    candidate = workbench.get("candidate_identity")
    if not isinstance(candidate, dict) or candidate.get("candidate_id") != candidate_id:
        raise HTTPException(404, {"code": "agent_release_candidate_not_found"})
    await _log_agent_version_call(request, credential=credential, status_code=200, action="gate_read", outcome="ok")
    # The Release tab's outcome, shared with Coach's evidence, so the gate never re-derives it.
    async with open_shared_ctx(user_store.db_path) as db:
        db.row_factory = aiosqlite.Row
        outcome = await release_check_outcome_conn(db, team_id=credential["team_id"], agent_id=agent_id, actor_user_id=owner.id, workbench=workbench)
    failure_summary = await _gate_failure_summary(request, team_id=credential["team_id"], agent_id=agent_id, counts=outcome["failure_summary"])
    return {
        "agent_id": agent_id,
        "candidate_identity": workbench.get("candidate_identity"),
        "active_plan_identity": workbench.get("active_plan_identity"),
        "active_snapshot_identity": workbench.get("active_snapshot_identity"),
        "readiness": outcome["readiness"],
        "stale_subject": outcome["stale_subject"],
        # The design contract promises reason codes here: without them CI learns only that the gate
        # is not clear, not what to fix (116: "ready_with_risks" with no hint it was owner_quality_skipped).
        "reason_codes": outcome["reason_codes"],
        "primary_action": workbench.get("primary_action"),
        # Eval credibility PRD R15: counts and rates only, the same content-free class as failure_summary.
        "comparison_overview": workbench.get("comparison_overview"),
        # PRD R13a: content-free counts of failing required cases by D3 label, plus the Owner's link.
        "failure_summary": failure_summary,
        "schema_version": 1,
    }


async def _gate_failure_summary(request: Request, *, team_id: str, agent_id: str, counts: dict[str, Any] | None) -> dict[str, Any] | None:
    """The Release tab's failing-case counts (None before a snapshot exists) and the Owner's Release tab URL.

    CI has no browser address, so the link uses the Team's selected public entry; when none is
    available it is None and the pipeline prints the Agent id instead. A link failure never hides
    the counts.
    """
    if counts is None:
        return None
    try:
        owner_url: str | None = await team_public_app_url(request, team_id=team_id, path=release_tab_path(agent_id))
    except (PublicEntryRequiredError, ValueError):
        owner_url = None
    return {**counts, "owner_url": owner_url}


@api_router.get("/agents/{agent_id}/version-proposals/{proposal_id}/automation-state")
async def read_agent_automation_state(
    agent_id: str,
    proposal_id: str,
    request: Request,
    grant_id: str,
    repository: str,
    ref: str,
    operation_key_hash: str = Query(pattern=r"^sha256:[0-9a-f]{64}$"),
    offset: int = Query(default=0, ge=0, le=100000),
    limit: int = Query(default=25, ge=1, le=50),
    credential: dict = Depends(require_scope("agent_versions:read")),
) -> dict[str, Any]:
    body = AutomationContextBody(grant_id=grant_id, repository=repository, ref=ref)
    _, actor = await _automation_actor(request=request, credential=credential, agent_id=agent_id, body=body, scope="gate:read")
    try:
        result = await AgentAutomationContextStore(request.app.state.user_store.db_path).read_automation_state(
            team_id=credential["team_id"],
            agent_id=agent_id,
            proposal_id=proposal_id,
            authority=_authority(credential, body, actor),
            operation_key_hash=operation_key_hash,
            offset=offset,
            limit=limit,
        )
    except Exception as exc:
        raise _automation_http(exc) from exc
    await _log_agent_version_call(request, credential=credential, status_code=200, action="automation_state_read", outcome="ok")
    return result


@api_router.get("/agents/{agent_id}/release-candidates/{candidate_id}/comparison-operation")
async def read_agent_comparison_operation(
    agent_id: str,
    candidate_id: str,
    request: Request,
    grant_id: str,
    repository: str,
    ref: str,
    operation_key_hash: str = Query(pattern=r"^sha256:[0-9a-f]{64}$"),
    credential: dict = Depends(require_scope("agent_versions:read")),
) -> dict[str, Any]:
    body = AutomationContextBody(grant_id=grant_id, repository=repository, ref=ref)
    _, actor = await _automation_actor(request=request, credential=credential, agent_id=agent_id, body=body, scope="gate:read")
    try:
        result = await AgentAutomationContextStore(request.app.state.user_store.db_path).read_comparison_operation(
            team_id=credential["team_id"],
            agent_id=agent_id,
            candidate_id=candidate_id,
            authority=_authority(credential, body, actor),
            operation_key_hash=operation_key_hash,
        )
    except Exception as exc:
        raise _automation_http(exc) from exc
    await _log_agent_version_call(request, credential=credential, status_code=200, action="comparison_operation_read", outcome="ok")
    return result
