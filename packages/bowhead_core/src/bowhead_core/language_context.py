"""Resolve interface, AI-output, source-data language, and week boundaries.

Interface language: request preference > account preference > Space preference
> browser/system language > English. Missing and ``auto`` interface preferences
follow Accept-Language. Channels without a browser (IM and scheduled jobs) use
the incoming message language in the browser's place, then the session's
previous answer language. AI output: team lock > saved concrete account choice
or ``follow_input`` > the question's language > the session's previous answer
language > concrete team default > interface language. A missing or personal
``auto`` answer preference counts as unset. Existing explicit preferences stay valid.
Source-data language: Space data setting > Chinese, independently of product copy.
This describes stored data and must not change when the interface language changes.
German model output remains gated by FEATURE_GERMAN_AI_OUTPUT; French is available.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, replace
from typing import Any

from bowhead_core.iana_time_zone import load_iana_time_zone
from bowhead_core.team_language_policy import SOURCE_LOCK, SOURCE_TEAM_DEFAULT, output_language_decision

# v1 supported languages (same domain for UI and answers).
SUPPORTED_LANGUAGES = ("zh-CN", "en-US", "de-DE", "fr-FR")
SUPPORTED_DATA_LANGUAGES = ("zh-CN", "en-US")
DEFAULT_LANGUAGE = "en-US"
# Source-data language describes existing data, independently of product copy.
DEFAULT_DATA_LANGUAGE = "zh-CN"


def german_ai_output_enabled() -> bool:
    """Return whether German model output is admitted in this deployment."""
    return os.environ.get("FEATURE_GERMAN_AI_OUTPUT", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Third settings tier (scheme A=③): follow this question's **carrier language**.
# ⚠ A **setting value**, not a language: never let it flow into `output_language`
# (`__post_init__` raises, appropriately: UI and answer languages must use supported locales).
FOLLOW_INPUT = "follow_input"

# First weekday.
SUPPORTED_WEEK_START = ("monday", "sunday")
DEFAULT_WEEK_START = "monday"

# Key for language_context in LLMRequest.metadata.
METADATA_KEY = "language_context"


@dataclass(frozen=True)
class LanguageContext:
    ui_language: str = DEFAULT_LANGUAGE
    output_language: str = DEFAULT_LANGUAGE
    data_language: str = DEFAULT_DATA_LANGUAGE
    week_start_day: str = DEFAULT_WEEK_START
    # The viewer's IANA time zone admitted with the turn ("" = none: channel, scheduled
    # or background start, or an older client). Not a language axis: it rides in this
    # snapshot because it shares the admission-to-resume lifecycle, so live, resumed
    # and continued executions state the same "today" (see ``model_clock``).
    time_zone: str = ""

    def __post_init__(self) -> None:
        if self.ui_language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"unsupported ui_language: {self.ui_language} (supported: {SUPPORTED_LANGUAGES})",
            )
        if self.output_language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"unsupported output_language: {self.output_language}",
            )
        if self.data_language not in SUPPORTED_DATA_LANGUAGES:
            raise ValueError(
                f"unsupported data_language: {self.data_language}",
            )
        if self.week_start_day not in SUPPORTED_WEEK_START:
            raise ValueError(
                f"unsupported week_start_day: {self.week_start_day}",
            )
        if self.time_zone and load_iana_time_zone(self.time_zone) is None:
            raise ValueError(f"unsupported time_zone: {self.time_zone!r}")

    @classmethod
    def default(cls) -> LanguageContext:
        """Use English product copy without changing the source-data language."""
        return cls()

    @classmethod
    def resolve(
        cls,
        *,
        user_settings: dict[str, str] | None = None,
        workspace_settings: dict[str, str] | None = None,
        accept_language: str | None = None,
        user_profile_language: str | None = None,
        input_carrier_language: str | None = None,
        team_language_policy: dict[str, Any] | None = None,
        session_last_language: str | None = None,
        message_language: str | None = None,
    ) -> LanguageContext:
        """按 fallback chain 解析三轴 + week_start_day.

        参数都是 plain dict 或 str，避免对 store 类强依赖（方便 test）.

        `team_language_policy`（批6-C）是团队级 `output_language` 策略；不传 =
        没有策略 = 逐字节沿用旧行为。档位优先级由
        `team_language_policy.effective_output_language_setting` 单点裁决——
        chat 路由判"要不要算提问载体语言"时读的是同一个函数，两处不能各读各的。
        """
        user_settings = user_settings or {}
        workspace_settings = workspace_settings or {}

        # ─── ui_language ────────────────────────────────────────
        # 1. user_profile (request-scoped, highest priority).
        # 2. user_settings.ui_language
        # 3. workspace.default_ui_language
        # Missing interface preferences follow the browser/system language.
        # Channels without a browser pass ``message_language`` (the incoming
        # message language, else the session's previous answer language) instead.
        # Unsupported or unavailable languages use English.
        ui = (
            _normalize_or_none(user_profile_language)
            or _normalize_or_none(user_settings.get("ui_language"))
            or _normalize_or_none(workspace_settings.get("default_ui_language"))
            or _normalize_or_none(message_language)
            or _normalize_accept_language(accept_language)
            or DEFAULT_LANGUAGE
        )

        # ─── output_language ────────────────────────────────────
        # Unset (missing or personal "auto") and "follow_input" follow this question's
        # carrier language, then the session's previous answer (then a concrete team
        # default for unset), then the interface.
        raw_output, output_source = output_language_decision(user_settings, team_language_policy)
        output = _resolve_output_language(
            raw_output,
            output_source,
            ui=ui,
            input_carrier_language=input_carrier_language,
            session_last_language=session_last_language,
        )
        if output == "de-DE" and not german_ai_output_enabled():
            # German UI copy remains available, while model output stays on a
            # conservative language until the deployment explicitly admits it.
            output = ui if ui != "de-DE" else "en-US"

        # ─── data_language ──────────────────────────────────────
        # Workspace-level attribute, no user override.
        data = _normalize_or_none(workspace_settings.get("default_data_language")) or DEFAULT_DATA_LANGUAGE

        # ─── week_start_day ─────────────────────────────────────
        wsd_raw = workspace_settings.get("week_start_day")
        wsd = wsd_raw if wsd_raw in SUPPORTED_WEEK_START else DEFAULT_WEEK_START

        return cls(
            ui_language=ui,
            output_language=output,
            data_language=data,
            week_start_day=wsd,
        )

    def to_dict(self) -> dict[str, str | bool]:
        """plain dict 形式，方便塞进 LLMRequest.metadata（避免 cross-package 类依赖）.

        An empty ``time_zone`` is omitted, so snapshots without a viewer zone keep
        the exact shape persisted before the field existed (digests and strict
        round-trip checks compare these dicts).
        """
        data = asdict(self)
        if not self.time_zone:
            data.pop("time_zone")
        return data

    def with_time_zone(self, time_zone: str | None) -> LanguageContext:
        """This context with the viewer zone ``time_zone``; an unknown or empty name clears it."""
        name = (time_zone or "").strip()
        return replace(self, time_zone=name if load_iana_time_zone(name) is not None else "")

    @classmethod
    def from_dict(cls, data: dict[str, str] | None) -> LanguageContext:
        """从 LLMRequest.metadata 提取还原（容错：缺字段走默认）."""
        if not data:
            return cls.default()
        return cls(
            ui_language=data.get("ui_language", DEFAULT_LANGUAGE),
            output_language=data.get("output_language", DEFAULT_LANGUAGE),
            data_language=data.get("data_language", DEFAULT_DATA_LANGUAGE),
            week_start_day=data.get("week_start_day", DEFAULT_WEEK_START),
            # Lenient for the zone: an unknown stored name means "no viewer zone".
            time_zone=_known_time_zone_or_empty(data.get("time_zone")),
        )


def _known_time_zone_or_empty(value: object) -> str:
    return value.strip() if isinstance(value, str) and load_iana_time_zone(value) is not None else ""


def resolve_turn_language(
    *,
    user_settings: dict[str, str] | None = None,
    workspace_settings: dict[str, str] | None = None,
    team_language_policy: dict[str, Any] | None = None,
    accept_language: str | None = None,
    user_profile_language: str | None = None,
    message_text: str = "",
    session_last_language: str | None = None,
    follow_message_when_unset: bool = False,
) -> LanguageContext:
    """一轮对话的语言解析**唯一入口**（web / IM / 任何新入口都走它）。

    ``follow_message_when_unset`` is for channels without browser context (IM
    bridges and scheduled jobs): the message language replaces the browser
    language, so an account without saved language preferences, team default,
    or team lock follows the language it writes in. A message without language
    evidence ("1", numbers, emoji) follows ``session_last_language``, then English.
    Saved account/Space preferences, team defaults/locks, and languages named in
    the message keep their precedence.

    它把三件事绑在一起：档位裁决（`output_language_decision`）、要不要算提问载体语言
    （`needs_input_carrier`）、以及最终解析（`LanguageContext.resolve`）。

    ⚠ **别在调用方重写这三步中的任何一步**：批6-C 就是因为「档位在 A 处判、carrier 在 B 处判」
    出过半生效（团队策略选了 follow_input，carrier 却恒为 None ⇒ 静默回落界面语言）。
    入口多一个，那种半生效就多一次机会 —— 飞书此前干脆连设置都不读（只认 IM 客户端语言），
    同一个人在 web 与 IM 拿到的语言可以不同（README §6 裂缝 3）。
    """
    raw_output, output_source = output_language_decision(user_settings, team_language_policy)
    carrier: str | None = None
    message_language: str | None = None
    needs_carrier = needs_input_carrier(raw_output, output_source)
    if message_text and (needs_carrier or follow_message_when_unset):
        from bowhead_core.effective_language import channel_message_language

        # One strict detector for every surface: weak Latin evidence ("ok", "SKU TOP10")
        # and Chinese with English product names never flip the answer to English;
        # without evidence the previous answer, team default, and interface decide.
        detected = channel_message_language(message_text) or None
        carrier = detected if needs_carrier else None
        if follow_message_when_unset:
            message_language = detected
    if follow_message_when_unset and message_language is None:
        message_language = _normalize_or_none(session_last_language)
    return LanguageContext.resolve(
        user_settings=user_settings,
        workspace_settings=workspace_settings,
        accept_language=accept_language,
        user_profile_language=user_profile_language,
        input_carrier_language=carrier,
        team_language_policy=team_language_policy,
        session_last_language=session_last_language,
        message_language=message_language,
    )


# Key for this turn's language in clarification resume snapshots.
# ⚠ **Do not bump `RESUME_CONTEXT_SCHEMA_VERSION`**: this is a purely additive optional key,
# ignored by old readers; bumping makes **rolled-back old processes reject all new clarifications**
# (newer schema than known ⇒ reject by policy), at far greater cost than benefit.
RESUME_LANGUAGE_KEY = "language_context"
# Viewer zone for resume snapshots whose language snapshot cannot carry it (missing
# snapshot when i18n is disabled). Additive optional key, like RESUME_LANGUAGE_KEY.
RESUME_TIME_ZONE_KEY = "viewer_time_zone"


def add_resume_time_zone(resume_context: dict[str, Any]) -> None:
    """Record the turn's bound viewer zone when the language snapshot does not carry it."""
    from bowhead_core.model_clock import turn_time_zone_name

    zone = turn_time_zone_name()
    language = resume_context.get(RESUME_LANGUAGE_KEY)
    if zone and not (isinstance(language, dict) and language.get("time_zone")):
        resume_context[RESUME_TIME_ZONE_KEY] = zone


