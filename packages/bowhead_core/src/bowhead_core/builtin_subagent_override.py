"""内置子代理 base(code) + delta(team) 的**唯一**合成点。

内置子代理管理 sprint（docs/sprints/builtin-subagent-admin/tech-design.md §3）。

两个 seam —— advertise（``lead_orchestrator._gather_lead_peers``）和 resolve
（``subagent.SubagentRunner._resolve_profile``）—— 都只调本模块，不各写一份合成规则。

三条不变量：

1. ``ov is None`` → 返回**原对象本身**（不是拷贝）。无 override 的团队走的必须是逐字节等价的
   老路径，这是回滚绳。
2. 只做白名单里的字段。``tools`` / ``trust_class`` / ``output_schema`` / ``callable_peers`` /
   ``name`` 永远不可被团队覆盖（I2/I3 不变量与 payload 契约的载体）。
3. 覆盖值不可信 —— **每个字段在 resolve 时都自带地板**，写入侧的 422 只是给 admin 的即时反馈：
   model 不在 allowlist → 忽略；description 过消毒 → 不安全回落 manifest 默认；
   skills 过 slug fullmatch → 形状不对的丢弃；prompt_append 重跑注入 lint → 命中整段丢弃；
   Legacy iteration metadata is ignored; Team run limits govern execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.builtin_subagent_override_store import BuiltinSubagentOverride
    from bowhead_core.subagent import SubagentProfile

logger = logging.getLogger(__name__)

# Runtime revalidation for followup P2-2 (same source as writes, avoiding rule drift)
from bowhead_core.agents.exceptions import (  # noqa: E402
    ManifestSecurityError as _ManifestSecurityError,
)
from bowhead_core.agents.manifest_security import (  # noqa: E402
    lint_user_append as _lint_user_append,
)
from bowhead_core.skill_upload import NAME_PATTERN as _SKILL_NAME_RE  # noqa: E402

_FLAG_TRUTHY = frozenset({"1", "true", "yes", "on"})

# llm_agent_type choices teams may assign to built-ins: team_llm_config.REQUIRED_AGENT_TYPES minus:
# - "lead": most expensive tier; giving a 100-iteration analyst peer the lead model silently consumes the costliest model
# (report_writer_loop.py records the same incident).
# - "judge": review-only, not a dispatchable peer tier.
# Do **not share** self-created Agents' TEAM_SUBAGENT_ALLOWED_MODEL_ROLES ({"analyst"}); built-ins already use
# narrative and other tiers, so restricting them to analyst would downgrade them.
MODEL_ALLOWLIST: frozenset[str] = frozenset({"analyst", "narrative", "modeler", "echo"})

# Align with peer_block._MAX_SYSTEM_DESC_CHARS: Lead never sees excess, so reject on write rather than
# silently swallowing it (import the constant instead of copying the literal, avoiding drift).
try:  # pragma: no cover - Import fails only if peer_block is refactored
    from bowhead_core.agents.prompts.peer_block import (
        _MAX_SYSTEM_DESC_CHARS as MAX_DESCRIPTION_CHARS,
    )
except ImportError:  # pragma: no cover
    MAX_DESCRIPTION_CHARS = 200

MAX_DISPLAY_NAME_CHARS = 40
# Team-specific append for built-in agents: **a segment after the existing prompt**, not a whole prompt,
# so limit it below self-created agents' MAX_PROMPT_CHARS_*.
MAX_PROMPT_APPEND_CHARS = 8000
MAX_SKILLS = 24

# Built-ins whose disable consequences need a second UI confirmation. Fully reversible by re-enabling, so backend does not prohibit it, but
# tell callers the consequences structurally; silent loss of report production can take hours to diagnose.
DISABLE_WARNINGS: dict[str, str] = {
    # Primary analysis peer, destination for compound data-analysis requests
    "compute": "disables_data_analysis",
    # consultant is the sole deck-deliverable peer (its manifest treats decks as the contract itself).
    # 116 live testing (2026-08-04): disabling it made Lead assign compute the same make-a-PPT request;
    # turns still completed but **no .pptx was produced**: two disabled turns yielded 11/14 artifacts with zero .pptx each,
    # while the enabled turn's 26 artifacts included consultant-deck.pptx.
    # ⚠ This is not graceful degradation. Lead's task text requests a downloadable PPTX: that is an **instruction**, not
    # **delivery**; task text or turn status alone falsely suggests successful degradation (I misjudged it twice).
    "consultant": "disables_deck_output",
}


def effective_model(manifest_model: str, ov: BuiltinSubagentOverride | None) -> str:
    """生效的 llm_agent_type。越界值忽略（保留 manifest 值）。"""
    if ov is None or not ov.model:
        return manifest_model
    if ov.model not in MODEL_ALLOWLIST:
        logger.warning(
            "builtin_override.model_ignored name=%s model=%s not_allowlisted",
            ov.name,
            ov.model,
        )
        return manifest_model
    return ov.model


def effective_display_name(manifest_name: str, ov: BuiltinSubagentOverride | None) -> str:
    if ov is None or not ov.display_name:
        return ""
    return ov.display_name[:MAX_DISPLAY_NAME_CHARS] or manifest_name


def effective_primary_capability(
    manifest_primary: str | None,
    ov: BuiltinSubagentOverride | None,
    *,
    eligible_capabilities: frozenset[str] | None = None,
) -> str | None:
    """Return the prompt-routing responsibility after a team override.

    This value affects legacy advertisement and the exact shadow snapshot; it
    never grants a Tool or bypasses execution admission. Revalidate against the
    live catalog here so a direct database write cannot make a retired/unknown
    capability authoritative.
    """

    if ov is None or not ov.primary_capability:
        return manifest_primary
    from bowhead_core.agents.capability_roles import active_capability_ids

    if ov.primary_capability not in active_capability_ids() or (
        eligible_capabilities is not None and ov.primary_capability not in eligible_capabilities
    ):
        logger.warning(
            "builtin_override.primary_capability_ignored name=%s capability=%s",
            ov.name,
            ov.primary_capability,
        )
        return manifest_primary
    return ov.primary_capability


def effective_description(
    manifest_desc: str,
    ov: BuiltinSubagentOverride | None,
) -> tuple[str, bool]:
    """返回 ``(生效描述, 是否被团队覆盖)``。

    ⚠️ 安全要点：``render_peer_block`` 对 ``source == "system"`` 的 peer 只做 normalize +
    XML escape + 截断，**不跑注入 lint、不跑 hijack 检查**（注释写明「trusted
    manifest.description」）。admin 一旦能改这个字段，它就不再是代码常量 —— 所以覆盖值必须在这里
    过和 team/user 源同一套消毒核。

    消毒判定不安全 → **回落 manifest 默认描述**，不是 drop peer：为一条 admin 打错的描述就让整个
    内置从 roster 消失，是比留着默认描述更糟的故障模式。
    """
    if ov is None or not ov.description:
        return manifest_desc, False
    from bowhead_core.agents.prompts.peer_block import sanitize_builtin_override_description

    cleaned = sanitize_builtin_override_description(ov.description, name=ov.name)
    if cleaned is None:
        logger.warning(
            "builtin_override.description_rejected name=%s fallback=manifest_default",
            ov.name,
        )
        return manifest_desc, False
    return cleaned, True


def effective_skill_names(ov: BuiltinSubagentOverride | None) -> tuple[str, ...]:
    """The same sanitized Skill selections for runtime and evidence."""
    if ov is None or not ov.skills:
        return ()
    skills = tuple(s for s in ov.skills if s and _SKILL_NAME_RE.fullmatch(s))[:MAX_SKILLS]
    if len(skills) != len([s for s in ov.skills if s]):
        logger.warning("builtin_override.skills_dropped name=%s kept=%d reason=bad_slug_or_limit", ov.name, len(skills))
    return skills


def apply_to_profile(
    profile: SubagentProfile,
    ov: BuiltinSubagentOverride | None,
) -> SubagentProfile:
    """Compose model, skills, prompt, display name and primary responsibility overrides.

    ``description`` 和 ``enabled`` 不在这里 —— 它们只作用于 advertise 侧（``enabled`` 由调用方在
    命中前拦截，见 ``_resolve_profile``）。exact primary 目前只进入 M3-13B immutable shadow snapshot，
    不改变 M3-12 runtime selection 或授权。

    ``ov is None`` 或无任何相关字段 → 返回**同一个对象**，不新建实例。
    """
    if ov is None:
        return profile
    from dataclasses import replace

    changed: dict[str, Any] = {}

    model = effective_model(profile.llm_agent_type, ov)
    if model != profile.llm_agent_type:
        changed["llm_agent_type"] = model

    display_name = effective_display_name(profile.name, ov)
    if display_name and display_name != profile.display_name:
        changed["display_name"] = display_name

    prompt = profile.system_prompt
    whitelist = profile.tool_whitelist
    if ov.skills:
        # Followup P2-2: revalidate skill-name slug shape at resolve. Writes already return 422; this is the lower
        # silent floor, mirroring subagent.py's team/custom branch (original comment: "stored record predates /
        # bypassed creation-time validation"). Without it, the module's promise that API-bypassing database rows
        # cannot harm runtime fails for this field: skill names enter the auto-read prelude directly.
        skills = effective_skill_names(ov)
        if skills:
            # Reproduce self-created subagents' prelude wording (subagent.py team branch), making built-in and self-created SOP
            # reading instructions byte-for-byte identical.
            names = ", ".join(f'"{n}"' for n in skills)
            prompt = f"## Auto-read skills\n第一步**必须**先 `skill_read` 读这些 SOP，再开工: {names}\n\n{prompt}"
            # Bound skills implicitly need skill_read: prelude forces it first; absent from whitelist, each run wastes a turn
            # (same precedent as subagent.py's force-included subagent_return / skill_read).
            if "skill_read" not in whitelist:
                whitelist = (*whitelist, "skill_read")
    if ov.prompt_append:
        # Followup P2-2: likewise rerun injection lint at resolve. Discard the whole segment on a hit (not truncate: lint-hit
        # text has no safe prefix); team-specific guidance is lost, but the built-in itself runs normally.
        append = ov.prompt_append[:MAX_PROMPT_APPEND_CHARS]
        if len(append) < len(ov.prompt_append):
            # Write side (routes/team_subagents.py) already rejects excess; reaching here means a pre-gate historical row
            # or write-side bypass. Preserve a truncated value (runtime exceptions would kill an already-running agent),
            # but **never silently**; previously nothing was logged here.
            logger.warning(
                "builtin_override.prompt_append_truncated name=%s stored=%d kept=%d",
                ov.name,
                len(ov.prompt_append),
                len(append),
            )
        try:
            _lint_user_append(append)
        except _ManifestSecurityError:
            logger.warning("builtin_override.prompt_append_rejected name=%s reason=injection_lint", ov.name)
            append = ""
        if append:
            prompt = f"{prompt}\n\n{append}"

    if prompt != profile.system_prompt:
        changed["system_prompt"] = prompt
    if whitelist != profile.tool_whitelist:
        changed["tool_whitelist"] = whitelist

    if ov.primary_capability:
        from bowhead_core.agents.capability_roles import supported_primary_capabilities

        current_primary = profile.primary_capability_ref.split("@", 1)[0] if profile.primary_capability_ref else None
        effective_primary = effective_primary_capability(
            current_primary,
            ov,
            eligible_capabilities=supported_primary_capabilities(
                profile.tool_whitelist,
                output_formats=("pptx",) if profile.deck_inputs else (),
            ),
        )
        if effective_primary is not None:
            from bowhead_core.agent_capability_catalog import latest_active_capability_ref

            exact_primary = latest_active_capability_ref(effective_primary)
            if exact_primary != profile.primary_capability_ref:
                changed["primary_capability_ref"] = exact_primary

    if not changed:
        return profile
    return replace(profile, **changed)


def restrict_override_to_manifest(
    name: str,
    ov: BuiltinSubagentOverride | None,
) -> BuiltinSubagentOverride | None:
    """Keep only the Team override fields the system Agent's manifest allows.

    The write route rejects other fields; this is the read-side floor for rows
    written before the manifest restricted them. Returns ``ov`` itself when
    nothing is dropped.
    """
    if ov is None:
        return None
    from dataclasses import is_dataclass, replace
    from types import SimpleNamespace

    from bowhead_core.system_agent_marketplace import system_agent_team_override_fields

    value_fields = ("display_name", "description", "model", "skills", "prompt_append", "max_iterations", "primary_capability")
    allowed = system_agent_team_override_fields(name)
    cleared: dict[str, Any] = {}
    if "enabled" not in allowed and not getattr(ov, "enabled", True):
        cleared["enabled"] = True
    for field_name in value_fields:
        if (field_name == "max_iterations" or field_name not in allowed) and getattr(ov, field_name, None) is not None:
            cleared[field_name] = None
    if is_dataclass(ov) and not isinstance(ov, type):
        return replace(ov, **cleared) if cleared else ov
    # Duck-typed rows (tests, adapters): keep every attribute readable after the copy.
    return SimpleNamespace(**{"enabled": True, **dict.fromkeys(value_fields), **vars(ov), **cleared})  # type: ignore[return-value]


__all__ = [
    "DISABLE_WARNINGS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_PROMPT_APPEND_CHARS",
    "MAX_SKILLS",
    "MODEL_ALLOWLIST",
    "apply_to_profile",
    "effective_description",
    "effective_display_name",
    "effective_primary_capability",
    "effective_model",
    "restrict_override_to_manifest",
]
