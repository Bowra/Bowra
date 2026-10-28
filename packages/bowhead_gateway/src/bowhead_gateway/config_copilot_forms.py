"""配置副驾表单注册（tech-design-batch0-backend.md §4 + tech-design-batch05-chat.md §4）.

批 0：scheduled_job。批 0.5：user_appearance / workspace_i18n（一档，chat 轻确认条）。
注册在 create_app() 内无条件执行（registry 幂等 replace 可重入）；routes and tools are always
mounted (the FEATURE_CONFIG_COPILOT gate was removed on 2026-08-18). context/materializer/read 通过 ctx.deps 取 gateway state，
core 不 import gateway；app 闭包是 HTTP 与 chat 两条消费路径的唯一同源注入点（P2-9 拍板）。

tier-1（可 propose）契约：read_accessor 返回须含 "fields" 子字典
{field: {"value": 当前值, "unset": bool}}——config_propose 计算 diff rows 的 from 值来源。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.agent_release_read_store import AgentReleaseReadStore
from bowhead_core.config_copilot import (
    POLICY_GENERATED,
    POLICY_SECRET,
    POLICY_SERVER_OWNED,
    AppliedChange,
    FieldIssue,
    FormContext,
    FormRegistration,
    LiteApplyError,
    MaterializeResult,
    ResolvedTarget,
    TargetResolution,
    register_form,
    unregister_form,
)
from bowhead_core.entitlements import EntitlementDenied
from bowhead_core.language_context import german_ai_output_enabled
from bowhead_core.scheduled_store import MAX_ACTIVE_JOBS_PER_USER
from bowhead_core.scheduler_loop import cron_validation_error, next_run_for_job
from bowhead_core.share_service import grants_for_viewer
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.team_subagent_access import TeamSubagentAccessResolver
from bowhead_core.team_subagent_store import TeamSubagentStore
from bowhead_core.theme_config import (
    KEY_APPEARANCE_THEME_CONFIG,
    KEY_TEAM_THEME_POLICY,
    THEME_FIELDS,
    ThemeConfigError,
    normalize_appearance_theme_config,
    parse_appearance_theme_config,
    parse_team_theme_policy,
    resolve_effective_theme,
)
from bowhead_core.workspace_settings_store import (
    KEY_DEFAULT_DATA_LANGUAGE,
    KEY_WEEK_START_DAY,
)
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway.access_context import AccessContextError, build_access_context
from bowhead_gateway.routes.scheduled import ScheduledJobCreateDTO
from bowhead_gateway.routes.team_subagents import _visible_to as _subagent_visible_to

if TYPE_CHECKING:
    from fastapi import FastAPI

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ME_SENTINEL = "__me__"


class TeamSubagentEditDTO(BaseModel):
    """Fields shared by legacy configuration and revisioned working-copy editors."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(max_length=40)
    description: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(min_length=1, max_length=15000)
    tool_refs: list[str]
    max_iterations: int = Field(ge=1, le=50)
    skills: list[str]
    output_schema: dict[str, Any] | None


async def _visible_subagents(ctx: FormContext) -> tuple[list[Any], dict[str, Any]]:
    store = ctx.deps["user_store"]
    if store is None or not ctx.team_id:
        raise RuntimeError("subagent store unavailable")
    user = await store.get_user(ctx.user_id)
    if user is None or user.team_id != ctx.team_id:
        raise RuntimeError("subagent actor unavailable")
    items = await store.list_team_subagent_summaries(ctx.team_id)
    granted: frozenset[str] = frozenset()
    access = ctx.deps.get("access_control_store")
    if access is not None:
        granted = await grants_for_viewer(access, team_id=ctx.team_id, user_id=ctx.user_id, kind="team_subagent")
    visible = [item for item in items if _subagent_visible_to(item, user, granted)]
    decisions = await TeamSubagentAccessResolver(store.db_path).resolve_many(
        team_id=ctx.team_id,
        actor_user_id=ctx.user_id,
    )
    return visible, decisions


async def _subagent_write_reason(ctx: FormContext, *, requires_mcp: bool) -> str | None:
    """Mirror the HTTP write gate using the DB-fresh team identity from chat."""
    entitlements = ctx.deps.get("entitlements")
    mode = ctx.deps.get("deployment_mode") or "saas"
    if entitlements is None:
        return "entitlement_unavailable" if mode == "local" else None
    try:
        await entitlements.assert_can_run_turn(ctx.team_id)
    except EntitlementDenied as exc:
        return exc.reason
    if requires_mcp and not await entitlements.is_feature_licensed(ctx.team_id, "mcp"):
        return "feature_unlicensed"
    return None


async def _subagent_read(ctx: FormContext) -> dict[str, Any]:
    items, decisions = await _visible_subagents(ctx)
    return {
        "items": [
            {
                "name": item.name,
                "display_name": item.display_name,
                "owned_by_me": bool((decision := decisions.get(item.id)) is not None and decision.resolution.can_manage),
            }
            for item in items
        ]
    }


async def _subagent_resolve(ctx: FormContext, intent: str) -> TargetResolution:
    items, decisions = await _visible_subagents(ctx)
    needle = intent.casefold().strip()
    exact = [item for item in items if item.name.casefold() == needle or (item.display_name or "").casefold() == needle]
    matches = exact or [item for item in items if needle in item.name.casefold() or needle in (item.display_name or "").casefold()]
    if len(matches) == 1:
        item = matches[0]
        decision = decisions.get(item.id)
        target = ResolvedTarget(id=item.id, label=item.display_name or item.name)
        if decision is None or not decision.resolution.can_manage:
            return TargetResolution(matched=target, not_writable_reason="not_owner")
        reason = await _subagent_write_reason(ctx, requires_mcp=bool(getattr(item, "team_tool_count", 0)))
        if reason:
            return TargetResolution(matched=target, not_writable_reason=reason)
        return TargetResolution(matched=target)
    names = tuple((item.display_name or item.name) for item in (matches if matches else items))
    return TargetResolution(candidates=names, ambiguous=len(matches) > 1)