def resume_viewer_time_zone(resume_context: Any) -> str:
    """The viewer zone saved with a resume snapshot (language snapshot first), or ``""``."""
    if not isinstance(resume_context, dict):
        return ""
    language = resume_context.get(RESUME_LANGUAGE_KEY)
    if isinstance(language, dict):
        zone = _known_time_zone_or_empty(language.get("time_zone"))
        if zone:
            return zone
    return _known_time_zone_or_empty(resume_context.get(RESUME_TIME_ZONE_KEY))


def resume_language_context(resume_context: Any, *, presentation_locale: str) -> LanguageContext:
    """Language for a same-turn resume whose durable turn records ``presentation_locale``.

    The turn's admitted context (the clarification snapshot under
    ``RESUME_LANGUAGE_KEY``) is used when its interface language matches the durable
    turn locale, so a resumed turn keeps the answer language the live turn used.
    Older snapshots without the key, or a mismatching one, fall back to the locale.
    """
    raw = resume_context.get(RESUME_LANGUAGE_KEY) if isinstance(resume_context, dict) else None
    if isinstance(raw, dict):
        try:
            snapshot = LanguageContext.from_dict(raw)
        except ValueError:
            snapshot = None
        if snapshot is not None and snapshot.ui_language == presentation_locale:
            return snapshot
    # The viewer zone is not a language choice: keep it even when the language falls back.
    return LanguageContext.resolve(accept_language=presentation_locale).with_time_zone(resume_viewer_time_zone(resume_context))


