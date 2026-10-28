"""Gateway HTTP request correlation and outer error-boundary contracts."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
import structlog
from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog
from bowhead_core.public_entries import PublicEntryStore
from bowhead_gateway.conditional_headers import (
    ConditionalHeaderCanonicalizationMiddleware,
)
from bowhead_gateway.http_request_context import RequestIdContextMiddleware
from bowhead_gateway.installation import CommunitySetupMiddleware
from bowhead_gateway.main import create_app
from bowhead_gateway.memory_mutation_context import MemoryMutationContextMiddleware
from bowhead_gateway.public_entry_context import PublicEntryContextMiddleware
from bowhead_gateway.public_redirects import PublicSlashRedirectMiddleware
from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from bowhead_gateway import error_handler, http_request_context

if TYPE_CHECKING:
    from pathlib import Path

_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ORIGIN = "https://admin.example"


class _ValidationBody(BaseModel):
    name: str


class _CaptureAccessLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append(
            (
                event,
                {
                    **structlog.contextvars.get_contextvars(),
                    **fields,
                },
            )
        )


async def _active_entry(store: PublicEntryStore) -> None:
    pending, token = await store.create_pending(
        team_id="team_http_context",
        base_url="https://registered.example/bowhead",
        actor_id="usr_owner",
    )
    await store.verify_challenge(
        entry_id=pending.id,
        token=token,
    )


async def _build_app(tmp_path: Path) -> FastAPI:
    app = create_app()
    app.state.error_catalog = load_error_catalog(default_error_catalog_dir())
    app.state.deployment_mode = "saas"
    store = PublicEntryStore(tmp_path / "public-entry.db")
    await store.initialize()
    await _active_entry(store)
    app.state.public_entry_store = store

    @app.get("/_test/http-context/success")
    async def _success(response: Response) -> dict[str, bool]:
        response.headers["X-Request-ID"] = "downstream-controlled-id"
        return {"ok": True}

    @app.get("/_test/http-context/http-error")
    async def _http_error() -> None:
        raise HTTPException(
            status_code=409,
            detail={"code": "not_local_deployment"},
        )

    @app.post("/_test/http-context/validation")
    async def _validation(body: _ValidationBody = Body(...)) -> dict[str, str]:
        return {"name": body.name}

    @app.get("/_test/http-context/unhandled")
    async def _unhandled() -> None:
        raise RuntimeError("diagnostic-unhandled-marker")

    return app


def _assert_request_id(response: httpx2.Response) -> str:
    request_id = response.headers["x-request-id"]
    assert _REQUEST_ID_RE.fullmatch(request_id)
    assert request_id != "client-controlled-id"
    assert request_id != "downstream-controlled-id"
    return request_id


def _assert_cors(response: httpx2.Response) -> None:
    assert response.headers["access-control-allow-origin"] == "*"
    exposed = {value.strip().lower() for value in response.headers["access-control-expose-headers"].split(",")}
    assert "x-request-id" in exposed


def test_create_app_has_the_reviewed_effective_user_middleware_order() -> None:
    app = create_app()

    # The Community setup gate runs inside request ids, CORS and public-entry context, so its
    # pending-installation refusals are still logged, CORS-readable and entry-scoped.
    # d89e42a77 placed the public slash redirect between entry context and the setup gate.
    assert [item.cls for item in app.user_middleware[:7]] == [
        RequestIdContextMiddleware,
        ConditionalHeaderCanonicalizationMiddleware,
        CORSMiddleware,
        PublicEntryContextMiddleware,
        PublicSlashRedirectMiddleware,
        CommunitySetupMiddleware,
        MemoryMutationContextMiddleware,
    ]


@pytest.mark.asyncio
async def test_request_id_covers_success_http_exception_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed_community_user_store,
) -> None:
    app = await _build_app(tmp_path)
    app.state.user_store = completed_community_user_store
    captured = _CaptureAccessLogger()
    monkeypatch.setattr(http_request_context, "logger", captured)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={
            "origin": _ORIGIN,
            "x-request-id": "client-controlled-id",
            "authorization": "Bearer request-secret",
        },
    ) as client:
        success = await client.get(
            "/_test/http-context/success?secret=query-secret",
        )
        known_4xx = await client.get("/_test/http-context/http-error")
        validation = await client.post(
            "/_test/http-context/validation",
            json={},
        )

    assert success.status_code == 200
    assert known_4xx.status_code == 409
    assert known_4xx.json()["detail"]["message"]
    assert validation.status_code == 422
    request_ids = {
        _assert_request_id(success),
        _assert_request_id(known_4xx),
        _assert_request_id(validation),
    }
    assert len(request_ids) == 3
    for response in (success, known_4xx, validation):
        _assert_cors(response)

    completed = [fields for event, fields in captured.events if event == "gateway.http_request_completed"]
    assert len(completed) == 3
    assert {fields["http_request_id"] for fields in completed} == request_ids
    assert {fields["status_code"] for fields in completed} == {200, 409, 422}
    assert all("route_template" in fields for fields in completed)
    assert "query-secret" not in repr(completed)
    assert "request-secret" not in repr(completed)
    assert structlog.contextvars.get_contextvars().get("http_request_id") is None


@pytest.mark.asyncio
async def test_public_entry_metadata_never_rejects_an_application_request(
    tmp_path: Path,
) -> None:
    app = await _build_app(tmp_path)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={
            "origin": _ORIGIN,
            "accept-language": "en-US",
            "x-bowhead-public-base": "https://unregistered.example/bowhead",
            "x-request-id": "client-controlled-id",
        },
    ) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "user@example.com", "password": "request-secret"},
        )
        invalid_hint = await client.get(
            "/api/v1/health",
            headers={"x-bowhead-public-base": "ftp://internal.example/secret"},
        )

    assert response.status_code != 421
    request_id = _assert_request_id(response)
    _assert_cors(response)
    assert request_id not in response.text
    assert "unregistered.example" not in response.text
    assert "request-secret" not in response.text
    assert invalid_hint.status_code != 421
    _assert_request_id(invalid_hint)
    _assert_cors(invalid_hint)
    assert "ftp://internal.example/secret" not in invalid_hint.text


@pytest.mark.asyncio
async def test_unhandled_500_uses_state_request_id_and_never_returns_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = await _build_app(tmp_path)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    caplog.set_level(logging.ERROR, logger="bowhead_gateway.error_handler")

    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={
            "origin": _ORIGIN,
            "accept-language": "en-US",
            "x-request-id": "client-controlled-id",
            "authorization": "Bearer request-secret",
        },
    ) as client:
        response = await client.get("/_test/http-context/unhandled")

    assert response.status_code == 500
    request_id = _assert_request_id(response)
    _assert_cors(response)
    assert response.json() == {
        "detail": {
            "code": "E_USER_VISIBLE_GENERIC",
            "message": ("Something went wrong. Please try again. If the problem continues, contact your administrator."),
        }
    }
    assert "diagnostic-unhandled-marker" not in response.text
    assert "request-secret" not in response.text
    matching_logs = [record for record in caplog.records if record.name == "bowhead_gateway.error_handler" and request_id in record.getMessage()]
    assert matching_logs
    assert "diagnostic-unhandled-marker" in caplog.text


@pytest.mark.asyncio
async def test_unhandled_500_falls_back_when_locale_or_catalog_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = await _build_app(tmp_path)

    def _fail_language(_scope: dict[str, Any]) -> str:
        raise RuntimeError("locale resolver failed")

    monkeypatch.setattr(error_handler, "resolve_terminal_error_language", _fail_language)

    class _BrokenCatalog:
        def render(self, *_args: Any, **_kwargs: Any) -> str:
            raise RuntimeError("catalog failed")

    app.state.error_catalog = _BrokenCatalog()
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"origin": _ORIGIN, "accept-language": "en-US"},
    ) as client:
        response = await client.get("/_test/http-context/unhandled")

    assert response.status_code == 500
    _assert_request_id(response)
    _assert_cors(response)
    detail = response.json()["detail"]
    assert detail["code"] == "E_USER_VISIBLE_GENERIC"
    # Locale resolution failed → English fallback (language-policy.md: errors use English without a language).
    assert detail["message"] == error_handler._BUILTIN_MESSAGES["en-US"][error_handler._GENERIC_ERROR_CODE]
    assert "catalog failed" not in response.text