async def _subagent_target_read(ctx: FormContext) -> dict[str, Any]:
    if ctx.target is None:
        raise RuntimeError("subagent target missing")
    visible, decisions = await _visible_subagents(ctx)
    if ctx.target.id not in {item.id for item in visible}:
        raise RuntimeError("subagent target not visible")
    decision = decisions.get(ctx.target.id)
    if decision is None or not decision.resolution.can_manage:
        raise RuntimeError("subagent target not writable")
    item_summary = next(item for item in visible if item.id == ctx.target.id)
    if await _subagent_write_reason(ctx, requires_mcp=bool(getattr(item_summary, "team_tool_count", 0))):
        raise RuntimeError("subagent target not writable")
    store = ctx.deps["user_store"]
    item = await store.get_team_subagent(ctx.team_id, ctx.target.id)
    if item is None:
        raise RuntimeError("subagent target missing")
    release_state = await TeamSubagentStore(store.db_path).release_definition_state(
        team_id=ctx.team_id,
        subagent_id=item.id,
    )
    if release_state is not None and release_state[0] == "revisioned":
        detail = await AgentReleaseReadStore(store.db_path).working_copy(
            team_id=ctx.team_id,
            agent_id=item.id,
            actor_user_id=ctx.user_id,
        )
        source = detail["working_copy"] or detail["live"]
        definition = source["definition"]
        listing = source["listing"]
        grants = definition.get("skill_grants") or []
        refs = definition.get("tool_refs", definition.get("tool_refs_json")) or []
        if isinstance(refs, str):
            refs = json.loads(refs)
        schema = definition.get("output_schema", definition.get("output_schema_json"))
        if isinstance(schema, str):
            schema = json.loads(schema)
        return {
            "display_name": listing.get("display_name") or item.display_name,
            "description": listing.get("description") or item.description,
            "system_prompt": definition.get("system_prompt") or item.system_prompt,
            "tool_refs": refs,
            "max_iterations": definition.get("max_iterations") or item.max_iterations,
            "skills": [
                grant["skill_ref"]["name"]
                for grant in grants
                if isinstance(grant, dict) and isinstance(grant.get("skill_ref"), dict) and isinstance(grant["skill_ref"].get("name"), str)
            ]
            if grants
            else (json.loads(definition["skills_json"]) if isinstance(definition.get("skills_json"), str) else definition.get("skills") or []),
            "output_schema": schema,
        }
    return {
        "display_name": item.display_name,
        "description": item.description,
        "system_prompt": item.system_prompt,
        "tool_refs": item.tool_refs,
        "max_iterations": item.max_iterations,
        "skills": item.skills,
        "output_schema": item.output_schema,
    }


async def _subagent_context(_ctx: FormContext) -> str:
    # context_provider enters the model's system prompt. Names are user-authored;
    # the chat read tool has a sanitizer, but this prompt path does not.
    return "只为用户明确点名且本人拥有的现有智能体起草修改；保存前由服务端重新确认目标和权限。"


