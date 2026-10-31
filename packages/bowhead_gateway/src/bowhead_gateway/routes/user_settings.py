"""V2.1.5 · /api/v1/user-settings routes.

GET    /api/v1/user-settings           → 拿当前 user 全部 settings (dict)
PUT    /api/v1/user-settings/{key}     → 设单个 key
DELETE /api/v1/user-settings/{key}     → 删单个 key

当前已支持 key:
- pptx_template_artifact_id (V2.1)
- appearance_theme_config (theme config M1)
- kb_file_scope (knowledge-base file list: all | personal | space | team)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.language_context import LanguageContext, german_ai_output_enabled
from bowhead_core.personal_theme_models import RESERVED_USER_SETTING_KEYS
from bowhead_core.theme_config import (
    KEY_APPEARANCE_THEME_CONFIG,
    THEME_FIELDS,
    ThemeConfigError,
    normalize_appearance_theme_config,
    parse_appearance_theme_config,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.agent_coach_after_edit import AGENT_COACH_AFTER_EDIT_VALUES, KEY_AGENT_COACH_AFTER_EDIT
from bowhead_gateway.configuration_field_guidance import page_field_contract
from bowhead_gateway.routes.auth import _current_user

if TYPE_CHECKING:
    from bowhead_core.user_settings_store import UserSettingsStore
    from bowhead_core.user_store import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/user-settings", tags=["user-settings"])

KEY_KB_FILE_SCOPE = "kb_file_scope"
# Values of GET /api/v1/kb/files ``scope`` the knowledge-base page may restore.
KB_FILE_SCOPE_VALUES = frozenset({"all", "personal", "space", "team"})


_ALLOWED_KEYS = {
    "pptx_template_artifact_id",
    KEY_APPEARANCE_THEME_CONFIG,
    # i18n v1
    "ui_language",  # "zh-CN" | "en-US" | "auto"  (auto = navigator.language)
    "output_language",  # "zh-CN" | "en-US" | "auto" | ...  (auto = unset: follow the question)
    "preferred_model",  # "provider:model" selected from server catalog
    # Personal Answer style setting. FE offers four choices: concise | detailed | technical
    # | business, but **intentionally no enum validation** here: old accounts may store other values, which tightening would
    # lock out. Consumers (chat.py UserProfileHint) also accept free strings.
    "answer_style",
    # V3 PRD-B memory page (also has dedicated /memory/dreaming/{pause,resume})
    "memory_dreaming_paused",  # "true" | "false"
    # Knowledge-base file list: last scope filter the user picked (follows the account across browsers).
    KEY_KB_FILE_SCOPE,
    # What Coach does after writing an Agent draft: run the release check, or ask first.
    KEY_AGENT_COACH_AFTER_EDIT,
}


def _store(request: Request) -> UserSettingsStore:
    store = getattr(request.app.state, "user_settings_store", None)
    if store is None:
        raise HTTPException(503, "user_settings_store not configured")
    return store


class SettingsValue(BaseModel):
    value: str = Field(..., description="单个 setting value (字符串, 复杂结构请 JSON 序列化)")


# Configuration copilot batch 1-a (tech-design-batch1 §7): removed batch 0.5's origin/proposal_id/op tuple;
# self-reported provenance is forgeable (P3-1). Chat writes/audits moved to /api/v1/config-proposals
# (server-verified proposal references); this route returns to batch 0 shape with no copilot traces.


class SettingsAll(BaseModel):
    field_contract: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    settings: dict[str, str]


def _validate_setting_value(key: str, value: str, request: Request | None = None) -> str:
    if key == "output_language" and value == "de-DE" and not german_ai_output_enabled():
        ui_language = LanguageContext.resolve(
            user_profile_language=(request.cookies.get("NEXT_LOCALE") if request else None),
            accept_language=(request.headers.get("accept-language") if request else None),
        ).ui_language
        message = {
            "zh-CN": "当前部署尚未启用德语 AI 回答。",
            "en-US": "German AI output is not enabled in this deployment.",
            "de-DE": "Die deutsche KI-Ausgabe ist in dieser Bereitstellung nicht aktiviert.",
            "fr-FR": "Les réponses de l’IA en allemand ne sont pas activées sur cette installation.",
        }[ui_language]
        raise HTTPException(400, message)
    if key == KEY_APPEARANCE_THEME_CONFIG:
        try:
            return normalize_appearance_theme_config(value)
        except ThemeConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
    if key == KEY_AGENT_COACH_AFTER_EDIT and value not in AGENT_COACH_AFTER_EDIT_VALUES:
        raise HTTPException(400, f"invalid {KEY_AGENT_COACH_AFTER_EDIT} (allowed: {sorted(AGENT_COACH_AFTER_EDIT_VALUES)})")
    if key == KEY_KB_FILE_SCOPE and value not in KB_FILE_SCOPE_VALUES:
        raise HTTPException(400, f"invalid {KEY_KB_FILE_SCOPE} (allowed: {sorted(KB_FILE_SCOPE_VALUES)})")
    return value


@router.get("", response_model=SettingsAll)
async def list_settings(
    request: Request,
    store: UserSettingsStore = Depends(_store),
    user: User = Depends(_current_user),
) -> SettingsAll:
    settings = await store.list_for_user(user.id)
    return SettingsAll(
        settings={key: value for key, value in settings.items() if key not in RESERVED_USER_SETTING_KEYS}, **page_field_contract("user_language")
    )


@router.put("/{key}", response_model=SettingsValue, dependencies=[Depends(require_not_readonly)])
async def set_setting(
    key: str,
    body: SettingsValue,
    request: Request,
    store: UserSettingsStore = Depends(_store),
    user: User = Depends(_current_user),
) -> SettingsValue:
    if key in RESERVED_USER_SETTING_KEYS:
        raise HTTPException(400, "reserved user setting key")
    if key not in _ALLOWED_KEYS:
        raise HTTPException(400, f"unknown key: {key} (allowed: {sorted(_ALLOWED_KEYS)})")
    value = _validate_setting_value(key, body.value, request)
    if key == KEY_APPEARANCE_THEME_CONFIG:
        await _require_legacy_theme_write_allowed(request, user_id=user.id, value=value)
    await store.set(user.id, key, value)
    return SettingsValue(value=value)


@router.delete("/{key}", dependencies=[Depends(require_not_readonly)])
async def delete_setting(
    key: str,
    request: Request,
    store: UserSettingsStore = Depends(_store),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    if key in RESERVED_USER_SETTING_KEYS:
        raise HTTPException(400, "reserved user setting key")
    if key not in _ALLOWED_KEYS:
        raise HTTPException(400, f"unknown key: {key} (allowed: {sorted(_ALLOWED_KEYS)})")
    if key == KEY_APPEARANCE_THEME_CONFIG:
        await _require_legacy_theme_delete_allowed(request, user_id=user.id)
    await store.delete(user.id, key)
    return {"ok": True, "key": key}


async def _require_legacy_theme_write_allowed(
    request: Request,
    *,
    user_id: str,
    value: str,
) -> None:
    personal_store = getattr(request.app.state, "personal_appearance_store", None)
    if personal_store is None:
        return
    rows = await personal_store.read(user_id)
    if rows.config_raw is None:
        return
    try:
        before = parse_appearance_theme_config(rows.legacy_raw or '{"version":1}')
        after = parse_appearance_theme_config(value)
    except ThemeConfigError as exc:
        raise _appearance_v3_managed() from exc
    non_chart_fields = tuple(field for field in THEME_FIELDS if field != "chartPalette")
    if any(before.get(field) != after.get(field) for field in non_chart_fields):
        raise _appearance_v3_managed()


async def _require_legacy_theme_delete_allowed(
    request: Request,
    *,
    user_id: str,
) -> None:
    personal_store = getattr(request.app.state, "personal_appearance_store", None)
    if personal_store is not None and (await personal_store.read(user_id)).config_raw is not None:
        raise _appearance_v3_managed()


def _appearance_v3_managed() -> HTTPException:
    return HTTPException(
        409,
        detail={"error": {"code": "appearance_v3_managed"}},
    )