def language_context_snapshot(source: Any) -> dict[str, str | bool] | None:
    """Capture the language context for persistence and same-turn resumption.

    A missing context produces no snapshot; reads then use current defaults.
    Resumed turns keep the captured context instead of resolving preferences
    from a later clarification or UI language change.
    """
    ctx = getattr(source, "language_context", None) if not isinstance(source, LanguageContext) else source
    if ctx is None:
        return None
    try:
        return ctx.to_dict()
    except Exception:  # noqa: BLE001 — duck-typed test doubles must not break clarification persistence.
        return None


def _follow_input_chain(
    *,
    ui: str,
    input_carrier_language: str | None,
    session_last_language: str | None,
    fallback: str | None = None,
) -> str:
    """Follow-the-question chain: this turn's carrier > the session's previous answer > ``fallback`` > ui.

    ★ 中间那一层是批7（口径乙）加的，不是可有可无的润色：乙 之后载体判据对**所有人
    每一轮**生效，于是"判不出"从边角变成主路 —— 用户续轮只回「ok」「继续」「1」时
    没有载体语言，直接回落 ui 就会让**语言在会话中途跳变**（英文对话里突然冒出中文
    答复）。今天这个洞被模型自身 97% 的粘性盖着（116 实测 `prod-corpus-2026-09-08.md`
    §2.2），乙 把语言变成后端确定性判据之后就盖不住了。

    ⚠ 它**不是** D2「会话粘性」的复活：无 UI、无用户可感开关，只在载体判不出时生效，
    永远输给上面每一层（本轮显式点名 / locks / 用户显式设的具体语言 / 本轮载体）——
    用户下一句换语言问，立刻就跟着换。D2 撤销时留档的三条前提正好都满足。

    Args:
        fallback: a concrete team default, ranked below the question and the previous answer.
    """
    return _normalize_or_none(input_carrier_language) or _normalize_or_none(session_last_language) or _normalize_or_none(fallback) or ui


