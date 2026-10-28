"""Agent manifest Pydantic schema (sprint multi-agent-v1 §5.3).

设计要点:
- `version`: int, snapshot per turn, 防热加载竞态 (T-CTO-P1.1)
- `trust_class`: 4 档显式声明, write-leaf 是强约束 (Anthropic single-write-leaf 学)
- `tools.default_enabled=False` deny-by-default + `tools.allow` 显式 allowlist
- `tools.deny` system-only (user manifest 不能 override 这字段; 校验在 registry 层)
- `input_schema` (handoff payload 校验, T-BE-P1.6) + `output_schema` (subagent_return 校验)
- `examples` 给 manifest-driven router LLM 做 few-shot
- `callable_peers` 给 v2 cross-domain handoff 用 (v1 字段已加, 调用走 v2)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 4 trust classes (adapted from Anthropic cookbook system-prompt header convention)
# delivery-surface M-C (2026-08-13): implement "report-writer", reserved since WP-F
# (both _write_guard.WRITE_TRUST_CLASSES and fence tests' _WRITE_TRUST_CLASSES include it).
# Semantics: data discipline remains read-mostly, but guarded writing (write_report, M-B bake +
# owner approval + revision CAS) lets agents write their own report body. CCA fence remains: team-created
# subagents still cannot write (filter_destructive removes it, security P0).
TrustClass = Literal["untrusted", "read-only", "read-mostly", "report-writer", "write-leaf"]

# Bowhead routing aliases (mapped by router.dev.yaml, etc.). v1 uses a permissive Literal; production validates strictly.
ModelAlias = Literal["lead", "analyst", "modeler", "narrative", "skill"]


class SystemSpec(BaseModel):
    """System prompt 来源: file (markdown) + 可选 append (短指令).

    `file` 指向 prompts/{system,user}/<workspace_id?>/<name>.md, 由 registry
    层在 base path 下 resolve + 校验 path traversal/symlink.
    """

    model_config = ConfigDict(extra="forbid")

    file: str = Field(..., min_length=1, max_length=256)
    append: str | None = Field(default=None, max_length=2000)


class ToolsSpec(BaseModel):
    """Tool 权限声明 (deny-by-default).

    `default_enabled=False` + `allow=[list]` 模式抄 Anthropic
    `default_config.enabled=false` + `configs[].enabled=true`. 显式 allowlist.

    `deny` 是 system-only 字段 (registry 校验 user manifest 不可设),
    用于系统级永久禁用 (e.g. `bi.delete_*` 永远不给 user agent).
    """

    model_config = ConfigDict(extra="forbid")

    default_enabled: bool = False
    allow: list[str] = Field(..., min_length=1)
    deny: list[str] = Field(default_factory=list)

    @field_validator("allow", "deny")
    @classmethod
    def _no_dup(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate tool names")
        return v


class SkillsSpec(BaseModel):
    """⚠️ DEPRECATED-INERT（2026-07-03，docs/skills-tools-ssot.md §5-G3/G6）.

    历史设计：`framework` = always-on 注入、`sop_lazy` = 按需 catalog 线索。
    实况：manifest_to_profile 从未消费本块（ζ 的动态 Layer-1 只挂在被覆盖的
    硬编码 compute 路径上），skill 感知的真实载体 = 各 peer prompt .md 的
    起手指引文本 + `skill_list` 全库工具。终态拍板 = 承认文本为权威，本块
    从全部 system manifest 移除；schema 字段保留仅为兼容历史 user manifest
    （extra='forbid'，删字段会拒载旧数据）——新 manifest 不要再写 `skills:`。
    """

    model_config = ConfigDict(extra="forbid")

    framework: list[str] = Field(default_factory=list, max_length=5)
    sop_lazy: list[str] = Field(default_factory=list, max_length=20)


class Example(BaseModel):
    """Steering example (Anthropic steering-examples.json 移植).

    用途:
    1. manifest-driven router LLM 看这些做 few-shot 决定 dispatch 哪 agent
    2. agent 输入 contract 文档 (开发者看)
    3. eval fixture 起点
    """

    model_config = ConfigDict(extra="forbid")

    event: str = Field(..., min_length=1, max_length=2000)
    description: str = Field(..., min_length=1, max_length=200)


TeamOverrideField = Literal[
    "enabled",
    "display_name",
    "description",
    "model",
    "skills",
    "prompt_append",
    "max_iterations",
    "primary_capability",
]
TEAM_OVERRIDE_FIELDS: tuple[TeamOverrideField, ...] = (
    "enabled",
    "display_name",
    "description",
    "model",
    "skills",
    "prompt_append",
    "max_iterations",
    "primary_capability",
)


class AgentListing(BaseModel):
    """Product listing of a system Agent (system manifests only).

    ``listed`` is the one switch for the Agent marketplace and the @ list. The
    listing never reaches the runtime profile, so it cannot change system
    revisions or manifest fingerprints.
    """

    model_config = ConfigDict(extra="forbid")

    listed: bool = False
    order: int = Field(..., ge=0, le=10_000)
    display_name: str = Field(..., min_length=1, max_length=40)
    summary: str = Field(..., min_length=1, max_length=200)
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    # None means the Team may change every override field.
    team_overrides: list[TeamOverrideField] | None = None

    @field_validator("capabilities")
    @classmethod
    def _known_capabilities(cls, value: list[str]) -> list[str]:
        from bowhead_core.system_agent_marketplace import SYSTEM_AGENT_CAPABILITIES

        if len(set(value)) != len(value):
            raise ValueError("duplicate listing capabilities")
        unknown = [capability for capability in value if capability not in SYSTEM_AGENT_CAPABILITIES]
        if unknown:
            raise ValueError(f"unknown listing capabilities: {unknown} (known: {sorted(SYSTEM_AGENT_CAPABILITIES)})")
        return value

    @field_validator("team_overrides")
    @classmethod
    def _no_duplicate_overrides(cls, value: list[TeamOverrideField] | None) -> list[TeamOverrideField] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("duplicate listing team_overrides")
        return value


class AgentManifest(BaseModel):
    """单 agent manifest (system 或 user).

    PR-#1 仅 system, PR-#2 加 user-defined 路径 + 强约束校验.

    强约束 (registry 层 enforce, 此 schema 仅 type-validate):
    - user manifest 不可设: `trust_class`, `output_schema`, `tools.deny`, `model` (超 plan tier)
    - `system.file` path traversal/symlink 校验在 registry
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        # Slug-only: prevent injection + filesystem escape
        pattern=r"^[a-z][a-z0-9-]{1,63}$",
    )
    version: int = Field(default=1, ge=1)
    model: ModelAlias
    trust_class: TrustClass
    max_iterations: int = Field(..., ge=1, le=200)
    max_output_tokens: int | None = Field(default=None, strict=True, ge=1)
    description: str = Field(..., min_length=1, max_length=500)

    # Sprint θ Phase 8b-4 + CTO P0 #3 fix: system Optional. Lead orchestrator
    # bypasses manifest system loading — builds prompt in-memory per turn (F2).
    # Existing specialist manifests still set system.file (markdown).
    system: SystemSpec | None = None
    tools: ToolsSpec
    skills: SkillsSpec = Field(default_factory=SkillsSpec)

    # Code-shipped Plugins compile into Tools and exact Skill grants.
    plugins: list[str] = Field(default_factory=list, max_length=32)
    # Exact system-owned source/operation declarations, independent of the
    # inert legacy skills block. None preserves legacy access; [] denies Skills.
    skill_grants: list[dict[str, Any]] | None = None

    @field_validator("skill_grants", mode="before")
    @classmethod
    def _exact_skill_grants(cls, value: Any) -> list[dict[str, Any]] | None:
        from bowhead_core.system_skill_snapshot import parse_system_manifest_skill_grants

        return None if value is None else [grant.to_canonical_dict() for grant in parse_system_manifest_skill_grants(value)]

    # JSONSchema dicts; registry validates additionalProperties=false
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None

    examples: list[Example] = Field(default_factory=list, max_length=10)

    # v2 cross-domain handoff: callable peer agents
    callable_peers: list[str] = Field(default_factory=list, max_length=20)

    # Explicit ownership of Lead routing roles: which responsibility is this Agent's primary one,
    # rather than what else it can do; Tool/Skill/produces still infer the latter. At registry
    # load, fail-fast validation checks active Definitions in the Capability catalog.
    primary_capability: str | None = Field(
        default=None,
        pattern=r"^cap_[a-z][a-z0-9_]{1,63}$",
    )

    # ── agent-capability-routing phase 2a: deliverable-capability **declarations** (axis B) ──
    #
    # Why **declare** instead of **derive** from `tools.allow` as axis A does
    # (discovery §2.3, this sprint's most counterintuitive rule): `consultant` and
    # `financial-analyst` have **identical** allowlists; their differences are 100% in prose.
    # `skill_invoke` is a generic dispatcher; having it does not imply deck production. `SkillsSpec` above
    # explicitly says `DEPRECATED-INERT`; `manifest_to_profile` never consumed it
    # (all nine system manifests observed had `skills: None`).
    # ⇒ Previously **nobody declared** this half of capability; hardcoded names were the only workaround.
    #
    # Specific replacement (fourth name-hardcoded location, omitted in discovery §2.1):
    # `agent_runner_adapter.py`'s `deck_inputs=(manifest.name in {"compute", "consultant"})`
    # its own comment said AgentManifest has extra='forbid' and **no deck_inputs field**,
    # hence name-derived. Originally intended for manifest; missing field forced name-based fallback.
    #
    # Vocabulary **reuses the export-format registry** (`canvas_export._FMT_SKILL` keys), without inventing a seventh
    # enum location; `test_export_format_registry_drift.py` already binds that table together.
    # Last sprint twice missed a location, yielding invalid Office output / unsupported producer.
    # Empty = no downloadable file production (canvas only), **not** a useless agent.
    produces: list[str] = Field(default_factory=list, max_length=16)

    # Capability protocol block opt-in: when true, the runtime appends the
    # protocol modules matching this Agent's tool whitelist (for example the
    # Datarc query protocol) to its system prompt. It is a property of the
    # Agent definition, never derived from the routing name; the default keeps
    # the prompt byte-identical. The batch-2 A/B evaluation concluded that
    # compute must stay off (builtin-capability-composition tech-design §12.1).
    capability_protocol_block: bool = False

    # System-only product listing (marketplace, @ list, Team overrides). It is
    # forbidden in user manifests and is never copied into the runtime profile.
    listing: AgentListing | None = None

    @field_validator("produces")
    @classmethod
    def _produces_known_formats(cls, v: list[str]) -> list[str]:
        # Defer import: `canvas_export` depends only on stdlib + bowhead_skill, but manifest loads early during
        # registry bootstrap; a constant table is not worth pulling it into the import chain.
        from bowhead_core.canvas_export import _FMT_SKILL

        if len(set(v)) != len(v):
            raise ValueError("duplicate produces")
        unknown = [f for f in v if f not in _FMT_SKILL]
        if unknown:
            raise ValueError(f"unknown produces format(s): {unknown} (可选: {sorted(_FMT_SKILL)})")
        return v

    @field_validator("callable_peers")
    @classmethod
    def _peers_no_dup(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate callable_peers")
        # Each item must be a valid agent name (slug-only).
        import re

        pat = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
        for name in v:
            if not pat.match(name):
                raise ValueError(f"invalid callable_peers name: {name!r} (必须 slug-only, regex {pat.pattern})")
        return v
