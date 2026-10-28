"""Finite, localized, input-free FastAPI request-validation projection."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import httpx2
import pytest
from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog
from bowhead_gateway.main import create_app
from bowhead_gateway.request_validation import (
    VALIDATION_ISSUE_CODES,
    VALIDATION_ISSUE_PARAMS,
)
from fastapi import Body, Query
from pydantic import BaseModel, Field, field_validator


class _ValidationItem(BaseModel):
    name: str = Field(min_length=3, max_length=8)
    password: str

    @field_validator("name")
    @classmethod
    def _reject_marker(cls, value: str) -> str:
        if value == "explode":
            raise ValueError("Authorization: Bearer validator-secret")
        return value


class _ValidationPayload(BaseModel):
    email: str
    items: list[_ValidationItem]
    metadata: dict[str, int]


def test_runtime_and_documented_validation_issue_sets_match() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    catalog_doc = (repo_root / "docs" / "user-facing-error-catalog.md").read_text(
        encoding="utf-8",
    )
    table = catalog_doc.split(
        "<!-- ERROR_VALIDATION_ISSUE_TABLE_START -->",
        maxsplit=1,
    )[1].split(
        "<!-- ERROR_VALIDATION_ISSUE_TABLE_END -->",
        maxsplit=1,
    )[0]
    documented: dict[str, frozenset[str]] = {}
    for line in table.splitlines():
        if not line.startswith("| `"):
            continue
        columns = [column.strip() for column in line.strip().strip("|").split("|")]
        code = columns[0].strip("`")
        params = frozenset() if columns[1] == "none" else frozenset(value.strip().strip("`") for value in columns[1].split(",") if value.strip())
        documented[code] = params

    assert set(documented) == VALIDATION_ISSUE_CODES
    assert documented == VALIDATION_ISSUE_PARAMS
    catalog = load_error_catalog(default_error_catalog_dir())
    assert set(catalog.by_lang["zh-CN"]) >= VALIDATION_ISSUE_CODES
    assert set(catalog.by_lang["en-US"]) >= VALIDATION_ISSUE_CODES


def _build_app(user_store):
    app = create_app()
    app.state.user_store = user_store
    app.state.error_catalog = load_error_catalog(default_error_catalog_dir())

    @app.post("/_test/request-validation")
    async def _validation_route(
        payload: _ValidationPayload = Body(...),
        page: Annotated[int, Query(gt=0)] = 1,
    ) -> dict[str, bool]:
        del payload, page
        return {"ok": True}

    return app


def _assert_no_raw_validation_keys(value: object) -> None:
    if isinstance(value, dict):
        assert "msg" not in value
        assert "input" not in value
        assert "ctx" not in value
        for nested in value.values():
            _assert_no_raw_validation_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_raw_validation_keys(nested)


@pytest.mark.asyncio
async def test_request_validation_projects_body_query_nested_and_sensitive_fields(completed_community_user_store) -> None:
    app = _build_app(completed_community_user_store)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"accept-language": "en-US"},
    ) as client:
        response = await client.post(
            "/_test/request-validation?page=not-an-integer",
            json={
                "email": 123,
                "items": [{"name": "x", "password": 456}],
                "metadata": {"dynamic-secret-key": "not-an-integer"},
            },
        )

    assert response.status_code == 422
    assert response.headers["x-request-id"]
    detail = response.json()["detail"]
    assert detail["code"] == "request_validation_failed"
    assert detail["message"] == "Check the highlighted fields and try again."
    assert detail["diagnostics"] == {
        "schema_version": 1,
        "disclosure": "public-neutral",
        "diagnostic_code": "request_validation_failure",
        "safe_detail": "phase=validate_input; failure_class=validation",
        "retryable": False,
    }
    assert detail["field_errors"] == [
        {"path": "page", "code": "invalid_format", "params": {}},
        {"path": "email", "code": "invalid_format", "params": {}},
        {
            "path": "items.name",
            "code": "value_too_short",
            "params": {"min_length": 3},
        },
        {"code": "invalid_value", "params": {}},
        {"path": "metadata", "code": "invalid_format", "params": {}},
    ]
    _assert_no_raw_validation_keys(response.json())
    assert "not-an-integer" not in response.text
    assert "dynamic-secret-key" not in response.text
    assert "password" not in response.text


@pytest.mark.asyncio
async def test_request_validation_unknown_validator_type_fails_closed(completed_community_user_store) -> None:
    app = _build_app(completed_community_user_store)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"accept-language": "zh-CN"},
    ) as client:
        response = await client.post(
            "/_test/request-validation",
            json={
                "email": "user@example.com",
                "items": [{"name": "explode", "password": "not-returned"}],
                "metadata": {},
            },
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == {
        "code": "request_validation_failed",
        "message": "请检查标出的内容后重试。",
        "diagnostics": {
            "schema_version": 1,
            "disclosure": "public-neutral",
            "diagnostic_code": "request_validation_failure",
            "safe_detail": "phase=validate_input; failure_class=validation",
            "retryable": False,
        },
        "field_errors": [
            {
                "path": "items.name",
                "code": "invalid_value",
                "params": {},
            }
        ],
    }
    assert "validator-secret" not in response.text
    assert "not-returned" not in response.text
    _assert_no_raw_validation_keys(response.json())


@pytest.mark.asyncio
async def test_request_validation_missing_fields_are_localized_and_deduplicated(completed_community_user_store) -> None:
    app = _build_app(completed_community_user_store)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"accept-language": "en-US"},
    ) as client:
        response = await client.post("/_test/request-validation", json={})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["field_errors"] == [
        {"path": "email", "code": "field_required", "params": {}},
        {"path": "items", "code": "field_required", "params": {}},
        {"path": "metadata", "code": "field_required", "params": {}},
    ]