async def _subagent_materialize(_ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    return MaterializeResult(normalized=data)


_SCHEDULED_GUIDANCE = """\
- cron：标准 5 段（分 时 日 月 周），周日=0。时间用 24 小时制换算：下午 X 点 = X+12 时，晚上 X 点 = X+12 时（如「下午4点」= 16 时）。
- 一天多个固定时刻是支持的：分钟位固定、小时位逗号列表，如「每天 7 点和 19 点」= '0 7,19 * * *'。
- 平台调度策略（硬约束）：最短间隔 60 分钟（sub-hourly 会被拒绝）；'*/N' 的 N>60 会被拒绝。
  用户需求超出策略时：选一个支持的、最接近的 cron，并把该字段列入 needs_confirm 说明原因
  （如「每 15 分钟」→ '0 * * * *' + needs_confirm）。
- 无法用单个 cron 语义正确表达的周期（如「隔周」）：直接提 clarification 问用户怎么取舍；
  禁止用日/周双限定硬凑（标准 cron 中 日 与 周 同时限定是「或」语义，会造成非预期触发）。
- 禁止 config 为 null 且 clarification 也为 null——要么给出完整 config，要么提出 clarification。
- tz：IANA 时区名；用户未提时区时用 "Asia/Shanghai"。
- query：写成给分析系统的完整自然语言指令（做什么分析、覆盖什么时间范围）。
- name：简短任务名，≤60 字符；避免与「已有任务名」重复。
- notify_channels：用户提到邮箱时填 {"email": ["..."]}；说“发给我/发我邮件”但没给地址时填
  {"email": ["__me__"]}；完全没提通知就填 {}。
- 用户点名了某个空间时，把空间名原样放进 workspace_intent（不要放进 config）。"""


async def _scheduled_context(ctx: FormContext) -> str:
    store = ctx.deps["scheduled_store"]
    user_store = ctx.deps["user_store"]
    jobs = await store.list_for_user(ctx.user_id)
    active = await store.count_active(ctx.user_id)
    spaces = await user_store.list_workspaces_for_user(ctx.user_id, team_id=ctx.team_id)
    lines = [
        f"- 已有任务名：{', '.join(job.name for job in jobs) or '（无）'}",
        f"- 可用空间名：{', '.join(str(item.get('name') or '') for item in spaces) or '（无）'}",
    ]
    if active >= MAX_ACTIVE_JOBS_PER_USER:
        lines.append(f"- 注意：该用户活跃任务已满（{active}/{MAX_ACTIVE_JOBS_PER_USER}），保存会被拒绝。")
    return "\n".join(lines)


async def _scheduled_materialize(ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    issues: list[FieldIssue] = []
    notes: dict[str, str] = {}
    normalized = dict(data)

    error = cron_validation_error(str(normalized.get("cron", "")))
    if error:
        issues.append(FieldIssue(field="cron", code="cron_invalid", message=error))

    channels = normalized.get("notify_channels") or {}
    emails = channels.get("email") if isinstance(channels, dict) else None
    if emails is not None:
        if not isinstance(emails, list):
            issues.append(
                FieldIssue(field="notify_channels", code="notify_shape", message='notify_channels.email must be a list, e.g. {"email": ["a@b.com"]}')
            )
        else:
            # Batch 1-b (review P1-3): chat tools have empty ctx.user_email; __me__ resolution falls back
            # to a user_id DB lookup. Missing row/store -> empty string, using existing email_invalid issues without crashing.
            me_email = ctx.user_email
            if not me_email and _ME_SENTINEL in emails:
                user_store = ctx.deps.get("user_store")
                if user_store is not None:
                    try:
                        user = await user_store.get_user(ctx.user_id)
                        me_email = getattr(user, "email", "") or ""
                    except Exception:
                        me_email = ""
            cleaned: list[str] = []
            for item in emails:
                address = me_email if item == _ME_SENTINEL else str(item)
                if _EMAIL_RE.match(address):
                    if address not in cleaned:
                        cleaned.append(address)
                else:
                    issues.append(
                        FieldIssue(field="notify_channels", code="email_invalid", message=f"invalid email dropped: {item!r}", repairable=False)
                    )
            normalized["notify_channels"] = {"email": cleaned} if cleaned else {}

    store = ctx.deps["scheduled_store"]
    existing = {job.name for job in await store.list_for_user(ctx.user_id)}
    name = str(normalized.get("name") or "")
    if name in existing:
        suffix = 2
        # R3 review P2-11: check duplicates after truncating to 60, preventing long-name truncation collisions.
        candidate = f"{name[:55]} ({suffix})"[:60]
        while candidate in existing:
            suffix += 1
            candidate = f"{name[:55]} ({suffix})"[:60]
        normalized["name"] = candidate
        notes["name"] = f"与已有任务重名，已改为「{candidate}」"

    intent = (ctx.intents.get("workspace") or "").strip()
    if intent:
        user_store = ctx.deps["user_store"]
        spaces = await user_store.list_workspaces_for_user(ctx.user_id, team_id=ctx.team_id)
        matches = [item for item in spaces if str(item.get("name") or "").strip() == intent]
        if len(matches) == 1:
            normalized["workspace_id"] = matches[0]["workspace_id"]
            notes["workspace_id"] = f"已按你的描述选择空间「{intent}」"
        else:
            issues.append(
                FieldIssue(
                    field="workspace_id",
                    code="workspace_intent_unresolved",
                    message=f"空间「{intent}」未找到或有歧义，已保持当前空间",
                    repairable=False,
                )
            )

    return MaterializeResult(normalized=normalized, issues=issues, notes=notes)


async def _visible_scheduled_jobs(ctx: FormContext) -> list[Any]:
    """镜像 routes/scheduled.py:383-392 的逐空间 READ_HISTORY 过滤（tech design 批 0.5 §2.3）.

    401/403/404 → 该空间 job 跳过；503 族（authz_unavailable）→ raise → config_read 的
    read_failed 降级——不把「鉴权服务不可用」静默吞成「没有任务」。user_store 缺失时与
    HTTP 路径的 _require_workspace_access 同语义（无门，scheduled.py:109-111）。
    """
    store = ctx.deps["scheduled_store"]
    jobs = await store.list_for_user(ctx.user_id)
    user_store = ctx.deps.get("user_store")
    if user_store is None:
        return list(jobs)
    access_store = ctx.deps.get("access_control_store")
    allowed: dict[str, bool] = {}
    visible: list[Any] = []
    for job in jobs:
        ws = job.workspace_id
        if ws not in allowed:
            try:
                await build_access_context(
                    user_store=user_store,
                    access_store=access_store,
                    user_id=ctx.user_id,
                    workspace_id=ws,
                    operation=SpaceOperation.READ_HISTORY,
                )
                allowed[ws] = True
            except AccessContextError as exc:
                if exc.status_code in {401, 403, 404}:
                    allowed[ws] = False
                else:
                    raise
        if allowed[ws]:
            visible.append(job)
    return visible


async def _scheduled_read(ctx: FormContext) -> dict[str, Any]:
    """批 0.5 config_read 消费：任务列表摘要（与 GET /jobs 同权限语义）。"""
    from datetime import UTC, datetime

    jobs = await _visible_scheduled_jobs(ctx)
    now = datetime.now(UTC)
    items = []
    for job in jobs:
        nxt = next_run_for_job(job.cron, job.tz, now) if job.status == "active" else None
        items.append({"name": job.name, "cron": job.cron, "tz": job.tz, "status": job.status, "next_run": nxt.isoformat() if nxt else None})
    return {"items": items, "max_active": MAX_ACTIVE_JOBS_PER_USER}


# ─── Batch 0.5: user_appearance (tier 1, tech-design-batch05-chat.md §4.1) ───

_PROFILE_FIELDS = ("colorProfile", "typographyProfile")  # KV stores {"type":"preset","id":…}; form flattens to id.


class AppearanceDraftDTO(BaseModel):
    """外观 8 字段，枚举直映 theme_config.py 的 frozenset；字段名保持 KV JSON 的 camelCase 1:1。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["system", "light", "dark"] | None = None
    colorProfile: Literal["bowhead", "ocean", "forest", "plum", "graphite"] | None = None
    typographyProfile: Literal["bowhead", "system", "readable", "serif-display"] | None = None
    typeScale: Literal["compact", "standard", "large", "extra-large"] | None = None
    density: Literal["comfortable", "compact", "spacious"] | None = None
    radius: Literal["balanced", "sharp", "soft"] | None = None
    motion: Literal["standard", "reduced"] | None = None
    chartPalette: Literal["default", "high-contrast", "colorblind-safe"] | None = None


_APPEARANCE_GUIDANCE = """\
- 全部字段可选：只填用户点名要改的字段，其余留 null（稀疏覆盖，未提及=保持现状）。
- mode：暗色/深色=dark，浅色/亮色=light，跟随系统=system。
- density：紧凑=compact，宽松=spacious，默认=comfortable。
- chartPalette：色觉友好/色盲友好=colorblind-safe，高对比=high-contrast。
- typeScale：大字/大号=large 或 extra-large，小字=compact。
- colorProfile/typographyProfile 填预设 id（见环境事实的可选值）。"""


def _flat_profile(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else value


async def _appearance_resolved(ctx: FormContext) -> dict[str, Any]:
    user_settings_store = ctx.deps["user_settings_store"]
    team_settings_store = ctx.deps["team_settings_store"]
    raw_user = await user_settings_store.get(ctx.user_id, KEY_APPEARANCE_THEME_CONFIG)
    try:
        user_cfg = parse_appearance_theme_config(raw_user) if raw_user else {"version": 1}
    except ThemeConfigError:
        user_cfg = {"version": 1}  # Same tolerance as routes/theme.py:_parse_user_config.
    raw_policy = await team_settings_store.get(ctx.team_id or "", KEY_TEAM_THEME_POLICY) if ctx.team_id else None
    try:
        policy = parse_team_theme_policy(raw_policy) if raw_policy else {"version": 1}
    except ThemeConfigError:
        policy = {"version": 1}
    return resolve_effective_theme(policy, user_cfg)


async def _appearance_context(ctx: FormContext) -> str:
    resolved = await _appearance_resolved(ctx)
    effective = {f: _flat_profile(resolved["effective"].get(f)) for f in THEME_FIELDS}
    lines = [
        f"- 当前生效外观：{json.dumps(effective, ensure_ascii=False)}",
        "- 可选值：mode=system|light|dark；colorProfile=bowhead|ocean|forest|plum|graphite；"
        "typographyProfile=bowhead|system|readable|serif-display；typeScale=compact|standard|large|extra-large；"
        "density=comfortable|compact|spacious；radius=balanced|sharp|soft；motion=standard|reduced；"
        "chartPalette=default|high-contrast|colorblind-safe",
    ]
    if resolved["locks"]:
        lines.append(f"- 被团队策略锁定（个人不可改）：{', '.join(sorted(resolved['locks']))}")
    return "\n".join(lines)


async def _appearance_materialize(ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    issues: list[FieldIssue] = []
    patch = {k: v for k, v in data.items() if v is not None}
    if patch:
        sparse: dict[str, Any] = {"version": 1}
        for field, value in patch.items():
            sparse[field] = {"type": "preset", "id": value} if field in _PROFILE_FIELDS else value
        try:
            # Real round-trip validation: schema blocks enum errors; this catches combination/shape regressions (materializers must run real validators).
            normalize_appearance_theme_config(json.dumps(sparse, ensure_ascii=False))
        except ThemeConfigError as exc:
            # Reject entirely: normalized must be empty (implementation review P2-2: consumers treat a returned original patch as valid).
            return MaterializeResult(normalized={}, issues=[FieldIssue(field="_root", code="theme_invalid", message=str(exc))])
        resolved = await _appearance_resolved(ctx)
        for field in sorted(set(patch) & set(resolved["locks"])):
            patch.pop(field)
            issues.append(FieldIssue(field=field, code="team_locked", message="该字段被团队策略锁定，个人设置不会生效", repairable=False))
    return MaterializeResult(normalized=patch, issues=issues)


async def _appearance_read(ctx: FormContext) -> dict[str, Any]:
    resolved = await _appearance_resolved(ctx)
    effective = {f: _flat_profile(resolved["effective"].get(f)) for f in THEME_FIELDS}
    user_cfg = resolved["userConfig"]
    return {
        "effective": effective,
        "source": resolved["source"],
        "locks": resolved["locks"],
        "user_overrides": {f: _flat_profile(user_cfg[f]) for f in THEME_FIELDS if f in user_cfg},
        # Tier-1 propose contract: from = user-visible effective value; unset = no personal override (undo deletes the key).
        "fields": {f: {"value": effective[f], "unset": resolved["source"].get(f) != "user"} for f in THEME_FIELDS},
    }


def _kv_plan(rows: list[dict[str, Any]], op: str) -> list[tuple[str, str | None]]:
    """rows → [(key, 目标值 | None=删除)]。undo 的 from_unset/from=None → 删键回默认。"""
    plan: list[tuple[str, str | None]] = []
    for row in rows:
        key = str(row.get("field") or "")
        if op == "apply":
            plan.append((key, str(row.get("to"))))
        elif row.get("from_unset") or row.get("from") is None:
            plan.append((key, None))
        else:
            plan.append((key, str(row.get("from"))))
    return plan


async def _appearance_lite_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str) -> list[AppliedChange]:
    """字段级合并收进 user_settings_store.update_value 单事务（批 1-a §6.2/§6.3）。

    apply 与 undo 双向都对 profile 字段包回 {"type":"preset","id"}；undo 只回写 rows
    涉及字段（from_unset → 删键），apply 之后设置页做的无关改动不受影响（P1-2 语义）。
    """
    store = ctx.deps["user_settings_store"]

    def mutate(before: str | None) -> str | None:
        try:
            cfg = parse_appearance_theme_config(before) if before else {"version": 1}
        except ThemeConfigError:
            cfg = {"version": 1}
        for field, value in _kv_plan(rows, op):
            if field not in THEME_FIELDS:
                raise LiteApplyError("apply_validation_failed", f"unknown appearance field: {field}")
            if value is None:
                cfg.pop(field, None)
            else:
                cfg[field] = {"type": "preset", "id": value} if field in _PROFILE_FIELDS else value
        cfg["version"] = 1
        try:
            return normalize_appearance_theme_config(json.dumps(cfg, ensure_ascii=False))
        except ThemeConfigError as exc:  # Raise within transaction -> rollback; no partial writes.
            raise LiteApplyError("apply_validation_failed", str(exc)) from exc

    before, after = await store.update_value(ctx.user_id, KEY_APPEARANCE_THEME_CONFIG, mutate)
    return [AppliedChange(scope="user", key=KEY_APPEARANCE_THEME_CONFIG, before=before, after=after, operation="put")]


# ─── Batch 0.5 wrap-up: user_language (tier 1, user KV, single output_language field) ───


class UserLanguageDraftDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "auto" counts as unset: answers follow the question, with the team default as a
    # fallback when the question gives no language (language_context.py).
    # ui_language is not registered because of the NEXT_LOCALE cookie + reload
    # trap (tech design §4.3).
    output_language: Literal["zh-CN", "en-US", "de-DE", "fr-FR", "auto", "follow_input"] | None = Field(
        default=None,
        description=(
            "Persistent AI-answer language, independent of interface and source-data language. "
            "auto follows the question with the Team default as fallback; follow_input follows the question without that fallback."
        ),
    )


_USER_LANGUAGE_GUIDANCE = """\
- Change only an explicitly requested persistent answer preference; a one-turn language instruction is not a saved preference.
- Use the read receipt's allowed values; deployment policy may restrict the schema's language choices.
- There is no "follow the interface language" value. Explain this limit when requested; never substitute a different setting."""

_OUTPUT_LANGUAGE_KEY = "output_language"


async def _user_language_current(ctx: FormContext) -> str | None:
    store = ctx.deps["user_settings_store"]
    return await store.get(ctx.user_id, _OUTPUT_LANGUAGE_KEY)


async def _user_language_context(ctx: FormContext) -> str:
    current = await _user_language_current(ctx)
    shown = current if current is not None else "auto (default)"
    return (
        f"- 当前输出语言：{shown}\n"
        "- 可选值：zh-CN|en-US|de-DE|fr-FR|auto|follow_input "
        "(auto=question language, team default as fallback; follow_input=question language without team fallback)"
    )


async def _user_language_materialize(ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    return MaterializeResult(normalized={k: v for k, v in data.items() if v is not None})


async def _user_language_read(ctx: FormContext) -> dict[str, Any]:
    current = await _user_language_current(ctx)
    return {
        # Tier-1 propose contract: unset = no personal setting (undo DELETE restores default auto).
        "fields": {_OUTPUT_LANGUAGE_KEY: {"value": current if current is not None else "auto", "unset": current is None}},
        _OUTPUT_LANGUAGE_KEY: {"value": current if current is not None else "auto", "is_default": current is None},
    }


_OUTPUT_LANGUAGE_VALUES = {"zh-CN", "en-US", "de-DE", "fr-FR", "auto", "follow_input"}


async def _user_language_lite_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str) -> list[AppliedChange]:
    """Validate and compare the single language preference inside its write transaction."""
    store = ctx.deps["user_settings_store"]
    if op not in {"apply", "undo"} or len(rows) != 1:
        raise LiteApplyError("apply_validation_failed", "invalid language preference operation")
    plan = _kv_plan(rows, op)
    for key, value in plan:
        if key != _OUTPUT_LANGUAGE_KEY:
            raise LiteApplyError("apply_validation_failed", f"unknown user_language field: {key}")
        if value is not None and value not in _OUTPUT_LANGUAGE_VALUES:
            raise LiteApplyError("apply_validation_failed", f"invalid output_language: {value}")
        if value == "de-DE" and not german_ai_output_enabled():
            raise LiteApplyError(
                "apply_validation_failed",
                "German AI output is not enabled in this deployment",
            )
    entitlements = ctx.deps.get("entitlements")
    if entitlements is not None:
        await entitlements.assert_can_run_turn(ctx.team_id)
    elif ctx.deps.get("deployment_mode") == "local" or ctx.deps.get("edition") == "community":
        raise LiteApplyError("entitlement_unavailable", "settings write authorization unavailable")
    key, value = plan[0]
    row = rows[0]
    expected = (None if row.get("from_unset") else row.get("from")) if op == "apply" else row.get("to")

    def mutate(before: str | None) -> str | None:
        if before != expected and before != value:
            raise LiteApplyError("configuration_baseline_changed", "answer-language preference changed")
        return value

    before, after = await store.update_value(ctx.user_id, key, mutate)
    return [AppliedChange(scope="user", key=key, before=before, after=after, operation="delete" if after is None else "put")]


# ─── Batch 0.5: workspace_i18n (tier 1, space scope, tech-design-batch05-chat.md §4.2) ───


class WorkspaceI18nDraftDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week_start_day: Literal["monday", "sunday"] | None = Field(
        default=None, description="First day of the week for the current Space's weekly grouping."
    )
    default_data_language: Literal["zh-CN", "en-US"] | None = Field(
        default=None,
        description="Language of stored source data; affects translation for every Space member, independently of interface and AI-answer language.",
    )


_WS_I18N_GUIDANCE = """\
- All fields are optional: propose only fields the user requested.
- Source-data language affects translation for all Space members. Verify the intended data language and require user confirmation.
- This form does not admit interface language or personal AI-answer language."""

_WS_I18N_DEFAULTS = {KEY_WEEK_START_DAY: "monday", KEY_DEFAULT_DATA_LANGUAGE: "zh-CN"}  # Same source as language_context.py:37-41.


async def _ws_i18n_current(ctx: FormContext) -> dict[str, str | None]:
    if not ctx.workspace_id:
        raise ValueError("workspace_required: workspace_i18n needs FormContext.workspace_id")
    store = ctx.deps["workspace_settings_store"]
    settings = await store.list_for_workspace(ctx.workspace_id)
    return {key: settings.get(key) for key in _WS_I18N_DEFAULTS}


async def _ws_i18n_context(ctx: FormContext) -> str:
    current = await _ws_i18n_current(ctx)
    shown = {key: (value if value is not None else f"{_WS_I18N_DEFAULTS[key]}（默认）") for key, value in current.items()}
    return f"- 当前空间设置：{json.dumps(shown, ensure_ascii=False)}\n- 可选值：week_start_day=monday|sunday；default_data_language=zh-CN|en-US"


async def _ws_i18n_materialize(ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    patch = {k: v for k, v in data.items() if v is not None}
    notes: dict[str, str] = {}
    if KEY_DEFAULT_DATA_LANGUAGE in patch:
        # Informational note, not an issue: notes pass through propose payload to light confirmation; Lead text must explain scope.
        notes[KEY_DEFAULT_DATA_LANGUAGE] = "空间数据语言影响全空间成员的 LLM 翻译方向"
    return MaterializeResult(normalized=patch, notes=notes)


async def _ws_i18n_read(ctx: FormContext) -> dict[str, Any]:
    current = await _ws_i18n_current(ctx)
    workspace_name: str | None = None
    user_store = ctx.deps.get("user_store")
    if user_store is not None:
        for item in await user_store.list_workspaces_for_user(ctx.user_id, team_id=ctx.team_id):
            if str(item.get("workspace_id") or "") == ctx.workspace_id:
                workspace_name = str(item.get("name") or "") or None
                break
    fields = {key: {"value": value if value is not None else _WS_I18N_DEFAULTS[key], "unset": value is None} for key, value in current.items()}
    return {
        "workspace_id": ctx.workspace_id,
        "workspace_name": workspace_name,
        "fields": fields,
        KEY_WEEK_START_DAY: {"value": fields[KEY_WEEK_START_DAY]["value"], "is_default": fields[KEY_WEEK_START_DAY]["unset"]},
        KEY_DEFAULT_DATA_LANGUAGE: {
            "value": fields[KEY_DEFAULT_DATA_LANGUAGE]["value"],
            "is_default": fields[KEY_DEFAULT_DATA_LANGUAGE]["unset"],
        },
    }


async def _ws_i18n_lite_apply(ctx: FormContext, rows: list[dict[str, Any]], op: str) -> list[AppliedChange]:
    """Validate, then atomically compare/apply the registered Space fields."""
    if not ctx.workspace_id:
        raise LiteApplyError("workspace_required", "workspace_i18n apply needs workspace_id")
    store = ctx.deps["workspace_settings_store"]
    plan = _kv_plan(rows, op)
    for key, value in plan:
        if key not in _WS_I18N_DEFAULTS:
            raise LiteApplyError("apply_validation_failed", f"unknown workspace_i18n field: {key}")
        if value is not None:
            try:
                # Same validator as HTTP PUT (workspace_settings._validate_value; import at call time to avoid cycles).
                from bowhead_gateway.routes.workspace_settings import _validate_value

                _validate_value(key, value)
            except HTTPException as exc:
                raise LiteApplyError("apply_validation_failed", str(exc.detail)) from exc
    expected = {row["field"]: (None if row.get("from_unset") else str(row.get("from"))) if op == "apply" else str(row.get("to")) for row in rows}
    before = await store.compare_and_apply(ctx.workspace_id, {key: (expected[key], value) for key, value in plan})
    if before is None:
        raise LiteApplyError("configuration_baseline_changed", "Space settings changed after the proposal; read again before confirming.")
    return [
        AppliedChange(scope="workspace", key=key, before=before[key], after=value, operation="delete" if value is None else "put")
        for key, value in plan
    ]


# ─── Registration ──────────────────────────────────────────


# ─── Batch 1-d: llm_provider (tech-design-batch1d-llm-provider.md §2/§4/§5/§6) ───

# The host is FE's ConnectDraft: use the UI capability vocabulary (7 terms, split by protocol;
# backend _CAPABILITIES has only 5). Wrong terms produce caps the host cannot render (review P2-6).
_UI_CAPS = ("chat", "tool", "vision", "embed", "rerank", "image_gen", "web_search")
# Match host `connectCaps` (models.tsx) by protocol: azure/custom_http use the four DECLARATIVE_HTTP_CAPS
# terms; anthropic uses three. Giving azure seven terms produces unrenderable capabilities (implementation review).
_DECLARATIVE_CAPS = ("chat", "tool", "vision", "embed")
_CAPS_BY_PROTOCOL = {
    "openai_compatible": _UI_CAPS,
    "anthropic_compatible": ("chat", "tool", "vision"),
    "azure_openai": _DECLARATIVE_CAPS,
    "custom_http": _DECLARATIVE_CAPS,
}
_PROVIDER_PROTOCOLS = tuple(_CAPS_BY_PROTOCOL)
_RUNTIME_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RUNTIME_KEY_STRIP_RE = re.compile(r"[^A-Za-z0-9_.-]+")

_PROVIDER_GUIDANCE = """\
- protocol：openai_compatible（绝大多数厂商的 /v1 兼容端点）｜anthropic_compatible｜azure_openai｜custom_http。认不出就提 clarification，不要猜。
- base_url：**截到 /v1 为止**，不要带 /chat/completions 之类的具体路径。从 curl 的 --url 或文档示例里取。
- name：给这个接入起的短名（会规范化成 runtime_key），只能用字母数字和 _ . -；不要与「已有 provider」重名。
- auth_type：curl 里是 `-H "Authorization: Bearer xxx"` → bearer；是自定义头（如 `-H "x-api-key: xxx"`）→ api_key 并把头名填进 auth_header_name。
- api_key：**资料里的密钥已被替换成 [REDACTED]，不要填进任何字段，也不要编造**。这个字段由用户自己填。
- models[].id：从 curl 的 `"model": "xxx"` 或文档的模型列表里取；caps 只能用「该 protocol 支持的能力」里列出的值。
- azure_openai 必须给 api_version；custom_http 必须至少给一个 model。
- **DashScope / 通义千问（base_url 含 dashscope.aliyuncs.com）**：给每个 chat 模型加
  request_body_overrides = {"chat": {"enable_thinking": false}}，并在 needs_confirm 里说明
  「该模型默认开启思考模式，非流式单次调用会明显变慢，已默认关闭」。
- 资料里没有的信息不要编造；缺 protocol 或 base_url 且无法合理推断时提 clarification。
"""


class ProviderModelDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    caps: list[str] = []
    deployment: str = ""
    request_body_overrides: dict[str, dict[str, Any]] = {}


class LlmProviderDraftDTO(BaseModel):
    """镜像 ConnectDraft 的可见子集（§1-3）——不含服务端身份概念（provider_id/reserve）。"""

    model_config = ConfigDict(extra="forbid")

    protocol: str = "openai_compatible"
    name: str = ""
    base_url: str = ""
    api_version: str = ""
    messages_path: str = ""
    auth_type: str = "bearer"
    auth_header_name: str = ""
    auth_prefix: str = ""
    models: list[ProviderModelDTO] = []
    api_key: str = ""  # POLICY_SECRET: never model-generated; direct bypass entry.


async def _llm_provider_existing_keys(ctx: FormContext) -> list[str]:
    store = ctx.deps.get("user_store")
    if store is None or not ctx.team_id:
        return []
    try:
        saved, _version = await store.get_team_llm_config_with_version(ctx.team_id)
    except Exception:
        return []
    return sorted((saved or {}).get("providers") or {})


async def _llm_provider_context(ctx: FormContext) -> str:
    keys = await _llm_provider_existing_keys(ctx)
    caps_lines = "\n".join(f"  - {protocol}：{'、'.join(caps)}" for protocol, caps in _CAPS_BY_PROTOCOL.items())
    return (
        f"- 已有 provider 的 name（不要重名）：{'、'.join(keys) if keys else '（无）'}\n"
        f"- 支持的 protocol：{'、'.join(_PROVIDER_PROTOCOLS)}\n"
        f"- 各 protocol 支持的能力：\n{caps_lines}"
    )


def _normalize_runtime_key(name: str) -> str:
    normalized = _RUNTIME_KEY_STRIP_RE.sub("-", (name or "").strip().lower()).strip("-")
    while normalized and not normalized[0].isalnum():
        normalized = normalized[1:]
    return normalized[:80]


async def _llm_provider_materialize(ctx: FormContext, data: dict[str, Any]) -> MaterializeResult:
    """纯本地校验——**不碰网络**（§1-5）。

    materializer 在 `move_on_after` 保护圈之外且在 repair 循环内，任何网络/DNS 调用的
    失败形态是挂住而不是降级（破铁律 3）。真连通性交给用户点「测试连接」。
    """
    normalized = dict(data)
    issues: list[FieldIssue] = []
    notes: dict[str, str] = {}

    protocol = str(normalized.get("protocol") or "").strip()
    if protocol not in _CAPS_BY_PROTOCOL:
        issues.append(FieldIssue(field="protocol", code="protocol_unknown", message=f"protocol 只能是：{'、'.join(_PROVIDER_PROTOCOLS)}"))
        protocol = ""
    normalized["protocol"] = protocol

    runtime_key = _normalize_runtime_key(str(normalized.get("name") or ""))
    if not runtime_key or not _RUNTIME_KEY_RE.match(runtime_key):
        issues.append(FieldIssue(field="name", code="name_invalid", message="name 需以字母或数字开头，只含字母数字和 _ . -"))
    else:
        if runtime_key != (normalized.get("name") or ""):
            notes["name"] = f"已规范化为 {runtime_key}"
        normalized["name"] = runtime_key
        if runtime_key in await _llm_provider_existing_keys(ctx):
            issues.append(FieldIssue(field="name", code="runtime_key_taken", message="该名称已被现有 provider 占用，请换一个"))

    base_url = str(normalized.get("base_url") or "").strip()
    if protocol != "azure_openai" and not base_url:
        issues.append(FieldIssue(field="base_url", code="base_url_required", message="需要 base_url"))
    if base_url:
        if not base_url.startswith(("http://", "https://")):
            issues.append(FieldIssue(field="base_url", code="base_url_invalid", message="base_url 需以 http(s):// 开头"))
        for tail in ("/chat/completions", "/completions", "/messages", "/embeddings"):
            if base_url.rstrip("/").endswith(tail):
                base_url = base_url.rstrip("/")[: -len(tail)]
                notes["base_url"] = f"已去掉末尾的 {tail}（base_url 只到 /v1）"
                break
        normalized["base_url"] = base_url

    if protocol == "azure_openai" and not str(normalized.get("api_version") or "").strip():
        issues.append(FieldIssue(field="api_version", code="api_version_required", message="azure_openai 需要 api_version"))

    auth_type = str(normalized.get("auth_type") or "bearer").strip()
    if auth_type not in ("bearer", "api_key", "secret_header", "none"):
        issues.append(FieldIssue(field="auth_type", code="auth_type_unknown", message="auth_type 只能是 bearer / api_key / secret_header / none"))
    elif auth_type in ("api_key", "secret_header") and not str(normalized.get("auth_header_name") or "").strip():
        issues.append(FieldIssue(field="auth_header_name", code="auth_header_required", message="该认证方式需要提供请求头名称"))

    models = normalized.get("models") or []
    if protocol == "custom_http" and not models:
        issues.append(FieldIssue(field="models", code="model_required", message="custom_http 至少要声明一个模型"))
    allowed_caps = set(_CAPS_BY_PROTOCOL.get(protocol, _UI_CAPS))
    cleaned_models: list[dict[str, Any]] = []
    for index, model in enumerate(models):
        item = dict(model)
        if not str(item.get("id") or "").strip():
            issues.append(FieldIssue(field="models", code="model_id_required", message=f"第 {index + 1} 个模型缺少 id"))
            continue
        bad_caps = [cap for cap in (item.get("caps") or []) if cap not in allowed_caps]
        if bad_caps:
            issues.append(
                FieldIssue(field="models", code="cap_unsupported", message=f"{protocol or '该 protocol'} 不支持能力：{'、'.join(bad_caps)}")
            )
            item["caps"] = [cap for cap in (item.get("caps") or []) if cap in allowed_caps]
        if not item.get("caps"):
            item["caps"] = ["chat"]
        bad_override_keys = [key for key in (item.get("request_body_overrides") or {}) if key not in allowed_caps]
        if bad_override_keys:
            issues.append(
                FieldIssue(
                    field="models",
                    code="override_key_invalid",
                    message=f"request_body_overrides 的键必须是能力名，收到：{'、'.join(bad_override_keys)}",
                )
            )
            item["request_body_overrides"] = {key: value for key, value in (item.get("request_body_overrides") or {}).items() if key in allowed_caps}
        cleaned_models.append(item)
    normalized["models"] = cleaned_models

    return MaterializeResult(normalized=normalized, issues=issues, notes=notes)


async def _llm_provider_read(ctx: FormContext) -> dict[str, Any]:
    """chat `config_read` 的读面（§3-3）——api_key 由 _strip_secrets 在出口置 [REDACTED]。"""
    store = ctx.deps.get("user_store")
    if store is None or not ctx.team_id:
        return {"providers": []}
    saved, _version = await store.get_team_llm_config_with_version(ctx.team_id)
    providers = (saved or {}).get("providers") or {}
    rows = []
    for key, provider in sorted(providers.items()):
        rows.append(
            {
                "name": key,
                "protocol": provider.get("type", ""),
                "base_url": provider.get("base_url") or provider.get("endpoint") or "",
                "models": [model.get("id") for model in (provider.get("models") or []) if isinstance(model, dict)],
            }
        )
    return {"providers": rows}


def register_gateway_forms(app: FastAPI) -> None:
    async def _personal_base_provider(ctx: FormContext):
        from bowhead_gateway.routes.theme import resolve_personal_base_for_user

        store = getattr(app.state, "user_store", None)
        user = await store.get_user(ctx.user_id) if store is not None else None
        if user is None or not ctx.team_id or not await store.user_has_active_team_membership(ctx.user_id, ctx.team_id):
            raise RuntimeError("personal appearance actor unavailable")
        user = replace(user, team_id=ctx.team_id, active_team_id=ctx.team_id)
        base, _ = await resolve_personal_base_for_user(app, team_store=app.state.team_settings_store, user=user)
        return base

    def _bound_deps() -> dict[str, Any]:
        # app.state is ready only during lifespan; retain a lookup closure at registration.
        return {
            "scheduled_store": getattr(app.state, "scheduled_store", None),
            "user_store": getattr(app.state, "user_store", None),
            "user_settings_store": getattr(app.state, "user_settings_store", None),
            "space_instructions_store": getattr(app.state, "space_instructions_store", None),
            "workspace_settings_store": getattr(app.state, "workspace_settings_store", None),
            "team_settings_store": getattr(app.state, "team_settings_store", None),
            "access_control_store": getattr(app.state, "access_control_store", None),
            "entitlements": getattr(app.state, "entitlements", None),
            "deployment_mode": getattr(app.state, "deployment_mode", "saas"),
            "edition": getattr(app.state, "edition", "business"),
            "personal_appearance_service": getattr(app.state, "personal_appearance_service", None),
            "personal_appearance_base_provider": _personal_base_provider,
        }

    def _with_deps(fn):
        async def _wrapped(ctx: FormContext, *args: Any) -> Any:
            ctx.deps.update(_bound_deps())
            return await fn(ctx, *args)

        return _wrapped

    register_form(
        FormRegistration(
            form_id="scheduled_job",
            schema=ScheduledJobCreateDTO,
            guidance=_SCHEDULED_GUIDANCE,
            field_policy={
                "name": POLICY_GENERATED,
                "cron": POLICY_GENERATED,
                "query": POLICY_GENERATED,
                "tz": POLICY_GENERATED,
                "notify_channels": POLICY_GENERATED,
                "workspace_id": POLICY_SERVER_OWNED,
                "session_strategy": POLICY_SERVER_OWNED,
                "anchor_session_id": POLICY_SERVER_OWNED,
                # scheduled-recipe-runs S1: the model **does not generate** saved-workflow bindings. The reference comes from
                # the saved card's structured ID or a user form selection. Model entry fabricates a name, but the bound
                # object executes unattended as the owner (same anti-fabrication discipline as P3 F-B).
                "recipe_ref": POLICY_SERVER_OWNED,
            },
            context_provider=_with_deps(_scheduled_context),
            materializer=_with_deps(_scheduled_materialize),
            risk_tier=2,
            read_accessor=_with_deps(_scheduled_read),
            write_descriptor="POST /api/v1/scheduled/jobs",
            # Batch 1-b: chat draft drawer opt-in (no field is secret; asserted at registration, tech-design-batch1b §2.5).
            chat_draft_supported=True,
        )
    )

    register_form(
        FormRegistration(
            form_id="llm_provider",
            schema=LlmProviderDraftDTO,
            guidance=_PROVIDER_GUIDANCE,
            field_policy={
                "protocol": POLICY_GENERATED,
                "name": POLICY_GENERATED,
                "base_url": POLICY_GENERATED,
                "api_version": POLICY_GENERATED,
                "messages_path": POLICY_GENERATED,
                "auth_type": POLICY_GENERATED,
                "auth_header_name": POLICY_GENERATED,
                "auth_prefix": POLICY_GENERATED,
                "models": POLICY_GENERATED,
                "api_key": POLICY_SECRET,
            },
            context_provider=_with_deps(_llm_provider_context),
            materializer=_with_deps(_llm_provider_materialize),
            risk_tier=2,
            read_accessor=_with_deps(_llm_provider_read),
            write_descriptor="PUT /api/v1/admin/llm-config",
            read_scope="team_admin",
            write_scope="team_admin",
            # Batch 1-d §3-2: extract pasted credentials for direct entry in the same browser (must be disabled for chat).
            secret_bypass_supported=True,
        )
    )

    register_form(
        FormRegistration(
            form_id="user_appearance",
            schema=AppearanceDraftDTO,
            guidance=_APPEARANCE_GUIDANCE,
            field_policy=dict.fromkeys(THEME_FIELDS, POLICY_GENERATED),
            context_provider=_with_deps(_appearance_context),
            materializer=_with_deps(_appearance_materialize),
            risk_tier=1,
            read_accessor=_with_deps(_appearance_read),
            write_descriptor="PUT /api/v1/user-settings/appearance_theme_config",
            lite_applier=_with_deps(_appearance_lite_apply),
        )
    )

    register_form(
        FormRegistration(
            form_id="user_language",
            schema=UserLanguageDraftDTO,
            guidance=_USER_LANGUAGE_GUIDANCE,
            field_policy={_OUTPUT_LANGUAGE_KEY: POLICY_GENERATED},
            context_provider=_with_deps(_user_language_context),
            materializer=_with_deps(_user_language_materialize),
            risk_tier=1,
            read_accessor=_with_deps(_user_language_read),
            write_descriptor="PUT /api/v1/user-settings/output_language",
            lite_applier=_with_deps(_user_language_lite_apply),
        )
    )

    from bowhead_gateway.configuration_personal_appearance import (
        PERSONAL_APPEARANCE_FIELDS,
        PersonalAppearanceDraftDTO,
        personal_appearance_context,
        personal_appearance_lite_apply,
        personal_appearance_materialize,
        personal_appearance_read,
    )
    from bowhead_gateway.configuration_space_instructions import (
        SpaceInstructionsDraftDTO,
        space_instructions_apply,
        space_instructions_context,
        space_instructions_materialize,
        space_instructions_read,
    )
    from bowhead_gateway.configuration_workspace_metadata import (
        WorkspaceMetadataDraftDTO,
        workspace_metadata_apply,
        workspace_metadata_context,
        workspace_metadata_materialize,
        workspace_metadata_read,
    )

    register_form(
        FormRegistration(
            form_id="personal_appearance",
            schema=PersonalAppearanceDraftDTO,
            guidance="Propose only explicit personal appearance scalar choices. Use reset for advertised scalar fields to inherit the Team/product default. No colors or whole-document writes.",
            field_policy=dict.fromkeys(PERSONAL_APPEARANCE_FIELDS, POLICY_GENERATED),
            context_provider=_with_deps(personal_appearance_context),
            materializer=_with_deps(personal_appearance_materialize),
            risk_tier=1,
            read_accessor=_with_deps(personal_appearance_read),
            write_descriptor="PATCH /api/v1/theme/personal",
            lite_applier=_with_deps(personal_appearance_lite_apply),
            reset_fields=PERSONAL_APPEARANCE_FIELDS,
        )
    )
    register_form(
        FormRegistration(
            form_id="workspace_metadata",
            schema=WorkspaceMetadataDraftDTO,
            guidance="Only change the requested current-Space name or description. Empty description clears it. User confirmation is required.",
            field_policy={"name": POLICY_GENERATED, "description": POLICY_GENERATED},
            context_provider=_with_deps(workspace_metadata_context),
            materializer=_with_deps(workspace_metadata_materialize),
            risk_tier=1,
            read_accessor=_with_deps(workspace_metadata_read),
            write_descriptor="PATCH /api/v1/workspace-settings/{workspace_id}/metadata",
            read_scope="ws_admin",
            write_scope="ws_admin",
            lite_applier=_with_deps(workspace_metadata_apply),
            lite_revision_field="revision",
        )
    )
    register_form(
        FormRegistration(
            form_id="space_instructions",
            schema=SpaceInstructionsDraftDTO,
            guidance=(
                "Propose the complete new current-Space instructions text. Keep every existing line the user did not ask to change, "
                "word for word. User confirmation is required; the saved text becomes a new version."
            ),
            field_policy={"content": POLICY_GENERATED},
            context_provider=_with_deps(space_instructions_context),
            materializer=_with_deps(space_instructions_materialize),
            risk_tier=1,
            read_accessor=_with_deps(space_instructions_read),
            write_descriptor="PUT /api/v1/space-instructions/{workspace_id}",
            read_scope="ws_member",
            write_scope="ws_admin",
            lite_applier=_with_deps(space_instructions_apply),
            lite_revision_field="revision",
        )
    )

    register_form(
        FormRegistration(
            form_id="workspace_i18n",
            schema=WorkspaceI18nDraftDTO,
            guidance=_WS_I18N_GUIDANCE,
            field_policy={KEY_WEEK_START_DAY: POLICY_GENERATED, KEY_DEFAULT_DATA_LANGUAGE: POLICY_GENERATED},
            context_provider=_with_deps(_ws_i18n_context),
            materializer=_with_deps(_ws_i18n_materialize),
            risk_tier=1,
            read_accessor=_with_deps(_ws_i18n_read),
            write_descriptor="PUT /api/v1/workspace-settings/{workspace_id}/{key}",
            read_scope="ws_member",
            write_scope="ws_admin",
            lite_applier=_with_deps(_ws_i18n_lite_apply),
        )
    )

    # Batch 3-a is gated at registration: with the flag off the collection
    # branch is unreachable and the existing five forms remain unchanged.
    if os.environ.get("FEATURE_CONFIG_EDIT", "0") == "1":
        register_form(
            FormRegistration(
                form_id="team_subagent",
                schema=TeamSubagentEditDTO,
                guidance=(
                    "只修改用户明确点名的现有子代理。target_intent 填原样名称，不编 ID；"
                    "tool_refs 和 skills 只能选当前授权目录中的条目，权限由保存接口再次校验。"
                    "配置草稿不会直接保存，用户需在右侧检查并点击保存。"
                ),
                field_policy=dict.fromkeys(TeamSubagentEditDTO.model_fields, POLICY_GENERATED),
                context_provider=_with_deps(_subagent_context),
                materializer=_with_deps(_subagent_materialize),
                risk_tier=2,
                read_accessor=_with_deps(_subagent_read),
                write_descriptor="PATCH /api/v1/team-subagents/{id}/configuration (If-Match)",
                chat_draft_supported=True,
                target_resolver=_with_deps(_subagent_resolve),
                target_read_accessor=_with_deps(_subagent_target_read),
                target_noun="子代理",
            )
        )
    else:
        unregister_form("team_subagent")

    from bowhead_gateway.configuration_team_settings import register_team_configuration_forms

    register_team_configuration_forms(_with_deps)
    from bowhead_gateway.configuration_team_models import register_model_configuration_forms

    register_model_configuration_forms(_with_deps)
