"""配置副驾 chat 工具（tech-design-batch05-chat.md §3/§5）：config_read + config_propose.

R6：Lead 读 registry 已注册配置面的当前值回答问题——以当前用户权限执行；
member 问 admin-only 面得到明确「无权限」的结构化正常返回（ok:false dict，
tool_registry channel_not_supported 范式），而非报错或幻觉。
R7：Lead 对一档（risk_tier=1）面提出轻确认条——工具零写入（R4-2 铁律在架构上
不可违）：只校验 patch、算 diff rows、emit ``config.lite_propose``；应用/撤销
发生在用户点击后的前端 → 现有 settings API（写路径零新增）。

Neither tool calls an LLM. Registration is unconditional; the FEATURE_CONFIG_COPILOT
gate was removed on 2026-08-18.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Final

from bowhead_router.types import ToolSchema
from pydantic import ValidationError

from bowhead_core.agents.manifest_security import ManifestSecurityError, lint_user_append
from bowhead_core.config_copilot import (
    MAX_TARGET_CANDIDATES,
    POLICY_GENERATED,
    POLICY_SECRET,
    POLICY_SERVER_OWNED,
    FormContext,
    FormRegistration,
    LiteApplyError,
    ResolvedTarget,
    _apply_field_policy,
    get_form,
    list_forms,
    readable_by,
)
from bowhead_core.events import make_event
from bowhead_core.prompt_injection_defense import ESCAPE_MARKER, has_injection_keyword, sanitize_for_injection
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_core.user_store import _id

logger = logging.getLogger(__name__)

TOOL_READ = "config_read"
TOOL_PROPOSE = "config_propose"

# Multilingual sprint (user decision on 2026-08-28): persist language preferences directly, without a confirmation bar.
#
# Why this surface may break the general rule against applying configuration directly:
# ① The user's words are already an explicit imperative ("以后都用英文回答"); a confirmation bar asks the same thing twice.
# ② Fully reversible, no external side effects: another sentence reverses a mistake, unlike secrets/schedules/members with external consequences.
# ③ Currently the only entrance that independently sets `output_language` and `follow_input`
#    (the profile page that could separately set both axes was removed by `8e6b5b9b9` on 2026-08-28).
# ⚠ Misclassification silently changes settings: hearing "这段用英文写" as "以后都用英文" has no confirmation-bar safeguard.
#   Prompt-side guardrails: `_MEMORY_PROTOCOL` and this tool's description require global wording such as "以后/都/换成"
#   to call it; scoped wording ("这段/这一部分") is followed only this turn, without changing settings.
_AUTO_APPLY_FORMS = frozenset({"user_language"})
_MSG_APPLIED = "已改好并保存。如实告知用户新的取值，别说「请确认」——本次没有确认条。"
_EVENT: Final = "config.lite_propose"  # Bare Final: sse_contract_guard collects only make_event(identifier)
_EVENT_DRAFT: Final = "config.draft_updated"  # Batch 1-b: tier-2 draft drawer (same bare Final discipline)

_REDACTED = "[REDACTED]"

# ── Sanitization seam (3-a prerequisite): three substitutions before others' config text enters Lead context ──
_UNTRUSTED_TEXT_CAP = 2000  # Per-field value; longer text is unusable, and read inventories current state rather than copying full text
_OBJECT_NAME_CAP = 120
_HIDDEN_VALUE = "（内容含诱导措辞，已隐藏）"
_HIDDEN_NAME = "（名称含诱导措辞，已隐藏）"
_TRUNCATED_SUFFIX = "…（内容过长已截断，完整内容请引导用户到设置页查看）"
# Inbound gate recognizes these three markers (`[[escaped:` comes from prompt_injection_defense.sanitize_for_injection)
#: ⚠ Take the escape marker from `prompt_injection_defense`, without hardcoding its literal:
#: `sanitize_for_injection` generates it; if its format changes while this code keeps the old string,
#: this surface's inbound gate silently fails (drift risk identified in final review on 2026-09-08).
#: The other three markers are private to this surface (hidden value/name/truncation suffix); do not share them.
_SANITIZER_MARKS: Final = (ESCAPE_MARKER, _HIDDEN_VALUE, _HIDDEN_NAME, _TRUNCATED_SUFFIX)
_MSG_MARKER_ECHOED = (
    "patch 里含有服务端的安全占位记号（如 [[escaped:…]] 或「已隐藏/已截断」字样）。"
    "那是读取时的展示替换，不是配置原文——不要把它写回配置。"
    "若你只是想改别的字段，就别改这个字段；确需改写它，请让用户到设置页确认原文。"
)

_MSG_UNKNOWN_FORM = "该配置面尚未接入副驾，请引导用户到设置页查看。"
# Branch read-denial copy by read_scope (implementation review P3-3: hardcoded team administrator names the wrong role for ws_admin)
_MSG_DENIED_BY_SCOPE = {
    "team_admin": "该配置面需要团队管理员权限。这是用户权限问题——如实告诉用户需要 owner/team_admin 角色或请管理员代查，不要猜测或编造配置内容。",
    "ws_admin": "该配置面需要空间管理员权限。这是用户权限问题——如实告诉用户需要空间管理员角色或请管理员代查，不要猜测或编造配置内容。",
}
_MSG_READ_FAILED = "读取失败，请引导用户到设置页查看。"
_MSG_CHANNEL = "本通道暂不支持行内确认，请引导用户到网页端会话或设置页修改。"
_MSG_WS_PERMISSION = "修改空间设置需要空间管理员权限。这是用户权限问题——如实告知用户，不要声称可以修改。"
_MSG_PROPOSED = (
    "The suggestion is saved and its confirmation event was emitted. Browser review readiness is not acknowledged. "
    "Ask the user to review it when the original surface is ready, then confirm. Never claim the confirmation is displayed or the setting is saved."
)
_MSG_NOT_DELIVERED = "The confirmation event could not be delivered. Do not claim the review is displayed or the setting is saved."
_MSG_PROPOSE_FAILED = "确认条服务暂不可用，请引导用户到设置页修改。"
_MSG_DRAFT_CREATED = "已在对话右侧打开配置草稿抽屉并填入建议。请用户在抽屉中检查并点击保存；你也可以继续按用户要求修改草稿。不要声称任务已创建。"
_MSG_DRAFT_UPDATED = "已把修改更新到右侧的配置草稿。请用户在抽屉中确认并保存；不要声称任务已创建。"
_MSG_DRAFT_GUARD = "注意：此前的草稿已保存为任务，本次是新的任务草稿——如实告知用户；若其本意是修改已保存的任务，请引导到设置页编辑。"
# Batch 3 (3-0c): collection-surface copy (editing one of N objects). All take target_noun/label parameters;
# the four task-specific constants above are scheduled_job-only (§6-12 requires parameters; frontend i18n remains outstanding).
_MSG_TARGET_REQUIRED = (
    "还不知道要改哪一个{noun}。先用 config_read 看清单，再把用户说的名字**原样**填进 target_intent 重试；不要自己挑一个，也不要编 id。"
)
_MSG_TARGET_NOT_FOUND = (
    "没有找到叫「{intent}」的{noun}。如实告诉用户，并请其从 candidates 里确认要改哪一个（把确认后的名字原样填 target_intent）；不要改别的对象。"
)
_MSG_TARGET_AMBIGUOUS = "有多个{noun}都像「{intent}」。请用户从 candidates 里确认要改哪一个再重试；不要自己挑一个。"
_MSG_TARGET_CHANGED = "草稿在编辑期间已切换目标，本次修改未写入。请确认当前要改的对象后重试。"
_MSG_TARGET_DRAFT_CREATED = "已在对话右侧打开配置草稿抽屉，填入对{noun}「{label}」的修改建议。请用户在抽屉中检查并点击保存；你也可以继续按用户要求修改草稿。不要声称修改已生效。"
_MSG_TARGET_DRAFT_UPDATED = "已把修改更新到右侧{noun}「{label}」的配置草稿。请用户在抽屉中确认并保存；不要声称修改已生效。"
_MSG_TARGET_DRAFT_GUARD = "注意：此前的草稿已保存，本次是新的草稿——如实告知用户。"

# Batch 1-a/1-b: narrow draft-persistence provider; this module may reach only draft-content operations on the returned object
# （create_lite_proposal + create/find_active/find_latest/update_form_draft_fields），
# Never access state changes or write executors: the LLM has no path to apply configuration (R4-2 guard, source-level
# grep pins allowed/forbidden sets, tech-design-batch1b §2.4).
# main.py injects lambda: app.state.config_draft_store at registration; None/returning None means service unavailable.
_proposal_writer_provider: Callable[[], Any] | None = None


def set_proposal_writer_provider(provider: Callable[[], Any] | None) -> None:
    global _proposal_writer_provider
    _proposal_writer_provider = provider


def _readable(reg: FormRegistration, access: dict[str, Any] | None) -> tuple[bool, str]:
    """read_scope 四档——判定上提到框架层（批 1-d：HTTP 端点要用同一份词表语义）。

    `ws_member_preauthorized=True` 是 chat 通道**独有**的前提：turn 建立时已过
    `build_access_context(EXECUTE)` 门，成员身份由构造保证；HTTP 端点没有这个前提。
    """
    return readable_by(reg, access, ws_member_preauthorized=True)


def _proposable(reg: FormRegistration, access: dict[str, Any] | None) -> bool:
    """write_scope 权限预检（避免注定失败的卡）；apply 期现有 endpoint 权限门仍双保险。"""
    if reg.write_scope == "user":
        return True
    if not access:  # ws_admin fails closed
        return False
    if access.get("is_team_admin") or access.get("team_role") in ("owner", "team_admin"):
        return True
    return access.get("workspace_role") == "ws_admin"


def _form_context(ctx: ToolExecutionContext) -> FormContext:
    locale = getattr(getattr(ctx, "language_context", None), "ui_language", None) or "en-US"
    return FormContext(
        user_id=getattr(ctx, "user_id", None) or "",
        user_email="",  # Chat tools have no email; batch 0.5 read/propose consumers do not use it
        team_id=getattr(ctx, "team_id", "") or None,
        locale=locale,
        current_values={},
        workspace_id=getattr(ctx, "workspace_id", "") or None,
        access=getattr(ctx, "access_context", None),
    )


def _allowed_values(reg: FormRegistration) -> dict[str, list[Any]]:
    """generated 字段的合法枚举词表（实现层 review P3-5）：附在 config_read 返回里，
    Lead 提 patch 时直接取值，省一轮 invalid_patch 自修。Literal|None 在 pydantic v2
    的 json schema 里是 anyOf 分支或顶层 enum，两种形状都收。"""
    schema = reg.schema.model_json_schema()
    generated = {name for name, policy in reg.field_policy.items() if policy == POLICY_GENERATED}
    out: dict[str, list[Any]] = {}
    for name, prop in (schema.get("properties") or {}).items():
        if name not in generated or not isinstance(prop, dict):
            continue
        enums: list[Any] = []
        candidates = [prop, *[b for b in prop.get("anyOf", []) if isinstance(b, dict)]]
        for branch in candidates:
            values = branch.get("enum")
            if isinstance(values, list):
                enums.extend(v for v in values if v is not None)
            elif "const" in branch and branch["const"] is not None:
                enums.append(branch["const"])
        if enums:
            out[name] = enums
    return out


def _strip_secrets(value: Any, secret_fields: set[str]) -> Any:
    """secret-free 红线（review P1-3）：递归剔除键名命中 secret 策略字段的值。

    config_read 返回 dict 会经 _serialize 原样进 LLM 上下文——四铁律 4 在 read
    路径的框架级 seam，不依赖各 read_accessor 自觉。
    """
    if isinstance(value, dict):
        return {k: (_REDACTED if k in secret_fields else _strip_secrets(v, secret_fields)) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_secrets(item, secret_fields) for item in value]
    return value


def _structural_injection(text: str) -> bool:
    """结构性注入 lint，**raw + NFKC 双跑**。

    双跑不是保险起见：全角 jailbreak（``ｉｇｎｏｒｅ　ｐｒｅｖｉｏｕｓ…``）在 raw 上不匹配
    ASCII 正则，而**模型自己会把同形字折叠**——peer_block 的 ``_lint_both`` 就是为这个实证
    攻击加的（只 lint raw、却把折叠后的形态喂给模型 = 白 lint）。我们原样吐 raw，所以更得
    按折叠后的形态判。
    """
    for form in (text, unicodedata.normalize("NFKC", text)):
        try:
            lint_user_append(form)
        except ManifestSecurityError:
            return True
    return False


def _sanitize_untrusted_text(value: str, *, form_id: str) -> str:
    """把一段**别人写的**配置文本变成可以进 Lead 上下文的形态（两层，响应不同）。

    为什么需要：`config_read` 的返回体经 `_serialize` 原样进 LLM 上下文（正对照：
    `peer_block.py` 对同类文本有 33 处消毒），而批 3 接的正是 subagent/skill 面——
    那里的 description/system_prompt 是**团队里另一个成员**自由填写的。今天已上线的面
    只有定时任务名（自己写给自己看，自注入），批 3 才是 severity 跳变。

    ① **结构性 lint 命中 → 整值隐藏**：`忽略之前的指令` / `<system>` / `[INST]` 这类在
       配置字段里没有正当用法，判据无歧义，不留原文。
    ② **关键词表命中 → 就地转义**（`[[escaped:充当]]`），**不隐藏**：关键词表含「扮演」
       「充当」「act as」，而这三个是子代理**人设的常用词**——实测 8 条真实人设里误命中 3 条。
       隐藏会把功能做坏，转义既拆掉指令性又留住语义。这正是 peer_block 自己踩过的账
       （`_HIJACK_TOKENS_SCHEMA` 的注释：广告面的全表搬到 schema 面误杀了一片正常中文）。
    ③ 超长截断并**显式说明**——不说明的话 Lead 会拿截断文本当全文去重写，把用户的长 prompt
       改没了（转义同理，故另有入站闸 `_echoes_sanitizer_marker`）。

    ⚠ 已知边界：只处理**值**不处理**键**。这两个面的键是 schema 字段名（服务端词表）；
    唯一用户可写键名的字段（provider `headers` / mcp `env`）是 `secret_value_fields`，
    整棵子树在上游就被 `_strip_secrets` 换成了 sentinel。
    """
    if not value:
        return value
    if _structural_injection(value):
        logger.warning("config_read.injection_hidden form=%s len=%d", form_id, len(value))
        return _HIDDEN_VALUE
    safe = sanitize_for_injection(value)
    if len(safe) > _UNTRUSTED_TEXT_CAP:
        safe = safe[:_UNTRUSTED_TEXT_CAP] + _TRUNCATED_SUFFIX
    return safe


def _sanitize_untrusted(value: Any, *, form_id: str) -> Any:
    """`_strip_secrets` 同款递归（同一条出口上的第二道红线）。"""
    if isinstance(value, str):
        return _sanitize_untrusted_text(value, form_id=form_id)
    if isinstance(value, dict):
        return {k: _sanitize_untrusted(v, form_id=form_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_untrusted(item, form_id=form_id) for item in value]
    return value


def _safe_object_name(name: str, *, form_id: str) -> str | None:
    """对象**名字**通道：不安全就丢，不做就地转义。

    名字太短、且它本身就是被展示的那个东西——一个带诱导措辞的名字**中和不了**
    （peer_block 的 `_sanitize_source_label` 同款判断：label 是广告本身）。
    返回 None = 调用方应丢弃这条候选 / 用占位替换。
    """
    flat = (name or "").strip()
    if not flat:
        return None
    if _structural_injection(flat) or has_injection_keyword(flat)[0]:
        logger.warning("config_copilot.object_name_dropped form=%s", form_id)
        return None
    return flat[:_OBJECT_NAME_CAP]


def _echoes_sanitizer_marker(value: Any) -> bool:
    """入站闸：patch 里出现**我们自己吐出去的**安全记号 = 模型把展示替换当成了配置原文。

    ①转义/②隐藏/③截断三种标记都会随 `config_read` 进模型上下文；模型照抄回来就会把
    `[[escaped:充当]]` 这种记号**存进用户的配置**。这条闸把这个回环堵死——转义策略
    （见 `_sanitize_untrusted_text` ②）之所以敢用，前提就是有这道回收闸。
    """
    if isinstance(value, str):
        return any(mark in value for mark in _SANITIZER_MARKS)
    if isinstance(value, dict):
        return any(_echoes_sanitizer_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_echoes_sanitizer_marker(item) for item in value)
    return False


# Refusal for specialist-owned forms when the specialist is not in this Turn's roster.
# Like the Lead prompt, it states a configuration state and names no absent Agent.
_SETTINGS_UNAVAILABLE: Final[dict[str, Any]] = {
    "ok": False,
    "error": "configuration_settings_unavailable",
    "message": "This team currently has no Agent that can change these saved settings. This is a configuration state, not a fault.",
}


def _specialist_refusal(ctx: ToolExecutionContext) -> dict[str, Any]:
    from bowhead_core.configuration_agent import AGENT_NAME, configuration_agent_in_roster

    if configuration_agent_in_roster(ctx):
        return {"ok": False, "error": "configuration_agent_delegation_required", "profile": AGENT_NAME}
    return dict(_SETTINGS_UNAVAILABLE)


def _legacy_configuration_description(action: str, fallback: str, *, specialist_in_roster: bool = False) -> str:
    """Flag-on copy; only a Turn whose roster holds the specialist may name it."""
    from bowhead_core.configuration_agent import AGENT_NAME, SUPPORTED_FORMS

    delegated = ", ".join(sorted(SUPPORTED_FORMS))
    routing = (
        f"delegate those forms to {AGENT_NAME} through subagent_spawn instead. "
        if specialist_in_roster
        else "this Turn has no Agent that can change them. "
    )
    return (
        f"{action} legacy configuration forms (scheduled_job, team_subagent) only. "
        f"Do not call this Tool for {delegated}; " + routing + "An omitted form_id on config_read lists available forms and their routing. "
        "Read the target and its legal values first; propose only requested fields. "
        "A proposal requires the user's existing confirmation/save action and is never a completed write. "
        "A recurring report with delivery is a scheduling request, not immediate analysis; create its reviewable draft. "
        "For scheduled_job, use the user's name/query, standard five-field cron, IANA tz and explicit notify_channels; "
        'notify_channels={"email":["__me__"]} sends to the user. '
        "For team_subagent, pass the user-provided name as target_intent and let the user review/save its draft. "
        "Return actual receipts and failures; never confirm for the user."
    )


def _read_schema(*, specialist_in_roster: bool = False) -> ToolSchema:
    return ToolSchema(
        name=TOOL_READ,
        description=_legacy_configuration_description(
            "Read",
            (
                "读取当前用户可见的配置现状（定时任务、外观、空间语言/周起始日等已接入配置副驾的面）。"
                "用户问「现在有哪些定时任务在跑 / 我的界面是什么模式」这类配置现状问题时调用。"
                "不带 form_id 先调用一次拿可读配置面列表，再带 form_id 读具体面。"
                "返回 ok:false 且 error=permission_denied 时，如实告诉用户需要相应管理员权限，"
                "禁止猜测或编造配置内容；error=unknown_form 时引导用户到设置页查看。只读，无副作用。"
            ),
            specialist_in_roster=specialist_in_roster,
        ),
        input_schema={
            "type": "object",
            "properties": {
                "form_id": {
                    "type": "string",
                    "maxLength": 80,
                    "description": "要读取的配置面 id；留空则返回全部可读配置面列表。",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )


def _propose_schema(*, specialist_in_roster: bool = False) -> ToolSchema:
    # Batch 1-b (review P1-2): description covers tier-1 confirmation bars and tier-2 draft drawers; ≤1024 characters.
    return ToolSchema(
        name=TOOL_PROPOSE,
        description=_legacy_configuration_description(
            "Propose changes to",
            (
                "发起配置修改。**回答语言（form_id=user_language）直接生效并保存，没有确认条**；"
                "其余面一律永不直接生效，等用户点确认。一档可逆设置（外观 mode/colorProfile/density/typeScale/"
                "radius/motion/chartPalette/typographyProfile；空间 week_start_day/default_data_language；"
                "output_language）：展示行内轻确认条，用户点击后生效——先用 config_read 确认现状与合法"
                "枚举，patch 只含要改字段。**回答语言**：用户说「以后都用英文/中文回答」「换成英文」这类"
                "**持久**语言偏好（「以后都用英文」「换成中文回答」「我用什么语言问就用什么语言答」→ "
                "follow_input）→ MUST 调本工具（form_id=user_language，改 output_language），**直接生效**，"
                "改完如实告知新取值即可，别说「请确认」。只在本轮照着换语言**不算办到**，下次会话就打回原样。"
                "⚠ 带范围限定的（「**这段**用英文写」「这一部分翻成英文」）**不是**持久偏好：本轮照做，别改设置。定时任务（form_id=scheduled_job）：patch 含 name/query/"
                'cron(标准5段)/tz(IANA)/notify_channels（{"email":["__me__"]} 表示发用户本人邮箱；未提'
                "通知则填 {}）——先用 config_read 查看已有任务避免重名；提交后会在对话右侧打开草稿抽屉"
                "（非确认条），用户在抽屉里检查并保存。**带周期+投递的请求**（「每周一早九点出销售周报发我」）"
                "= 建定时任务，**不是现在做一次分析**，别去问要看哪些指标/哪个时间窗。"
                "用户点名某个空间时把空间名原样填 workspace_intent。"
                "修改已有团队子代理（form_id=team_subagent）：先用 config_read 看可见列表，把用户说的"
                "子代理名称原样填 target_intent；patch 只放要改的配置字段。右侧草稿供用户检查并手动保存，"
                "不要声称已更新。"
                "不要代替用户确认，不要声称设置已应用或任务已创建。返回 ok:false 时按 message 如实转告"
                "用户（无权限/通道不支持/已是目标值等），不要编造结果。"
            ),
            specialist_in_roster=specialist_in_roster,
        ),
        input_schema={
            "type": "object",
            "properties": {
                "form_id": {
                    "type": "string",
                    "maxLength": 80,
                    "description": "配置面 id（如 user_appearance / workspace_i18n / scheduled_job）。",
                },
                "patch": {
                    "type": "object",
                    "description": "要修改的字段→目标值；只含用户点名要改的字段。",
                },
                "workspace_intent": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "用户点名的目标空间名（原样填写，服务端负责解析）；未点名则省略。",
                },
                "target_intent": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "修改集合型配置时，用户点名的目标对象名称；原样填写，不填 id。",
                },
            },
            "required": ["form_id", "patch"],
            "additionalProperties": False,
        },
    )


async def _read_handler(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    from bowhead_core.configuration_settings_target import validate_settings_target

    access = getattr(ctx, "access_context", None)
    form_id = str(args.get("form_id") or "").strip()
    from bowhead_core.configuration_agent import AGENT_NAME, SUPPORTED_FORMS, configuration_agent_in_roster

    if ctx.role == "lead" and form_id in SUPPORTED_FORMS:
        return _specialist_refusal(ctx)

    if getattr(ctx, "configuration_settings_target", None) is not None:
        form_id = form_id or "workspace_i18n"
        rejection = await validate_settings_target(ctx, form_id)
        if rejection is not None:
            return rejection
    if not form_id:
        from bowhead_core.configuration_contracts import DOMAINS, domain_enabled

        forms = []
        for fid in list_forms():
            if fid in DOMAINS and not domain_enabled(fid):
                continue
            reg = get_form(fid)
            if reg is None or reg.read_accessor is None:
                continue
            ok, _reason = _readable(reg, access)
            forms.append(
                {
                    "form_id": fid,
                    "read_scope": reg.read_scope,
                    "readable_now": ok,
                    **(
                        ({"delegate_to": AGENT_NAME} if configuration_agent_in_roster(ctx) else {"routing": "configuration_settings_unavailable"})
                        if ctx.role == "lead" and fid in SUPPORTED_FORMS
                        else {}
                    ),
                }
            )
        return {"ok": True, "forms": forms}

    reg = get_form(form_id)
    if reg is None or reg.read_accessor is None:
        return {"ok": False, "error": "unknown_form", "known_forms": list_forms(), "message": _MSG_UNKNOWN_FORM}
    ok, reason = _readable(reg, access)
    if not ok:
        denial = _MSG_DENIED_BY_SCOPE.get(reg.read_scope, _MSG_DENIED_BY_SCOPE["team_admin"])
        return {"ok": False, "error": reason, "form_id": form_id, "message": denial}
    try:
        data = await reg.read_accessor(_form_context(ctx))
    except Exception as exc:
        logger.warning("config_read.failed form=%s error=%s", form_id, type(exc).__name__)
        return {"ok": False, "error": "read_failed", "form_id": form_id, "message": _MSG_READ_FAILED}
    # Batch 1-d review P2-10: strip value-level secrets too; field-level POLICY_SECRET does not cover dict values
    secret_fields = {name for name, policy in reg.field_policy.items() if policy == POLICY_SECRET} | set(reg.secret_value_fields)
    # Both red lines share one exit in fixed order: strip secrets (iron rule 4), then sanitize (others' text must not become instructions).
    # Reversing the order makes the sanitizer scan raw secrets, needlessly exposing them again.
    safe_data = _sanitize_untrusted(_strip_secrets(data, secret_fields), form_id=form_id)
    out: dict[str, Any] = {"ok": True, "form_id": form_id, "data": safe_data}
    allowed = _allowed_values(reg)
    if allowed:
        out["allowed_values"] = allowed  # Values for propose patches (see _propose_schema description)
    return out


def _target_read_shape_violation(current: Any, schema_fields: set[str]) -> str | None:
    """批 3（3-0c）：`target_read_accessor` 返回体的**运行期**形状 chokepoint。

    这条断言注册期做不到（`register_gateway_forms` 是同步函数、无 `FormContext`、
    `app.state` 未就绪——3-0a 的五条断言全是静态内省，r5-P0-5 已改口径），所以打在
    唯一调用点上。契约是 `{field: 当前值}`；写成 `read_accessor` 的**面级**形状
    （tier-1 的 `{"fields": {...}}`、tier-2 `_scheduled_read` 的 `{"items": [...]}`）
    两者同型同缺省，不在这里熔断就会往下走成一张字段全错的草稿。
    """
    if not isinstance(current, dict):
        return f"not_a_dict={type(current).__name__}"
    unknown = sorted(str(k) for k in current if not isinstance(k, str) or k not in schema_fields)
    if unknown:
        return f"unknown_keys={unknown[:5]}"
    return None


async def _resolve_collection_target(
    reg: FormRegistration,
    form_id: str,
    args: dict[str, Any],
    fctx: FormContext,
    active: Any,
) -> tuple[ResolvedTarget | None, dict[str, Any] | None]:
    """批 3（3-0c）：集合型面的「改哪一个」——**服务端**解析（铁律 1：模型只给名字意图）。

    返回 `(target, error)`，恰有一个非 None。四档（§2.3）：命中 → 用；零命中 →
    `target_not_found`；多命中 → `target_ambiguous`；解析器抛 → `propose_failed`
    （与既有 `draft_lookup_failed` 同款结构化降级，不 raise 穿到 turn）。

    ⚠ 任何一档的返回体都**不含 id**——那是铁律 1 的可证伪判据（§6-1）。
    """
    resolver = reg.target_resolver
    assert resolver is not None  # Caller checked is_collection; registration guarantees all four parts
    noun = reg.target_noun
    intent = str(args.get("target_intent") or "").strip()
    if not intent:
        # Follow-up turns (also edit the description) omit the name; reuse the object this session's draft is editing.
        # Otherwise turn two returns target_required, forcing Lead to guess and refill a name.
        prev = (active.payload or {}).get("target") if active is not None else None
        if isinstance(prev, dict) and prev.get("id"):
            return ResolvedTarget(id=str(prev["id"]), label=str(prev.get("label") or "")), None
        return None, {"ok": False, "error": "target_required", "form_id": form_id, "message": _MSG_TARGET_REQUIRED.format(noun=noun)}
    try:
        resolution = await resolver(fctx, intent)
    except Exception as exc:
        logger.warning("config_propose.target_resolve_failed form=%s error=%s", form_id, type(exc).__name__)
        return None, {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}
    if resolution.not_writable_reason:
        reason = resolution.not_writable_reason
        message = {
            "not_owner": "这个对象可以查看，但只有当前 Owner 能修改。",
            "feature_unlicensed": "这个对象可以查看，但团队当前没有修改其工具配置所需的许可。",
            "team_readonly": "团队当前为只读状态，不能保存配置修改。",
        }.get(reason, "这个对象可以查看，但当前环境不能修改，请稍后重试或联系团队负责人。")
        return None, {
            "ok": False,
            "error": "target_not_writable",
            "form_id": form_id,
            "reason": reason,
            "message": message,
        }
    if resolution.matched is not None:
        # Others author object names, which enter Lead copy, draft payload and drawer title. Sanitize once at this
        # sole resolution exit so all three agree (names cannot be neutralized; substitute a placeholder when unsafe).
        safe_label = _safe_object_name(resolution.matched.label, form_id=form_id)
        return replace(resolution.matched, label=safe_label or _HIDDEN_NAME), None
    # candidates must come only from the filtered collection (resolver responsibility, §2.2 tier one). Framework caps again
    # to prevent a resolver feeding a whole table into LLM context; truncation must be disclosed explicitly, never silent.
    # ⚠ Candidate lists are name channels: drop unsafe entries entirely (placeholders cannot answer
    # which object to edit), so candidates may be shorter after filtering than the resolver's list.
    candidates = [safe for safe in (_safe_object_name(str(n), form_id=form_id) for n in resolution.candidates[:MAX_TARGET_CANDIDATES]) if safe]
    # Dropping also makes the list incomplete: Lead cannot present either kind of incomplete list as exhaustive
    truncated = bool(resolution.truncated) or len(candidates) < len(resolution.candidates)
    if truncated:
        logger.info("config_propose.target_candidates_truncated form=%s total=%d kept=%d", form_id, len(resolution.candidates), len(candidates))
    template = _MSG_TARGET_AMBIGUOUS if resolution.ambiguous else _MSG_TARGET_NOT_FOUND
    out: dict[str, Any] = {
        "ok": False,
        "error": "target_ambiguous" if resolution.ambiguous else "target_not_found",
        "form_id": form_id,
        "candidates": candidates,
        "message": template.format(noun=noun, intent=intent),
    }
    if truncated:
        out["truncated"] = True
    return None, out


async def _propose_form_draft(
    reg: FormRegistration,
    form_id: str,
    args: dict[str, Any],
    ctx: ToolExecutionContext,
    *,
    _collection_retry_target_id: str | None = None,
) -> dict[str, Any]:
    """批 1-b（tech-design-batch1b §2.3）：tier-2 面的草稿抽屉链路。

    校验/materializer 与 tier-1 同跑法；repairable issue（cron 非法等模型可修）结构化
    回 Lead 自修；非 repairable（空间歧义/邮箱剔除）转 field_meta needs_confirm。
    草稿 = 「AI 建议真源」，抽屉手改在 FE 本地态；保存走现有表单 API。
    """
    patch = args.get("patch")
    if not isinstance(patch, dict) or not patch:
        return {"ok": False, "error": "empty_patch", "message": "patch 必须是非空对象（只含要改的字段）。"}
    generated = {name for name, policy in reg.field_policy.items() if policy == POLICY_GENERATED}
    unknown = sorted(set(patch) - generated)
    if unknown:
        return {"ok": False, "error": "unknown_field", "fields": unknown, "allowed_fields": sorted(generated)}
    # Inbound gate: model copied config_read safety placeholders as original text (tier-1 cannot structurally reach
    # here: all its fields are Literal enums, whose schemas reject marked values).
    echoed = sorted(name for name, value in patch.items() if _echoes_sanitizer_marker(value))
    if echoed:
        logger.info("config_propose.sanitizer_marker_echoed form=%s fields=%s", form_id, echoed)
        return {"ok": False, "error": "invalid_patch", "fields": echoed, "message": _MSG_MARKER_ECHOED}
    access = getattr(ctx, "access_context", None)
    if not _proposable(reg, access):
        return {"ok": False, "error": "permission_denied", "form_id": form_id, "message": _MSG_WS_PERMISSION}

    writer = _proposal_writer_provider() if _proposal_writer_provider is not None else None
    if writer is None:
        return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}
    team_id = getattr(ctx, "team_id", "") or ""
    user_id = getattr(ctx, "user_id", "") or ""
    session_id = getattr(ctx, "session_id", "") or ""
    fctx = _form_context(ctx)  # Moved up for 3-0c: pure construction; target resolution needs it

    # Fetch the active draft before validation: tier-2 DTOs are full forms (name/query/cron required);
    # incremental patches (change time to ten) must merge with current draft values to pass the schema (observed in dogfood).
    try:
        active = await writer.find_active_form_draft(team_id=team_id, owner_user_id=user_id, session_id=session_id, form_id=form_id)
    except Exception as exc:
        logger.warning("config_propose.draft_lookup_failed form=%s error=%s", form_id, type(exc).__name__)
        return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}

    # ── Batch 3 (3-0c): collection-surface branch. Sole criterion: target_resolver present. Singleton surfaces
    # (batch 0–1's five surfaces + scheduled_job) always use else, preserving byte-for-byte behavior (isolation guard pins this).
    # Resolve after find_active: follow-up turns lacking target_intent need the active draft's target as
    # fallback (handoff originally said before, because target-switch detection depends on resolution; that criterion runs only
    # after both are available, so ordering is unaffected. Without fallback, turn two necessarily returns target_required).
    is_collection = reg.target_resolver is not None
    target: ResolvedTarget | None = None
    switched = False
    if is_collection:
        target, target_error = await _resolve_collection_target(reg, form_id, args, fctx, active)
        if target_error is not None:
            return target_error
        assert target is not None
        if _collection_retry_target_id is not None and target.id != _collection_retry_target_id:
            return {
                "ok": False,
                "error": "target_changed",
                "form_id": form_id,
                "message": _MSG_TARGET_CHANGED,
            }
        fctx.target = target
        prev_target = (active.payload or {}).get("target") if active is not None else None
        # Switching target (§2.5) reuses the same draft but replaces the whole payload; no byte of A's suggestions may enter B.
        # Active drafts without target (legacy/anomalous rows) also count as switches; fail safely by rebuilding the baseline.
        switched = active is not None and (not isinstance(prev_target, dict) or prev_target.get("id") != target.id)

    # Fixed merge order (as in batch 0 §2.7, completed for drafts): current draft ← patch → backfill server_owned
    # (model-invisible fields such as workspace_id) → schema validation. Without backfill, required server_owned fields
    # make every propose invalid_patch, but model-supplied values cause unknown_field: a dogfood-observed loop.
    draft_fields = dict((active.payload or {}).get("fields") or {}) if active is not None else {}
    if is_collection:
        if switched:
            draft_fields = {}
        try:
            current = await reg.target_read_accessor(fctx)  # type: ignore[misc]  # Registration guarantees non-None
        except Exception as exc:
            logger.warning("config_propose.target_read_failed form=%s error=%s", form_id, type(exc).__name__)
            return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}
        violation = _target_read_shape_violation(current, set(reg.schema.model_fields))
        if violation is not None:
            logger.warning("config_propose.target_read_shape form=%s reason=%s", form_id, violation)
            return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}
        # ② Merge baseline = current object values ⊕ active draft (§2.6). Using only drafts for collections, as for singletons,
        # makes first-proposal full-form DTOs invalid_patch, forcing Lead to invent current values (r2-P0-4).
        base_fields = {**current, **draft_fields}
    else:
        base_fields = draft_fields
    candidate = {**base_fields, **patch}
    server_defaults = {name: value for name, value in base_fields.items() if reg.field_policy.get(name) == POLICY_SERVER_OWNED}
    if fctx.workspace_id and "workspace_id" in reg.field_policy:
        server_defaults.setdefault("workspace_id", fctx.workspace_id)
    merged, policy_issues = _apply_field_policy(reg, candidate, server_defaults)
    if policy_issues:
        return {
            "ok": False,
            "error": "form_context_missing",
            "issues": [{"field": i.field, "code": i.code, "message": i.message} for i in policy_issues],
            "message": "服务端缺少必要上下文（如当前空间），请引导用户到设置页创建。",
        }

    try:
        # Users may explicitly clear nullable collection fields (e.g. agent output_schema=None).
        # Reusing singleton exclude_none here erases that edit from the draft and event.
        validated = reg.schema.model_validate(merged).model_dump(mode="json", exclude_none=not is_collection)
    except ValidationError as exc:
        issues = [{"field": ".".join(str(part) for part in error["loc"]) or "_root", "message": error["msg"]} for error in exc.errors()]
        return {
            "ok": False,
            "error": "invalid_patch",
            "issues": issues,
            "message": "按字段说明补齐/修正 patch 后重试（只填你要改的字段即可，其余沿用草稿现值）。",
        }
    if not validated:
        return {"ok": False, "error": "empty_patch", "message": "patch 未包含任何有效字段值。"}
    intent = str(args.get("workspace_intent") or "").strip()
    if intent:
        # Iron rule 1: model supplies only Space-name intent; server materializer resolves by name within accessible Spaces (P1-1)
        fctx.intents["workspace"] = intent
    schema_fields = set(reg.schema.model_fields)
    try:
        result = await reg.materializer(fctx, validated)
    except Exception as exc:
        logger.warning("config_propose.draft_materialize_failed form=%s error=%s", form_id, type(exc).__name__)
        return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}

    # Repairable (model-fixable, e.g. invalid cron) or outside schema (_root global rejection) → structured feedback for Lead self-repair;
    # nonrepairable (Space ambiguity/email removal) → field_meta.needs_confirm in the drawer.
    blocking = [i for i in result.issues if i.repairable or i.field == "_root" or i.field not in schema_fields]
    if blocking:
        return {
            "ok": False,
            "error": "invalid_patch",
            "issues": [{"field": i.field, "code": i.code, "message": i.message} for i in blocking],
            "message": "配置未通过服务端校验，按 issues 修正后重试。",
        }
    normalized = {k: v for k, v in result.normalized.items() if k in schema_fields}
    cumulative: list[str] | None = None
    if is_collection:
        # ★ Projection is cumulative, not current-turn-only (§2.4 reversed this; r5-P0-1 proved it with builtin full PUT:
        # current-turn-only rewrites turn one's edits to original values in turn two). Cumulative = union of prior-turn patch keys:
        # do not persist untouched current values (old body/new etag, r1-P1-1), and retain all previous edits.
        # Missing key set falls back to draft fields' keys: §4.4 defines cumulative as equivalent to that set.
        # Without fallback, a legacy draft missing the key gives FE an overlay set narrower than fields, omitting real edits.
        prev_payload = (active.payload or {}) if (active is not None and not switched) else {}
        prev_cumulative = list(prev_payload.get("changed_fields_cumulative") or prev_payload.get("fields") or {})
        cumulative = sorted({*prev_cumulative, *patch})
        # ⚠ Projection applies only to collections (r5-P0-2): unconditional projection empties scheduled_job name/query
        # in turn two's drawer and causes invalid_patch in turn three. Patches allow only GENERATED keys, so server_owned
        # is always projected away, but ScheduledJobCreateDTO requires those four fields.
        normalized = {k: v for k, v in normalized.items() if k in cumulative}
    secret_fields = {name for name, policy in reg.field_policy.items() if policy == POLICY_SECRET}
    # Pre-emit defense (review P1-4): fields persist with events in turn_log; reject keys outside schema or secret keys immediately
    if set(normalized) - schema_fields or set(normalized) & secret_fields:
        logger.warning("config_propose.draft_fields_violation form=%s", form_id)
        return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}

    issue_by_field = {i.field: i for i in result.issues}
    # meta covers normalized ∪ schema fields with issues, preserving hints for unresolved fields such as ambiguous Spaces
    meta_fields = set(normalized) | (set(issue_by_field) & schema_fields)
    field_meta = {
        name: {
            "origin": "generated" if reg.field_policy.get(name, POLICY_GENERATED) == POLICY_GENERATED else "server",
            "needs_confirm": name in issue_by_field,
            "note": (issue_by_field[name].message if name in issue_by_field else result.notes.get(name)),
        }
        for name in meta_fields
    }
    workspace_id = normalized.get("workspace_id") or (getattr(ctx, "workspace_id", "") or None)

    # Persist target with only id/label (§4.4), no etag; FE refetches on opening confirmation,
    # because combining the old persisted etag with a new body is exactly r1-P1-1's failure.
    target_payload = {"id": target.id, "label": target.label} if target is not None else None

    created = False
    draft = None
    guard_needed = False  # consumed guard (review P2-3): check before creation; find_latest afterward sees only this draft
    try:
        if active is None and session_id:
            latest = await writer.find_latest_form_draft(team_id=team_id, owner_user_id=user_id, session_id=session_id, form_id=form_id)
            guard_needed = latest is not None and latest.status == "consumed"
        for attempt in (1, 2):
            if active is None:
                draft = await writer.create_form_draft(
                    draft_id=_id("ccd"),
                    team_id=team_id,
                    owner_user_id=user_id,
                    form_id=form_id,
                    workspace_id=workspace_id,
                    turn_id=getattr(ctx, "turn_id", "") or None,
                    session_id=session_id or None,
                    fields=normalized,
                    field_meta=field_meta,
                    notes=dict(result.notes),
                    target=target_payload,
                    changed_fields_cumulative=cumulative,
                )
                created = True
                break
            payload = active.payload or {}
            if switched:
                # Target switch: do not merge old-target content (§2.5 whole-payload replacement).
                merged_fields, merged_meta, merged_notes = normalized, field_meta, dict(result.notes)
            else:
                merged_fields = {**(payload.get("fields") or {}), **normalized}
                merged_meta = {**(payload.get("field_meta") or {}), **field_meta}
                merged_notes = {**(payload.get("notes") or {}), **result.notes}
            update = await writer.update_form_draft_fields(
                active.id,
                team_id=team_id,
                owner_user_id=user_id,
                expected_version=active.row_version,
                fields=merged_fields,
                field_meta=merged_meta,
                notes=merged_notes,
                # ⚠ Updates replace the entire payload: omitted values disappear (pinned by 3-0b contract tests),
                # causing r1-P1-2's target always None from turn three onward, silently disabling switch detection.
                target=target_payload,
                changed_fields_cumulative=cumulative,
            )
            if update.outcome == "ok":
                draft = update.draft
                break
            # CAS conflict dual diagnosis (review P2-2): still draft = concurrent propose bumped first → reread and remerge
            # once; terminal/not_found → create anew. Conflict again → abandon (propose_failed).
            if attempt == 1 and update.draft is not None and update.draft.status == "draft":
                if is_collection:
                    # Collection projection includes cumulative prior-turn keys; retrying pre-conflict normalized values overwrites
                    # another turn's new edits. Merge again from latest draft/current object values, pinning the original target.
                    if _collection_retry_target_id is not None:
                        return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}
                    assert target is not None
                    latest_target = (update.draft.payload or {}).get("target")
                    if not isinstance(latest_target, dict) or latest_target.get("id") != target.id:
                        return {
                            "ok": False,
                            "error": "target_changed",
                            "form_id": form_id,
                            "message": _MSG_TARGET_CHANGED,
                        }
                    return await _propose_form_draft(
                        reg,
                        form_id,
                        args,
                        ctx,
                        _collection_retry_target_id=target.id,
                    )
                active = update.draft
                continue
            if update.draft is not None and update.draft.status == "consumed":
                guard_needed = True  # Guard terminal state when consume wins too
            active = None
            if is_collection:
                # Creating a new row means a new draft: reset cumulative keys to this turn. The old draft is consumed (=saved),
                # so its prior edits already exist in object values; retaining them in cumulative overwrites the object with stale values.
                cumulative = sorted(patch)
                normalized = {k: v for k, v in normalized.items() if k in cumulative}
        if draft is None:
            raise RuntimeError("config_draft_upsert_unsettled")
    except Exception as exc:
        logger.warning("config_propose.draft_persist_failed form=%s error=%s", form_id, type(exc).__name__)
        return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}

    if target is not None:
        # Collections cannot use the three task-specific strings (§6-12 parameters; frontend i18n still outstanding for 3-a)
        message = (_MSG_TARGET_DRAFT_CREATED if created else _MSG_TARGET_DRAFT_UPDATED).format(noun=reg.target_noun, label=target.label)
        if created and guard_needed:
            message += _MSG_TARGET_DRAFT_GUARD
    else:
        message = _MSG_DRAFT_CREATED if created else _MSG_DRAFT_UPDATED
        if created and guard_needed:
            message += _MSG_DRAFT_GUARD

    bus = getattr(ctx, "parent_bus", None)
    if bus is None:
        return {"ok": False, "error": "not_delivered", "message": _MSG_NOT_DELIVERED, "draft_id": draft.id}
    draft_payload = draft.payload or {}
    payload = {
        "draft_id": draft.id,
        # ★beat anchor (as in batch 0.5 review P0-1): FE dispatcher sees payload, not the event envelope
        "turn_id": getattr(ctx, "turn_id", "") or "",
        "form_id": form_id,
        "created": created,
        "updated_fields": sorted(patch),  # This turn's edits drive drawer highlights; fields is the merged full form
        "fields": draft_payload.get("fields") or normalized,
        "field_meta": draft_payload.get("field_meta") or field_meta,
        "notes": draft_payload.get("notes") or dict(result.notes),
        "workspace_id": draft.workspace_id,
    }
    if target_payload is not None:
        # Singleton payloads keep the original four keys+envelope byte-for-byte; only collections add these two keys (§6-9 emit contract).
        # cumulative is FE's overlay key set (§4.4): refetch detail when opening confirmation and overlay only these keys.
        payload["target"] = draft_payload.get("target") or target_payload
        payload["changed_fields_cumulative"] = draft_payload.get("changed_fields_cumulative") or cumulative
    delivered = await bus.emit(make_event(_EVENT_DRAFT, payload["turn_id"], payload))
    if delivered is False:
        # Draft persisted (harmless orphan); no card delivered → report degradation honestly
        return {"ok": False, "error": "not_delivered", "message": _MSG_NOT_DELIVERED, "draft_id": draft.id}
    return {"ok": True, "draft_id": draft.id, "updated_fields": sorted(patch), "message": message}


async def _propose_handler(
    args: dict[str, Any],
    ctx: ToolExecutionContext,
    *,
    expected_data: dict[str, Any] | None = None,
    prepare_only: bool = False,
    emit_confirmation: bool = True,
) -> dict[str, Any]:
    from bowhead_core.configuration_settings_target import validate_settings_target

    if not getattr(ctx, "config_lite_supported", False):
        return {"ok": False, "error": "channel_not_supported", "message": _MSG_CHANNEL}

    form_id = str(args.get("form_id") or "").strip()
    from bowhead_core.configuration_agent import SUPPORTED_FORMS

    if ctx.role == "lead" and form_id in SUPPORTED_FORMS:
        return _specialist_refusal(ctx)

    rejection = await validate_settings_target(ctx, form_id)
    if rejection is not None:
        return rejection
    reg = get_form(form_id)
    if reg is None:
        return {"ok": False, "error": "unknown_form", "known_forms": list_forms(), "message": _MSG_UNKNOWN_FORM}
    if reg.risk_tier != 1:
        # Batch 1-b (tech-design-batch1b §2.3): opted-in tier-2 surfaces use the draft drawer;
        # others retain prior rejection (secret surfaces require design first, review P1-4).
        if not reg.chat_draft_supported:
            return {
                "ok": False,
                "error": "tier_not_supported",
                "message": "该配置面的 chat 确认面尚未上线，请引导用户到设置页使用智能填写。",
            }
        return await _propose_form_draft(reg, form_id, args, ctx)

    patch = args.get("patch", {})
    reset = args.get("reset", [])
    reset_all = args.get("reset_all", False)
    if not isinstance(patch, dict) or not isinstance(reset, list) or any(not isinstance(key, str) for key in reset) or type(reset_all) is not bool:
        return {"ok": False, "error": "invalid_patch", "message": "Use a JSON object for patch and field names for reset."}
    if (
        set(reset) - reg.reset_fields
        or set(reset) & set(patch)
        or len(set(reset)) != len(reset)
        or (reset_all and (not reg.reset_all or patch or reset))
    ):
        return {
            "ok": False,
            "error": "invalid_configuration_reset",
            "message": "Use only advertised reset fields, separately from setting the same fields.",
        }
    if reset_all:
        reset = list(reg.schema.model_fields)
    if not patch and not reset:
        return {"ok": False, "error": "empty_patch", "message": "Specify a changed field or an advertised reset operation."}
    generated = {name for name, policy in reg.field_policy.items() if policy == POLICY_GENERATED}
    unknown = sorted(set(patch) - generated)
    if unknown:
        # Fail closed (as in _apply_field_policy): reject unknown keys together; return allowed fields for Lead self-repair
        return {"ok": False, "error": "unknown_field", "fields": unknown, "allowed_fields": sorted(generated)}

    access = getattr(ctx, "access_context", None)
    if not _proposable(reg, access):
        return {"ok": False, "error": "permission_denied", "form_id": form_id, "message": _MSG_WS_PERMISSION}

    fctx = _form_context(ctx)
    if reg.typed_patch and _echoes_sanitizer_marker(patch):
        return {
            "ok": False,
            "error": "sanitized_value_not_editable",
            "message": "The read contains abbreviated or escaped values. Edit this setting on its original page to preserve the complete value.",
        }
    try:
        if reg.typed_patch:
            initial = await reg.read_accessor(fctx)
            values = {key: info["value"] for key, info in initial.get("fields", {}).items() if key in generated}
            validated_all = reg.schema.model_validate({**values, **patch}, strict=True).model_dump(mode="json", exclude_unset=True)
            validated = {key: validated_all[key] for key in patch}
        else:
            validated = reg.schema.model_validate(patch).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        issues = [{"field": ".".join(str(part) for part in error["loc"]) or "_root", "message": error["msg"]} for error in exc.errors()]
        return {"ok": False, "error": "invalid_patch", "issues": issues, "message": "按合法枚举值修正 patch 后重试。"}
    if not validated and not reset:
        return {"ok": False, "error": "empty_patch", "message": "patch 未包含任何有效字段值。"}

    try:
        result = await reg.materializer(fctx, validated)
        # Implementation review P2-2: issues outside validated keys (e.g. _root) mean materializer global rejection;
        # return structured feedback to Lead, or silent admission creates a card whose apply inevitably returns 400 (batch 1 contract guard).
        global_issues = [i for i in result.issues if i.field not in validated]
        if global_issues:
            return {
                "ok": False,
                "error": "invalid_patch",
                "issues": [{"field": i.field, "code": i.code, "message": i.message} for i in global_issues],
                "message": "配置未通过服务端校验，按 issues 修正后重试。",
            }
        normalized = {k: v for k, v in result.normalized.items() if k in validated}
        dropped = sorted(set(validated) - set(normalized))  # Fields removed by materializer (e.g. team_locked)
        if dropped and not normalized:
            return {
                "ok": False,
                "error": "all_fields_locked",
                "issues": [{"field": i.field, "code": i.code, "message": i.message} for i in result.issues],
                "message": "这些字段被团队策略锁定，无法由个人修改——如实告知用户。",
            }
        current = await reg.read_accessor(fctx) if reg.read_accessor else {}
    except Exception as exc:
        logger.warning("config_propose.materialize_failed form=%s error=%s", form_id, type(exc).__name__)
        return {"ok": False, "error": "propose_failed", "message": _MSG_READ_FAILED}

    if expected_data is not None:
        secret_fields = {name for name, policy in reg.field_policy.items() if policy == POLICY_SECRET}
        safe_current = _sanitize_untrusted(_strip_secrets(current, secret_fields), form_id=form_id)
        if safe_current != expected_data:
            return {
                "ok": False,
                "error": "configuration_baseline_changed",
                "message": "Settings changed during generation. Read again before proposing.",
            }

    fields_info = current.get("fields") if isinstance(current.get("fields"), dict) else {}
    for key in reset:
        info = fields_info.get(key, {})
        if "default" not in info:
            return {"ok": False, "error": "configuration_reset_unavailable", "field": key}
        normalized[key] = info["default"]
    rows: list[dict[str, Any]] = []
    for fname, to_value in normalized.items():
        info = fields_info.get(fname) if isinstance(fields_info.get(fname), dict) else {}
        from_value = info.get("value")
        if from_value == to_value and (fname not in reset or info.get("unset")):
            continue  # Exclude no-op rows
        rows.append(
            {
                "field": fname,
                "from": from_value,
                "to": to_value,
                "from_unset": bool(info.get("unset", False)),
                **({"operation": "reset", "reset_all": reset_all} if fname in reset else {}),
                **({"label": reg.schema.model_fields[fname].title or reg.schema.model_fields[fname].description or fname} if reg.typed_patch else {}),
                **({"baseline_revision": current[reg.lite_revision_field]} if reg.lite_revision_field else {}),
            }
        )
    if not rows:
        return {"ok": False, "error": "already_set", "message": "这些设置已是目标值，如实告知用户即可，不需要确认条。"}

    notes = {k: v for k, v in result.notes.items() if k in {row["field"] for row in rows}}

    # Direct-apply surfaces (rationale in _AUTO_APPLY_FORMS): no proposal, no card, no bus required.
    if not prepare_only and form_id in _AUTO_APPLY_FORMS and reg.lite_applier is not None:
        try:
            await reg.lite_applier(fctx, rows, "apply")
        except LiteApplyError as exc:
            return {"ok": False, "error": exc.code, "message": "The preference was not saved. Read its current value before trying again."}
        except Exception as exc:
            logger.warning("config_propose.auto_apply_failed form=%s error=%s", form_id, type(exc).__name__)
            return {"ok": False, "error": "apply_failed", "message": "设置没能保存，如实告知用户，别说已改好。"}
        applied: dict[str, Any] = {"ok": True, "applied": True, "form_id": form_id, "rows": rows, "message": _MSG_APPLIED}
        if notes:
            applied["notes"] = notes
        return applied

    bus = getattr(ctx, "parent_bus", None)
    if emit_confirmation and bus is None:
        return {"ok": False, "error": "not_delivered", "message": _MSG_NOT_DELIVERED}

    # Batch 1-a: persist before emit; clickable cards require an existing server row. Without a row, never emit
    # a doomed unclickable card. Orphan proposed rows left by failed emit are harmless.
    writer = _proposal_writer_provider() if _proposal_writer_provider is not None else None
    if writer is None:
        return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}
    proposal_id = _id("ccd")
    proposal_workspace_id = fctx.workspace_id if reg.write_scope.startswith("ws_") else None
    workspace_name = current.get("workspace_name") if reg.write_scope.startswith("ws_") else None
    rejection = await validate_settings_target(ctx, form_id)
    if rejection is not None:
        return rejection
    admitted = getattr(ctx, "configuration_settings_target", None)
    settings_target = admitted.target.projection() if admitted is not None else None
    try:
        await writer.create_lite_proposal(
            draft_id=proposal_id,
            team_id=getattr(ctx, "team_id", "") or "",
            owner_user_id=getattr(ctx, "user_id", "") or "",
            form_id=form_id,
            workspace_id=proposal_workspace_id,
            turn_id=getattr(ctx, "turn_id", "") or None,
            session_id=getattr(ctx, "session_id", "") or None,
            rows=rows,
            notes=notes,
            workspace_name=workspace_name,
            **({"settings_target": settings_target} if settings_target is not None else {}),
        )
    except Exception as exc:
        logger.warning("config_propose.persist_failed form=%s error=%s", form_id, type(exc).__name__)
        return {"ok": False, "error": "propose_failed", "message": _MSG_PROPOSE_FAILED}

    payload = {
        "proposal_id": proposal_id,
        # ★beat anchor (review P0-1): FE dispatcher sees payload, not envelope; explicitly include it as in plan_proposed
        "turn_id": getattr(ctx, "turn_id", "") or "",
        "form_id": form_id,
        "workspace_id": proposal_workspace_id,
        "team_id": getattr(ctx, "team_id", "") or "",
        "workspace_name": workspace_name,
        "rows": rows,
        "notes": notes,
        **({"review_page": reg.review_page} if reg.review_page else {}),
        **({"settings_target": settings_target} if settings_target is not None else {}),
    }
    delivered = await bus.emit(make_event(_EVENT, payload["turn_id"], payload)) if emit_confirmation else True
    if delivered is False:
        return {"ok": False, "error": "not_delivered", "message": _MSG_NOT_DELIVERED}

    out: dict[str, Any] = {
        "ok": True,
        "proposal_id": proposal_id,
        "rows": rows,
        "message": _MSG_PROPOSED if emit_confirmation else "The suggestion is saved without a confirmation event. No setting has been written.",
        "confirmation_event_emitted": emit_confirmation,
        "ui_status": "unacknowledged",
    }
    if notes:
        out["notes"] = notes
    if dropped:
        out["dropped_fields"] = dropped
        out["message"] += f" 另有字段被团队策略锁定未纳入：{'、'.join(dropped)}——如实告知用户。"
    return out


def roster_scoped_tool_schemas(schemas: list[ToolSchema], ctx: object) -> list[ToolSchema]:
    """Advertise legacy config Tools with copy matching this Turn's roster.

    Registered schemas never name the configuration specialist; only a Turn
    whose actor-scoped roster holds it gets the delegating description.
    """
    from bowhead_core.configuration_agent import configuration_agent_in_roster

    if not configuration_agent_in_roster(ctx):
        return schemas
    scoped = {TOOL_READ: _read_schema(specialist_in_roster=True), TOOL_PROPOSE: _propose_schema(specialist_in_roster=True)}
    return [scoped.get(schema.name, schema) for schema in schemas]


def register_config_copilot_chat_tools(
    registry: ToolRegistry,
    *,
    proposal_writer_provider: Callable[[], Any] | None = None,
    configuration_access_resolver: Any = None,
) -> bool:
    """注册配置副驾的两个 chat 工具（Lead-only）。

    2026-08-18 Allen 拍板拆掉 `FEATURE_CONFIG_COPILOT` 门控：副驾对所有客户常开，
    不再保留「不发版就能摘掉」的阀门。返回 True 保留是为了不改调用方签名。
    """
    if proposal_writer_provider is not None:
        set_proposal_writer_provider(proposal_writer_provider)
    for schema, handler in ((_read_schema(), _read_handler), (_propose_schema(), _propose_handler)):
        registry.register(
            ToolSpec(
                schema=schema,
                handler=handler,
                needs_approval=False,
                risk_level="low",
                allowed_for_roles=frozenset({"lead"}),
                visible_to_llm=True,
            )
        )
    from bowhead_core.configuration_agent import register_configuration_agent_tools

    register_configuration_agent_tools(registry, access_resolver=configuration_access_resolver)
    logger.info("config_copilot_chat_tools.registered (flag on)")
    return True


__all__ = [
    "TOOL_PROPOSE",
    "TOOL_READ",
    "register_config_copilot_chat_tools",
    "set_proposal_writer_provider",
]
