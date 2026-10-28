"""ws-datarc-decouple P2b (③/R3) — team datarc connection HITL CRUD (admin-only).

Team-admin endpoints to register / manage the team's datarc connections — the registry that
``chat._resolve_team_datarc_client`` reads at turn-start to build the per-team DatarcClient. All
endpoints are team-scoped via ``user.team_id`` from the ``team_admin`` authz dep, so a caller can
only ever touch their OWN team's connections (IDOR-safe by construction; ``DatarcConnectionStore``
methods are team_id-keyed — R7.1).

Create-time guards:
  - Deployment-mode outbound URL policy: local permits enterprise intranet
    addresses; SaaS accepts only public HTTPS addresses.
  - Fernet auth-at-rest with the F14 guard (store refuses to persist a plaintext auth_key when
    ``BOWHEAD_FERNET_KEY`` is unset → surfaced as 503).

The ``DatarcConnection`` DTO returned carries NO secret (only ``key_hint`` = last 4). The
optional ``POST /validate`` dry-run hits datarc (``list_account_projects`` / ``list_workspaces``)
to confirm a (base_url, auth_key[, account_uid]) actually works — no side effects.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.published_diagnostics import (
    publish_datarc_connection_failure,
)
from bowhead_core.ssrf_guard import SsrfBlocked, validate_url
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_bi_bridge import (
    BIConnectionError,
    BINotFoundError,
    BIPermissionError,
    BIValidationError,
    DatarcClient,
)
from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.error_handler import localize_error_message
from bowhead_gateway.routes.auth import _require_team_role_dep
from bowhead_memory import VectorClient

if TYPE_CHECKING:
    from bowhead_core.datarc_connection_store import DatarcConnection, DatarcConnectionStore
    from bowhead_core.user_store import User

router = APIRouter()
logger = logging.getLogger(__name__)
_REQUIRE_TEAM_ADMIN = _require_team_role_dep("owner", "team_admin")
_STATUS = {"active", "archived"}


def _coded_failure(status_code: int, code: str) -> HTTPException:
    published = publish_datarc_connection_failure(code)
    detail: dict[str, Any] = {"code": code}
    if published is not None:
        detail["diagnostics"] = published.to_wire()
    return HTTPException(status_code, detail=detail)


def _conn_store(request: Request) -> DatarcConnectionStore:
    store: DatarcConnectionStore | None = getattr(request.app.state, "datarc_connection_store", None)
    if store is None:
        raise _coded_failure(503, "datarc_connection_store_unavailable")
    return store


def _check_base_url(base_url: str) -> None:
    try:
        validate_url(base_url)
    except (SsrfBlocked, ValueError) as exc:
        raise _coded_failure(422, "datarc_connection_url_rejected") from exc


def _validation_failure_code(exc: Exception) -> str:
    if isinstance(exc, BIPermissionError):
        return "datarc_connection_auth_rejected"
    if isinstance(exc, BIConnectionError):
        return "datarc_connection_unreachable"
    if isinstance(exc, (BINotFoundError, BIValidationError)):
        return "datarc_connection_configuration_invalid"
    return "datarc_connection_validation_failed"


async def _validation_failure_payload(
    request: Request,
    exc: Exception,
    *,
    embed_info: dict[str, Any],
) -> dict[str, Any]:
    code = _validation_failure_code(exc)
    published = publish_datarc_connection_failure(code)
    logger.exception(
        "datarc_connection.validate_failed code=%s error_type=%s",
        code,
        type(exc).__name__,
    )
    payload: dict[str, Any] = {
        "ok": False,
        "error_code": code,
        "error": await localize_error_message(request, code),
        "embed": embed_info,
    }
    if published is not None:
        payload["diagnostics"] = published.to_wire()
    return payload


def _public(c: DatarcConnection) -> dict[str, Any]:
    """The DTO already excludes the secret (only key_hint); echo it verbatim."""
    return {
        "id": c.id,
        "team_id": c.team_id,
        "name": c.name,
        "base_url": c.base_url,
        "account_uid": c.account_uid,
        "key_hint": c.key_hint,
        "status": c.status,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


class CreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    base_url: str = Field(..., min_length=1, max_length=2048)
    auth_key: str = Field(..., min_length=1, max_length=4096)
    account_uid: str = Field("", max_length=128)
    status: str = "active"  # active | archived


class PatchBody(BaseModel):
    auth_key: str | None = Field(None, min_length=1, max_length=4096)
    status: str | None = None  # active | archived


class ValidateBody(BaseModel):
    base_url: str = Field(..., min_length=1, max_length=2048)
    auth_key: str = Field(..., min_length=1, max_length=4096)
    account_uid: str = Field("", max_length=128)


@router.post("", dependencies=[Depends(require_not_readonly)])
async def create_connection(
    body: CreateBody,
    request: Request,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: DatarcConnectionStore = Depends(_conn_store),
) -> dict[str, Any]:
    _check_base_url(body.base_url)
    if body.status not in {"active", "archived"}:
        raise HTTPException(status_code=422, detail="status must be active or archived")
    try:
        conn = await store.create_connection(
            team_id=user.team_id,
            name=body.name,
            base_url=body.base_url,
            auth_key=body.auth_key,
            account_uid=body.account_uid,
            status=body.status,
        )
    except RuntimeError as e:
        # F14: BOWHEAD_FERNET_KEY unset → store refuses to persist plaintext.
        logger.exception(
            "datarc_connection.create_secret_storage_unavailable error_type=%s",
            type(e).__name__,
        )
        raise _coded_failure(
            503,
            "datarc_connection_secret_storage_unavailable",
        ) from e
    except Exception as e:
        logger.exception(
            "datarc_connection.create_failed error_type=%s",
            type(e).__name__,
        )
        raise _coded_failure(409, "datarc_connection_create_failed") from e
    return _public(conn)


@router.get("")
async def list_connections(
    request: Request,
    include_archived: bool = False,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: DatarcConnectionStore = Depends(_conn_store),
) -> dict[str, Any]:
    try:
        rows = await store.list_connections(
            user.team_id,
            include_archived=include_archived,
        )
    except Exception as e:
        logger.exception(
            "datarc_connection.list_failed error_type=%s",
            type(e).__name__,
        )
        raise _coded_failure(503, "datarc_connection_load_failed") from e
    return {"items": [_public(r) for r in rows]}


@router.post("/validate")
async def validate_connection(
    body: ValidateBody,
    request: Request,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
) -> dict[str, Any]:
    """Dry-run a (base_url, auth_key[, account_uid]) against datarc — no side effects. With an
    account_uid → list that account's projects; without → list the workspaces the key sees.
    Also probes the datarc embed proxy (``?uri=health``) so admins see whether the key has the
    embed permission (powers concept-resolve dictionary lookups) — advisory, never fails the
    validate (embed-via-openapi)."""
    embed_info: dict[str, Any] = {"available": False}
    _check_base_url(body.base_url)
    # Probe client must exactly match **runtime** shape (EmbedProxyClientFactory.get_client).
    # Previously ssrf_guard was off here; internal/HTTP on-prem Datarc probed successfully, showing embed available
    # to admin while guarded runtime client could not connect. False success hid the real cause of always-empty
    # concept resolution. To test the guard, test the one on the actual path.
    _embed_probe = VectorClient(
        base_url=body.base_url,
        access_token=body.auth_key,
        datarc_proxy=True,
        ssrf_guard=True,
    )
    try:
        await _embed_probe.health()
        embed_info["available"] = True
    except Exception as e:
        logger.exception(
            "datarc_connection.embed_probe_failed error_type=%s",
            type(e).__name__,
        )
    finally:
        await _embed_probe.aclose()
    client = DatarcClient(base_url=body.base_url, auth_key=body.auth_key)
    try:
        if body.account_uid:
            projects = await client.list_account_projects(body.account_uid, limit=50)
            return {
                "ok": True,
                "project_count": len(projects),
                "projects": [{"id": str(p.get("id") or p.get("projectId") or ""), "name": p.get("name") or ""} for p in projects][:50],
                "embed": embed_info,
            }
        workspaces = await client.list_workspaces()
        return {
            "ok": True,
            "workspace_count": len(workspaces),
            "workspaces": [{"id": w.id, "name": w.name} for w in workspaces][:50],
            "embed": embed_info,
        }
    except Exception as e:
        return await _validation_failure_payload(
            request,
            e,
            embed_info=embed_info,
        )
    finally:
        await client.aclose()


@router.get("/{dc_id}")
async def get_connection(
    dc_id: str,
    request: Request,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: DatarcConnectionStore = Depends(_conn_store),
) -> dict[str, Any]:
    try:
        conn = await store.get_connection(
            dc_id,
            user.team_id,
        )  # team-scoped read → IDOR-safe
    except Exception as e:
        logger.exception(
            "datarc_connection.get_failed error_type=%s",
            type(e).__name__,
        )
        raise _coded_failure(503, "datarc_connection_load_failed") from e
    if conn is None:
        raise _coded_failure(404, "datarc_connection_not_found")
    return _public(conn)


@router.patch("/{dc_id}", dependencies=[Depends(require_not_readonly)])
async def patch_connection(
    dc_id: str,
    body: PatchBody,
    request: Request,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: DatarcConnectionStore = Depends(_conn_store),
) -> dict[str, Any]:
    try:
        conn = await store.get_connection(dc_id, user.team_id)
    except Exception as e:
        logger.exception(
            "datarc_connection.update_lookup_failed error_type=%s",
            type(e).__name__,
        )
        raise _coded_failure(503, "datarc_connection_update_failed") from e
    if conn is None:
        raise _coded_failure(404, "datarc_connection_not_found")
    if body.status is not None:
        if body.status not in _STATUS:
            raise HTTPException(422, detail={"code": "request_validation_failed"})
        try:
            await store.set_status(dc_id, user.team_id, body.status)
        except Exception as e:
            logger.exception(
                "datarc_connection.status_update_failed error_type=%s",
                type(e).__name__,
            )
            raise _coded_failure(409, "datarc_connection_update_failed") from e
    if body.auth_key is not None:
        try:
            await store.update_connection_auth(dc_id, user.team_id, auth_key=body.auth_key)
        except RuntimeError as e:
            logger.exception(
                "datarc_connection.rotate_secret_storage_unavailable error_type=%s",
                type(e).__name__,
            )
            raise _coded_failure(
                503,
                "datarc_connection_secret_storage_unavailable",
            ) from e
        except Exception as e:
            logger.exception(
                "datarc_connection.rotate_failed error_type=%s",
                type(e).__name__,
            )
            raise _coded_failure(409, "datarc_connection_update_failed") from e
    try:
        got = await store.get_connection(dc_id, user.team_id)
    except Exception as e:
        logger.exception(
            "datarc_connection.update_reload_failed error_type=%s",
            type(e).__name__,
        )
        raise _coded_failure(
            503,
            "datarc_connection_update_failed",
        ) from e
    return _public(got) if got is not None else _public(conn)
