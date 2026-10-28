"""Publish safe, localized HTTP errors.

Use the current NEXT_LOCALE cookie, then an authenticated account preference,
then browser/system language, then English. Explicit account choices stay authoritative.
Codes, diagnostics, and validation fields remain stable across languages.
"""

from __future__ import annotations

import logging
import re
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bowhead_core.error_catalog import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, ErrorCatalog
from bowhead_core.language_context import _normalize_accept_language  # type: ignore[attr-defined]
from bowhead_core.published_diagnostics import (
    SKILL_WORKFLOW_FAILURE_CODES,
    clarification_failure_copy_code,
    publish_request_validation_failure,
    publish_skill_workflow_failure,
)
from bowhead_core.skill_upload import SkillUploadError
from bowhead_memory.privacy_guard import MemoryWriteBlockedError
from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from bowhead_gateway.http_request_context import (
    REQUEST_ID_HEADER,
    cors_response_headers,
    request_id_from_scope,
)
from bowhead_gateway.skill_workflow_errors import (
    project_skill_workflow_error_params,
)

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_GENERIC_ERROR_CODE = "E_USER_VISIBLE_GENERIC"
_BUILTIN_MESSAGES = {
    "zh-CN": {
        _GENERIC_ERROR_CODE: "操作失败，请稍后重试；如问题持续，请联系管理员。",
    },
    "en-US": {
        _GENERIC_ERROR_CODE: ("Something went wrong. Please try again. If the problem continues, contact your administrator."),
    },
    "de-DE": {
        _GENERIC_ERROR_CODE: "Ein Fehler ist aufgetreten. Versuchen Sie es erneut. Wenden Sie sich bei anhaltenden Problemen an Ihre Administration.",
    },
    "fr-FR": {
        _GENERIC_ERROR_CODE: "Une erreur s’est produite. Réessayez. Si le problème persiste, contactez votre administrateur.",
    },
}


def resolve_terminal_error_language(scope: dict[str, Any]) -> str:
    """Resolve locale without DB, auth, theme, public-entry, or external calls."""

    from starlette.datastructures import Headers

    headers = Headers(scope=scope)
    raw_cookie = headers.get("cookie")
    if raw_cookie:
        cookies = SimpleCookie()
        cookies.load(raw_cookie)
        locale = cookies.get("NEXT_LOCALE")
        if locale is not None and locale.value in SUPPORTED_LANGUAGES:
            return locale.value
    return _normalize_accept_language(headers.get("accept-language")) or DEFAULT_LANGUAGE


def _terminal_error_message(
    scope: dict[str, Any],
    *,
    code: str,
) -> str:
    try:
        language = resolve_terminal_error_language(scope)
    except Exception:
        language = DEFAULT_LANGUAGE
    if language not in SUPPORTED_LANGUAGES:
        language = DEFAULT_LANGUAGE
    fallback = _BUILTIN_MESSAGES[language].get(
        code,
        _BUILTIN_MESSAGES[language][_GENERIC_ERROR_CODE],
    )
    try:
        app = scope.get("app")
        catalog: ErrorCatalog | None = getattr(
            getattr(app, "state", None),
            "error_catalog",
            None,
        )
        if catalog is None:
            return fallback
        rendered = catalog.render(code, language)
        return rendered if rendered and rendered != code else fallback
    except Exception:
        return fallback


def _authoritative_request_id(scope: dict[str, Any]) -> str:
    request_id = request_id_from_scope(scope)
    if request_id is not None and _REQUEST_ID_RE.fullmatch(request_id):
        return request_id
    fallback = uuid4().hex
    logger.warning(
        "gateway.request_id_state_missing fallback_request_id=%s",
        fallback,
    )
    state = scope.setdefault("state", {})
    if isinstance(state, dict):
        state["http_request_id"] = fallback
    return fallback


def _error_path_user_id(request: Request) -> str | None:
    """Signed-in caller id for localization only — never authorizes, never raises.

    This used to read ``settings.dev_default_user``, which is unset in every real
    deployment, so the user-settings branch below was dead in production and the whole
    account preference was ignored. Resolve the real authenticated
    identity instead, but stay fault-tolerant: this runs *inside* the error handler,
    where raising would replace the user's real error with a 500.
    """
    try:
        from bowhead_gateway.routes.auth import signed_in_user_id_or_none

        return signed_in_user_id_or_none(request)
    except Exception as exc:
        logger.debug("error_handler.identity_unavailable err=%s", exc)
        return None