def needs_input_carrier(raw_output: str | None, output_source: str) -> bool:
    """Return whether this turn's answer language depends on the question language.

    It must agree with ``_resolve_output_language``: the chat route and channel
    resolver use it to decide whether to detect the carrier language at all.
    """
    if raw_output == FOLLOW_INPUT:
        return True
    if _normalize_or_none(raw_output):
        # A concrete team default ranks below the question; locks and saved choices above it.
        return output_source == SOURCE_TEAM_DEFAULT
    if not raw_output or raw_output == "auto":
        # Unset follows the question; only a locked "auto" pins the interface language.
        return output_source != SOURCE_LOCK
    return False


def _resolve_output_language(
    raw_output: str | None,
    output_source: str,
    *,
    ui: str,
    input_carrier_language: str | None,
    session_last_language: str | None,
) -> str:
    """Resolve the answer language from the decided preference and its source.

    Unset preferences (no account choice or personal ``auto``) follow the question:
    this question's carrier language, then the session's previous answer, then a
    concrete team default, then the interface. Team locks and saved concrete
    account choices rank above the question; explicit ``follow_input`` follows the
    question without the team default fallback.
    """
    if raw_output == FOLLOW_INPUT:
        return _follow_input_chain(
            ui=ui,
            input_carrier_language=input_carrier_language,
            session_last_language=session_last_language,
        )

    concrete = _normalize_or_none(raw_output)
    if concrete:
        if output_source == SOURCE_TEAM_DEFAULT:
            return _follow_input_chain(
                ui=ui,
                input_carrier_language=input_carrier_language,
                session_last_language=session_last_language,
                fallback=concrete,
            )
        # Locks and saved concrete account choices are explicit choices above the question.
        return concrete

    if not raw_output or raw_output == "auto":
        if output_source == SOURCE_LOCK:
            # A team locked to "auto" pins every answer to the interface language.
            return ui
        # Unset, personal "auto" (decided as unset) and a team default of "auto".
        return _follow_input_chain(
            ui=ui,
            input_carrier_language=input_carrier_language,
            session_last_language=session_last_language,
        )

    # Unsupported stored answer preferences use the English fallback.
    return DEFAULT_LANGUAGE


def _normalize_or_none(raw: str | None) -> str | None:
    """规范化：'auto' / 'follow_input' / 空 → None；受支持语言通过；其他 None."""
    if not raw or raw in ("auto", FOLLOW_INPUT):
        return None
    if raw in SUPPORTED_LANGUAGES:
        return raw
    return None


def _normalize_accept_language(header: str | None) -> str | None:
    """Choose a supported browser language by quality, preserving order on ties."""
    if not header:
        return None
    locales = {"zh": "zh-CN", "en": "en-US", "de": "de-DE", "fr": "fr-FR"}
    candidates: list[tuple[float, int, str]] = []
    for index, item in enumerate(header.split(",")):
        parts = item.strip().split(";")
        primary = parts[0].strip().lower().replace("_", "-").split("-", 1)[0]
        locale = locales.get(primary)
        quality = 1.0
        for parameter in parts[1:]:
            name, _, value = parameter.strip().partition("=")
            if name.lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
                break
        if locale and 0 < quality <= 1:
            candidates.append((quality, -index, locale))
    return max(candidates)[2] if candidates else None
