"""Resolve team answer-language defaults and locks independently of UI copy.

New accounts receive an automatic (unset) answer preference. A team default
applies when an account has no answer preference; a team lock overrides the account.
Existing explicit preferences, including follow_input, remain supported.

LanguageContext and chat carrier detection must use the same decision from
this module so a follow_input policy also computes the question language.
"""

from __future__ import annotations

import json
from typing import Any, Final

KEY_TEAM_LANGUAGE_POLICY: Final = "language_policy"

# Allowed policy modes, consistent with `language_context`: explicit languages, follow_input
# (follow the question), or auto (follow the UI). Deliberately reject arbitrary strings: admins author policies,
# and one spelling error would silently make the entire team fall back to the UI language.
ALLOWED_OUTPUT_LANGUAGE: Final = ("zh-CN", "en-US", "de-DE", "fr-FR", "follow_input", "auto")


class TeamLanguagePolicyError(ValueError):
    """策略 JSON 不合法（形状 / 档位）。调用方转 400。"""


def normalize_team_language_policy(raw_json: str) -> str:
    """校验并规范化策略 JSON；返回紧凑串（入库形态）。

    形状（与 theme policy 对齐）::

        {"version": 1,
         "defaults": {"output_language": "follow_input"},
         "locks": {"output_language": false}}

    `defaults` 缺省即"不设团队默认"；`locks` 缺省即 false。
    """
    try:
        obj = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise TeamLanguagePolicyError(f"{KEY_TEAM_LANGUAGE_POLICY}: invalid JSON") from exc
    if not isinstance(obj, dict):
        raise TeamLanguagePolicyError(f"{KEY_TEAM_LANGUAGE_POLICY}: must be a JSON object")

    defaults_raw = obj.get("defaults") or {}
    locks_raw = obj.get("locks") or {}
    if not isinstance(defaults_raw, dict) or not isinstance(locks_raw, dict):
        raise TeamLanguagePolicyError(f"{KEY_TEAM_LANGUAGE_POLICY}: defaults/locks must be objects")

    normalized: dict[str, Any] = {"version": 1}

    lang = defaults_raw.get("output_language")
    if lang is not None:
        if lang not in ALLOWED_OUTPUT_LANGUAGE:
            raise TeamLanguagePolicyError(
                f"{KEY_TEAM_LANGUAGE_POLICY}: unsupported output_language {lang!r} (allowed: {', '.join(ALLOWED_OUTPUT_LANGUAGE)})",
            )
        if lang == "de-DE":
            from bowhead_core.language_context import german_ai_output_enabled

            if not german_ai_output_enabled():
                raise TeamLanguagePolicyError(
                    f"{KEY_TEAM_LANGUAGE_POLICY}: German AI output is not enabled in this deployment",
                )
        normalized["defaults"] = {"output_language": lang}

    locked = locks_raw.get("output_language")
    if locked:
        # Locking a nonexistent default is meaningless: a lock means overriding everyone with that value.
        if "defaults" not in normalized:
            raise TeamLanguagePolicyError(
                f"{KEY_TEAM_LANGUAGE_POLICY}: locks.output_language requires defaults.output_language",
            )
        normalized["locks"] = {"output_language": True}

    return json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))


def parse_team_language_policy(raw_json: str | None) -> dict[str, Any]:
    """入库串 → dict；坏数据不抛，返回空策略。

    读路径**永不因为一行坏配置打断对话** —— 策略缺失退回用户自己的设置，
    那正是没有策略时的行为。
    """
    if not raw_json:
        return {}
    try:
        obj = json.loads(raw_json)
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


# The mode's **source** affects priority (docs/design/language-policy.md): a team `defaults`
# value ranks below the question's language and the previous answer (a fallback for unset
# accounts); `locks` and saved concrete user choices are explicit choices above it.
SOURCE_LOCK: Final = "lock"
SOURCE_USER: Final = "user"
SOURCE_TEAM_DEFAULT: Final = "team_default"
SOURCE_NONE: Final = ""


def output_language_decision(
    user_settings: dict[str, str] | None,
    team_policy: dict[str, Any] | None,
) -> tuple[str | None, str]:
    """`output_language` 档位 + **它是从哪来的**。

    Returns:
        ``(档位值, 来源)``；档位值原样交给 `LanguageContext.resolve` 的分支处理
        （`follow_input` / 具体语言 / `auto` / None），本函数不做语言解析。
        来源 ∈ `SOURCE_LOCK` / `SOURCE_USER` / `SOURCE_TEAM_DEFAULT` / `SOURCE_NONE`。

    ⚠ 这是档位的**唯一裁决点**：`LanguageContext.resolve` 与 chat 路由（决定要不要算
    提问载体语言）都必须读它，不许各读各的 dict —— 否则会出现「档位认了、carrier 恒为
    None」这种半生效（批6-C 已有前科，见模块 docstring）。
    """
    policy = team_policy or {}
    defaults = policy.get("defaults") or {}
    locks = policy.get("locks") or {}
    team_lang = defaults.get("output_language") if isinstance(defaults, dict) else None

    if isinstance(locks, dict) and locks.get("output_language") and team_lang:
        return str(team_lang), SOURCE_LOCK

    own = (user_settings or {}).get("output_language")
    # A personal "auto" (the new-account seed) is not a choice, so the team default applies.
    if own and own != "auto":
        return str(own), SOURCE_USER
    if team_lang:
        return str(team_lang), SOURCE_TEAM_DEFAULT
    return None, SOURCE_NONE