async def _resolve_ui_language(request: Request) -> str:
    """Use cookie/account preferences, then browser/system language, then English."""
    cookie_language = request.cookies.get("NEXT_LOCALE")
    if cookie_language in SUPPORTED_LANGUAGES:
        return cookie_language

    user_settings_store = getattr(request.app.state, "user_settings_store", None)
    if user_settings_store is not None:
        user_id = _error_path_user_id(request)
        if user_id:
            try:
                lang = await user_settings_store.get(user_id, "ui_language")
                if lang and lang != "auto" and lang in SUPPORTED_LANGUAGES:
                    return lang
                if lang == "auto":
                    return _normalize_accept_language(request.headers.get("accept-language")) or DEFAULT_LANGUAGE
            except Exception as _e:
                logger.debug("error_handler.user_settings_failed err=%s", _e)
    return _normalize_accept_language(request.headers.get("accept-language")) or DEFAULT_LANGUAGE


async def localize_error_message(
    request: Request,
    code: str,
    **params: Any,
) -> str:
    """Render a catalog-backed message for non-exception response payloads.

    Most HTTP errors are localized by the registered exception handler. A small
    number of APIs return structured validation issues in a successful response;
    those callers use this helper so their ``message`` follows the same locale
    and catalog instead of exposing validator prose.
    """
    catalog: ErrorCatalog | None = getattr(request.app.state, "error_catalog", None)
    if catalog is None:
        return code
    ui_language = await _resolve_ui_language(request)
    if not catalog.has(code, ui_language):
        return code
    return catalog.render(code, ui_language, **params)


def _enrich_coded_mapping(
    catalog: ErrorCatalog | None,
    value: dict[str, Any],
    ui_language: str,
) -> dict[str, Any]:
    """为一个已登记的 `{code}` mapping 补 message；其他输入保持对象身份."""
    code = value.get("code")
    if not isinstance(code, str) or not code or catalog is None:
        return value
    if isinstance(value.get("message"), str) and value["message"].strip():
        return value
    if not catalog.has(code, ui_language):
        return value

    params = {key: param for key, param in value.items() if key not in {"code", "message", "error"}}
    copy_code = clarification_failure_copy_code(code, value.get("diagnostics"))
    message = catalog.render(copy_code, ui_language, **params)
    if message == copy_code:
        return value
    return {**value, "message": message}


def _enrich_http_detail(
    catalog: ErrorCatalog | None,
    detail: Any,
    ui_language: str,
) -> Any:
    """兼容 `{code}` 与 `{error:{code}}` 两类 detail，不改变其他 envelope."""
    if not isinstance(detail, dict):
        return detail

    enriched = _enrich_coded_mapping(catalog, detail, ui_language)
    if enriched is not detail:
        return enriched

    nested_error = detail.get("error")
    if not isinstance(nested_error, dict):
        return detail
    enriched_error = _enrich_coded_mapping(catalog, nested_error, ui_language)
    if enriched_error is nested_error:
        return detail
    return {**detail, "error": enriched_error}


def _build_error_body(
    catalog: ErrorCatalog | None,
    *,
    code: str | None,
    fallback_message: str,
    ui_language: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """构造统一 error body。"""
    if code is None:
        return {"error": {"message": fallback_message or "Unknown error"}}
    if catalog is None:
        return {"error": {"code": code, "message": fallback_message or code}}
    message = catalog.render(code, ui_language, **params)
    return {"error": {"code": code, "message": message}}


async def _skill_upload_error_handler(
    request: Request,
    exc: SkillUploadError,
) -> JSONResponse:
    """渲染 SkillUploadError → 400；历史自由文本 fail closed."""

    catalog: ErrorCatalog | None = getattr(request.app.state, "error_catalog", None)
    ui_lang = await _resolve_ui_language(request)
    code = exc.code if exc.code in SKILL_WORKFLOW_FAILURE_CODES else "skill_bundle_invalid"
    params = project_skill_workflow_error_params(code, exc.params) if code == exc.code else {}
    if code != exc.code:
        logger.warning(
            "skill_upload.uncoded_error http_request_id=%s err=%s",
            request_id_from_scope(request.scope),
            exc,
        )
    body = _build_error_body(
        catalog,
        code=code,
        fallback_message=code,
        ui_language=ui_lang,
        params=params,
    )
    published = publish_skill_workflow_failure(code)
    if published is not None:
        body["error"]["diagnostics"] = published.to_wire()
    return JSONResponse(status_code=409 if code == "skill_write_conflict" else 400, content=body)


async def _http_exception_error_handler(
    request: Request,
    exc: HTTPException,
) -> Response:
    """为已登记的 code-only HTTPException 补双语 message."""
    catalog: ErrorCatalog | None = getattr(request.app.state, "error_catalog", None)
    ui_language = await _resolve_ui_language(request)
    detail = _enrich_http_detail(catalog, exc.detail, ui_language)
    if detail is exc.detail:
        return await http_exception_handler(request, exc)
    enriched = HTTPException(
        status_code=exc.status_code,
        detail=detail,
        headers=exc.headers,
    )
    return await http_exception_handler(request, enriched)


async def _request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Map Pydantic errors to finite, localized, input-free field issues."""

    route = request.scope.get("route")
    if getattr(route, "name", None) in {
        "owner_results_query",
        "report_results_query",
    }:
        return JSONResponse(
            status_code=400,
            content={"detail": {"error": {"code": "results_query_invalid"}}},
            headers={
                "Cache-Control": "private, no-store",
                "Vary": "Cookie",
            },
        )

    from bowhead_gateway.request_validation import (
        project_request_validation_errors,
    )

    code = "request_validation_failed"
    catalog: ErrorCatalog | None = getattr(request.app.state, "error_catalog", None)
    ui_language = await _resolve_ui_language(request)
    fallback = "请检查标出的内容后重试。" if ui_language == "zh-CN" else "Check the highlighted fields and try again."
    message = catalog.render(code, ui_language) if catalog is not None else fallback
    if not message or message == code:
        message = fallback
    published = publish_request_validation_failure()
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": code,
                "message": message,
                "diagnostics": (published.to_wire() if published is not None else None),
                "field_errors": project_request_validation_errors(
                    request,
                    exc.errors(),
                ),
            }
        },
    )


async def _memory_write_blocked_handler(
    request: Request,
    exc: MemoryWriteBlockedError,
) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=423,
        content={
            "detail": {
                "code": "memory_write_temporarily_blocked",
                "operation_id": exc.operation_id,
                "retryable": True,
            },
        },
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
        },
    )


async def _unhandled_exception_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Dependency-free final 500 response used by Starlette ServerErrorMiddleware."""

    request_id = _authoritative_request_id(request.scope)
    logger.exception(
        "gateway.unhandled_exception http_request_id=%s",
        request_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": _GENERIC_ERROR_CODE,
                "message": _terminal_error_message(
                    request.scope,
                    code=_GENERIC_ERROR_CODE,
                ),
            }
        },
        headers={
            REQUEST_ID_HEADER: request_id,
            "Cache-Control": "no-store",
            **cors_response_headers(request.scope),
        },
    )


async def _client_disconnect_handler(request: Request, exc: ClientDisconnect) -> Response:
    """The caller dropped its own request body mid-upload; nothing failed and no one is listening.

    Routes that read ``request.stream()`` themselves (reference catalog / send) see this whenever the
    browser aborts an in-flight request — e.g. the composer cancelling a stale catalog query on the
    next keystroke. Without this it fell through to the final 500 handler and logged a traceback per
    keystroke, burying real errors.
    """
    logger.info("gateway.client_disconnected http_request_id=%s", _authoritative_request_id(request.scope))
    return Response(status_code=499, headers={"Cache-Control": "no-store"})


def register_error_handlers(app: FastAPI) -> None:
    """主入口：把所有 i18n 感知的异常处理器注册到 FastAPI app."""
    app.add_exception_handler(ClientDisconnect, _client_disconnect_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, _http_exception_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        _request_validation_error_handler,
    )
    app.add_exception_handler(SkillUploadError, _skill_upload_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(  # type: ignore[arg-type]
        MemoryWriteBlockedError,
        _memory_write_blocked_handler,
    )
    app.add_exception_handler(Exception, _unhandled_exception_error_handler)
