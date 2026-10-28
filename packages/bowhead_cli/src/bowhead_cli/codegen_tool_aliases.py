"""Sprint ω Phase 4 — codegen for tool_aliases.yaml.

Reads `packages/bowhead_core/src/bowhead_core/agents/manifests/tool_aliases.yaml`
and writes 7 deterministic output files:

  1. `bowhead_core/agents/_generated/tool_names.py`
     Python constants like `BI_QUERY = "bi_query"`. Prompts/code import these
     instead of literal strings — so future renames are single-source.

  2. `bowhead_core/agents/_generated/role_bindings.py`
     `_PARADIGM_V2_ROLE_MAP: dict[str, frozenset[str]]`. Replaces inline literal
     at `tool_registry.py:2731-2768`.

  3. `bowhead_core/agents/_generated/legacy_trace_names.py`
     `LEGACY_TRACE_NAMES: dict[str, str]` — legacy alias → canonical. Used by
     trace replay UI (`§2.13`) so old recordings render without crash.

  4. `bowhead_core/agents/_generated/tool_renames.py`
     `TOOL_RENAMES: dict[str, str]` — 改名前的旧名 → 现名, plus
     `canonical_tool_name()`. **Runtime-authoritative**, unlike (3): every
     name-keyed decision resolves through it. See `docs/design/工具改名迁移.md`.

  5. `bowhead_frontend/lib/_generated/tool-display.ts`
     TS `TOOL_DISPLAY` map + `TOOL_RENAMES` + `canonicalToolName()`.

  6. `bowhead_core/agents/_generated/tool_display.py`
     `TOOL_DISPLAY` —— 工具的**用户面**中英名字与一句话说明。后端建 agent 的
     工具勾选列表读它；文案与前端步骤卡/chip 同一份，别再各写一张手写表。

  7. `bowhead_core/agents/_generated/tool_kinds.py`
     `TOOL_KINDS` (user-visible Tool or internal part), `TOOL_RISKS` and
     `USER_VISIBLE_TOOLS`. The frontend gets the same `kind` / `risk` on each
     `TOOL_DISPLAY` entry. See docs/design/capability-center-plugins-tech-design.md §4.1.

Hard exclusion (NEVER touched by codegen): Prometheus metric label
`bowhead_bi_query_url_only_total` at `tool_registry.py:1334`. r3.1 §2.3
invariant — dashboard-bound, renaming breaks Grafana.

Usage:
    uv run python -m bowhead_cli.codegen_tool_aliases [--check]

With `--check`: re-runs codegen in memory and exits non-zero if output files
on disk differ from generated. Suitable for pre-commit hook + CI guard.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

_HEADER = """# ⚠️ AUTO-GENERATED — DO NOT EDIT BY HAND ⚠️
#
# Source: packages/bowhead_core/src/bowhead_core/agents/manifests/tool_aliases.yaml
# Regenerate: uv run python -m bowhead_cli.codegen_tool_aliases
# CI check  : uv run python -m bowhead_cli.codegen_tool_aliases --check
"""

_TS_HEADER = """// ⚠️ AUTO-GENERATED — DO NOT EDIT BY HAND ⚠️
//
// Source: packages/bowhead_core/src/bowhead_core/agents/manifests/tool_aliases.yaml
// Regenerate: uv run python -m bowhead_cli.codegen_tool_aliases
"""


def _repo_root() -> Path:
    """Resolve repo root by walking up from this file."""
    here = Path(__file__).resolve()
    # .../packages/bowhead_cli/src/bowhead_cli/codegen_tool_aliases.py
    # parents[4] = repo root
    return here.parents[4]


def _yaml_path() -> Path:
    return _repo_root() / "packages/bowhead_core/src/bowhead_core/agents/manifests/tool_aliases.yaml"


def _gen_dir_py() -> Path:
    return _repo_root() / "packages/bowhead_core/src/bowhead_core/agents/_generated"


def _gen_dir_ts() -> Path:
    return _repo_root() / "packages/bowhead_frontend/lib/_generated"


def load_aliases() -> list[dict[str, Any]]:
    with _yaml_path().open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    tools = data.get("tools") or []
    # Deterministic sort by canonical name (DeepSeek prefix-cache + diff stability)
    return sorted(tools, key=lambda t: t["canonical"])


def load_retired() -> list[str]:
    """`retired:` —— **已连注册点一起删除**的工具名。

    与 `renamed_from` 的区别（别混）：`renamed_from` 是纯改名，旧名在解析时会被
    **归一成现名**；退役的工具没有等价现名（`bi_attribute` 不是 `bi_query`），
    归一过去等于静默换掉分发目标。所以 `retired` 只做一件事：继续占住
    `RESERVED_TOOL_NAMES`，不让 user skill 撞掉一个历史 trace 里出现过的名字
    （`skill_upload.py` G5）。不进 `ALL_TOOL_NAMES`、不进 role binding、
    不进 `TOOL_RENAMES`。
    """
    with _yaml_path().open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return sorted(str(n) for n in (data.get("retired") or []))


TOOL_KIND_VALUES = ("tool", "part")
TOOL_RISK_VALUES = ("read", "compute", "artifact_write", "external_write", "destructive")


def validate_kinds(tools: list[dict[str, Any]]) -> None:
    """Every entry declares `kind`; a user-visible Tool also declares `risk`."""
    for t in tools:
        name = t["canonical"]
        kind = t.get("kind")
        if kind not in TOOL_KIND_VALUES:
            raise ValueError(f"Tool {name!r} has kind {kind!r}; expected one of {TOOL_KIND_VALUES}.")
        risk = t.get("risk")
        if kind == "tool" and risk is None:
            raise ValueError(f"Tool {name!r} is user-visible and must declare risk.")
        if risk is not None and risk not in TOOL_RISK_VALUES:
            raise ValueError(f"Tool {name!r} has risk {risk!r}; expected one of {TOOL_RISK_VALUES}.")


def render_tool_kinds_py(tools: list[dict[str, Any]]) -> str:
    """Generate resource kind and risk maps for built-in Tools and system parts."""
    validate_kinds(tools)
    kinds = ", ".join(f'"{value}"' for value in TOOL_KIND_VALUES)
    risks = ", ".join(f'"{value}"' for value in TOOL_RISK_VALUES)
    lines = [
        _HEADER,
        '"""Resource kind and risk of each built-in tool name.',
        "",
        "`tool` entries are user-visible built-in Tools; `part` entries are internal system parts",
        'that are never shown, selected or disabled on their own."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Literal",
        "",
        f"ToolKind = Literal[{kinds}]",
        f"ToolRisk = Literal[{risks}]",
        "",
        "TOOL_KINDS: dict[str, ToolKind] = {",
    ]
    for t in tools:
        lines.append(f'    "{t["canonical"]}": "{t["kind"]}",')
    lines.append("}")
    lines.append("")
    lines.append("# Every user-visible Tool has a risk; a part only keeps one it already had in the authoring catalog.")
    lines.append("TOOL_RISKS: dict[str, ToolRisk] = {")
    for t in tools:
        if t.get("risk"):
            lines.append(f'    "{t["canonical"]}": "{t["risk"]}",')
    lines.append("}")
    lines.append("")
    lines.append('USER_VISIBLE_TOOLS: frozenset[str] = frozenset(name for name, kind in TOOL_KINDS.items() if kind == "tool")')
    lines.append("")
    return "\n".join(lines)


def render_tool_names_py(tools: list[dict[str, Any]], retired: list[str] | None = None) -> str:
    """Generate Python constants like `BI_QUERY = "bi_query"`."""
    lines = [_HEADER, '"""Python constants for tool names. Import these instead of literals."""', "", "from __future__ import annotations", ""]
    for t in tools:
        name = t["canonical"]
        const = name.upper()
        lines.append(f'{const} = "{name}"')
    lines.append("")
    lines.append("# 现役工具名（**只含 canonical**）—— 契约测试 / role binding 校验用。")
    lines.append("# ⚠ 改名前的旧名**不在这里**：它们不是工具，只是保留名，见 RESERVED_TOOL_NAMES。")
    lines.append("# 混在一起会让「每个真工具都要显式选 kind」这类守卫对着一个不存在的名字要求元数据。")
    lines.append("ALL_TOOL_NAMES: frozenset[str] = frozenset({")
    for t in tools:
        lines.append(f'    "{t["canonical"]}",')
    lines.append("})")
    lines.append("")
    lines.append("# 保留名 = 现役名 ∪ 改名前的旧名。**user skill 不许撞的名字集合**")
    lines.append("# (skill_upload.py G5)：旧名让出去会被用户 skill 占用，而历史 trace 里分不出来。")
    lines.append("RESERVED_TOOL_NAMES: frozenset[str] = ALL_TOOL_NAMES | frozenset({")
    _canon = {t["canonical"] for t in tools}
    _reserved_extra = set(collect_renames(tools))
    for nm in retired or []:
        if nm in _canon:
            raise ValueError(f"retired {nm!r} 与某个 canonical 同名——退役的工具不能同时是活工具")
        _reserved_extra.add(nm)
    for nm in sorted(_reserved_extra):
        lines.append(f'    "{nm}",')
    lines.append("})")
    lines.append("")
    lines.append("# pseudo 条目（display-only，非注册 ToolSpec；如 python_sandbox 经")
    lines.append("# skill_invoke 执行、仅 trace 显示用）——namespace 不相交测试的豁免集。")
    lines.append("PSEUDO_TOOL_NAMES: frozenset[str] = frozenset({")
    for t in tools:
        if t.get("pseudo"):
            lines.append(f'    "{t["canonical"]}",')
    lines.append("})")
    lines.append("")
    return "\n".join(lines)


def render_role_bindings_py(tools: list[dict[str, Any]]) -> str:
    """Generate _PARADIGM_V2_ROLE_MAP dict.

    Each canonical name AND each legacy alias is bound to the same role set —
    legacy aliases are still registered as tools (their handlers route to the
    canonical), so they need role enforcement too. Without this, paradigm_v2
    role gate would drop legacy-tool calls during the deprecation window.
    """
    lines = [
        _HEADER,
        '"""Role bindings: which agent role(s) may invoke each tool.',
        "",
        "Single source of truth. tool_registry.py imports this — do not duplicate",
        "in-line literal dict." + '"""',
        "",
        "from __future__ import annotations",
        "",
        "_PARADIGM_V2_ROLE_MAP: dict[str, frozenset[str]] = {",
    ]
    # Build (name, roles) entries: canonical first, then legacy aliases
    # inherit parent's roles. r3.1 §2.3 invariant (Phase 4 P0-1 fix):
    # if a name is ALSO a canonical entry elsewhere, its OWN entry wins —
    # legacy-alias inherit must NEVER widen privileges. We pre-collect the
    # canonical set, then skip any legacy-alias whose name is also canonical.
    canonical_names = {t["canonical"] for t in tools}
    entries: dict[str, list[str]] = {}
    for t in tools:
        if t.get("pseudo"):
            # G5: pseudo entries (display-only, not registered ToolSpecs) produce no role bindings —
            # the former phantom python_sandbox binding was always inert (set_allowed_roles silently skips it)
            # and misleading in audits (docs/skills-tools-ssot.md §5-G5).
            continue
        roles = sorted(t.get("allowed_roles") or [])
        entries[t["canonical"]] = roles
    for t in tools:
        if t.get("pseudo"):
            continue
        roles = sorted(t.get("allowed_roles") or [])
        for legacy in t.get("legacy_aliases") or []:
            if legacy in canonical_names:
                # canonical entry already set — skip inherit to avoid privilege drift
                continue
            entries[legacy] = roles
    for name in sorted(entries):
        roles = entries[name]
        roles_repr = ", ".join(f'"{r}"' for r in roles)
        lines.append(f'    "{name}": frozenset({{{roles_repr}}}),')
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def render_legacy_trace_names_py(tools: list[dict[str, Any]]) -> str:
    """Generate LEGACY_TRACE_NAMES — legacy alias → canonical mapping for trace replay.

    Per r3.1 §2.13: clean break for runtime dispatch (LLM emits canonical only),
    but trace renderer needs to display old recordings without crash.
    """
    lines = [
        _HEADER,
        '"""Legacy alias → canonical mapping for trace replay UI.',
        "",
        "Sprint ω §2.13: runtime dispatch is clean-break (LLM uses canonical names",
        "only). This map is read-only — used by /reports trace renderer to translate",
        'historical tool_name="bi_ask_question" → display as "查询数据"."""',
        "",
        "from __future__ import annotations",
        "",
        "LEGACY_TRACE_NAMES: dict[str, str] = {",
    ]
    for t in tools:
        name = t["canonical"]
        for legacy in t.get("legacy_aliases") or []:
            lines.append(f'    "{legacy}": "{name}",')
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def render_tool_display_py(tools: list[dict[str, Any]]) -> str:
    """Generate user-facing tool names and descriptions from the shared manifest.

    German and French use their translations when present, with English fallback
    for untranslated entries. This display map never changes tool dispatch or roles.
    """
    lines = [
        _HEADER,
        '"""Localized user-facing tool names and descriptions.',
        "",
        'Source: agents/manifests/tool_aliases.yaml. Display only; never dispatch or authorization."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import TypedDict",
        "",
        "",
        "class ToolDisplay(TypedDict):",
        "    display_zh: str",
        "    display_en: str",
        "    display_de: str",
        "    display_fr: str",
        "    blurb_zh: str",
        "    blurb_en: str",
        "    blurb_de: str",
        "    blurb_fr: str",
        "    sigil: str",
        "    category: str",
        "",
        "",
        "TOOL_DISPLAY: dict[str, ToolDisplay] = {",
    ]
    for t in tools:
        name = t["canonical"]
        fields = {
            "display_zh": t.get("display_zh") or name,
            "display_en": (t.get("display_en") or "").strip(),
            "blurb_zh": (t.get("blurb_zh") or "").strip(),
            "blurb_en": (t.get("blurb_en") or "").strip(),
        }
        for suffix in ("de", "fr"):
            fields[f"display_{suffix}"] = (t.get(f"display_{suffix}") or "").strip() or fields["display_en"]
            fields[f"blurb_{suffix}"] = (t.get(f"blurb_{suffix}") or "").strip() or fields["blurb_en"]
        for key, value in fields.items():
            if not value:
                raise ValueError(f"Tool {name!r} is missing {key}; user-facing names and descriptions are required.")
            if '"' in value:
                raise ValueError(f"Tool {name!r} has a double quote in {key}; codegen does not escape quotes.")
        lines.append(f'    "{name}": {{')
        for key, value in fields.items():
            lines.append(f'        "{key}": "{value}",')
        lines.append(f'        "sigil": "{t.get("sigil") or "·"}",')
        lines.append(f'        "category": "{t.get("category") or "unknown"}",')
        lines.append("    },")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def collect_renames(tools: list[dict[str, Any]]) -> dict[str, str]:
    """`renamed_from` 的扁平映射：旧名 → 现名。

    与 `legacy_aliases` 是**两个不同的东西**，不要合并（docs/design/工具改名迁移.md §2）：

    - `legacy_aliases` = 历史上**双注册**的旧名（`bi_ask_question` 等）。它们各自
      **仍有自己的 handler**（`visible_to_llm=False`），语义只用于展示归一。
      把它们做成运行时规范化会**改变分发目标**（换一个 handler），是行为变更。
    - `renamed_from` = **纯改名**：旧名没有、也不会有自己的 ToolSpec，
      解析时一律规范化成现名。这是本表唯一进运行时判定的来源。
    """
    canonical_names = {t["canonical"] for t in tools}
    legacy_names = {legacy for t in tools for legacy in (t.get("legacy_aliases") or [])}
    out: dict[str, str] = {}
    for t in tools:
        for old in t.get("renamed_from") or []:
            if old in canonical_names:
                raise ValueError(f"renamed_from {old!r} 与某个 canonical 同名——旧名不得是活工具")
            if old in legacy_names:
                raise ValueError(f"renamed_from {old!r} 同时登记在 legacy_aliases——两种语义只能选一个")
            if old in out and out[old] != t["canonical"]:
                raise ValueError(f"renamed_from {old!r} 被两个工具认领：{out[old]} / {t['canonical']}")
            out[old] = t["canonical"]
    return out


def render_tool_renames_py(tools: list[dict[str, Any]]) -> str:
    """Generate TOOL_RENAMES + canonical_tool_name() — the runtime-authoritative map."""
    renames = collect_renames(tools)
    lines = [
        _HEADER,
        '"""工具改名的读路径规范化表。**运行时权威**。',
        "",
        "旧名 → 现名。客户自建 agent 的 system_prompt / tool_refs 里可能永久留着旧名，",
        "我们看不到也改不了 ⇒ 一个字节的客户数据都不动，只在**解析时**归一。",
        "别名是永久的，不是过渡期。规范见 docs/design/工具改名迁移.md。",
        "",
        "⚠ 与 legacy_trace_names.LEGACY_TRACE_NAMES 不同：那是双注册旧名的**展示**归一,",
        '把它接进运行时会改变分发目标 —— 两张表**不合并**。"""',
        "",
        "from __future__ import annotations",
        "",
        "TOOL_RENAMES: dict[str, str] = {",
    ]
    for old in sorted(renames):
        lines.append(f'    "{old}": "{renames[old]}",')
    lines.append("}")
    lines.append("")
    lines.append("")
    lines.append("def canonical_tool_name(name: str) -> str:")
    lines.append('    """旧名 → 现名；未登记的名字**原样返回**（未知工具照旧走各自的报错路）。"""')
    lines.append("    return TOOL_RENAMES.get(name, name)")
    lines.append("")
    return "\n".join(lines)


def render_tool_display_ts(tools: list[dict[str, Any]]) -> str:
    """Generate frontend TS `TOOL_DISPLAY` map.

    Schema must match `packages/bowhead_frontend/lib/tool-display.ts` ToolDisplay
    interface: { label, labelEn, blurb, blurbEn, sigil, category }.

    `labelEn` 不是可选的：前端在用的展示表（步骤卡、chip、工具选择器）全部按 locale
    取名，只发中文会让英文用户看到中文名或裸工具名。yaml 里 display_en 是必填字段，
    这里缺就直接抛 —— 静默回落成中文正是 capability-surface-audit 查出的老毛病。

    `blurb` 同样要发到前端：建 agent 的工具勾选列表（`create-subagent-card.tsx`）
    调的是只返回**裸工具名数组**的那个目录端点，拿不到后端目录里的说明，只能在前端
    按名字前缀猜（`kb_*` 一律「查询知识库表与记录」，7 个工具直接显示代号本身）。
    116 真机 dogfood 2026-09-07 实拍。文案给它一份，别让它再自己编一张表。

    文案按界面语言分支输出（`{ "zh-CN", "en-US", "de-DE", "fr-FR" }`），前端按当前语言取一支。
    德语、法语可选：yaml 没写 `display_de` / `blurb_de`（`display_fr` / `blurb_fr`）时用英文
    占位——德语、法语界面显示英文，**绝不回落成中文**（此前前端只分「英文 / 其他」，德语界面
    整片落回中文）。
    """
    lines = [
        _TS_HEADER,
        "",
        'import type { GeneratedToolDisplay } from "../tool-display";',
        "",
        "export const TOOL_DISPLAY: Record<string, GeneratedToolDisplay> = {",
    ]
    for t in tools:
        name = t["canonical"]
        label = t.get("display_zh") or t["canonical"]
        label_en = (t.get("display_en") or "").strip()
        if not label_en:
            raise ValueError(f"tool `{name}` 缺 display_en —— 展示名是中英成对的，不能只写一半")
        blurb = (t.get("blurb_zh") or "").strip()
        blurb_en = (t.get("blurb_en") or "").strip()
        if not blurb or not blurb_en:
            raise ValueError(f"tool `{name}` 缺 blurb_zh/blurb_en —— 一句话说明也是中英成对的")
        # German placeholder: fall back to English when untranslated (see the YAML header comment).
        label_de = (t.get("display_de") or "").strip() or label_en
        blurb_de = (t.get("blurb_de") or "").strip() or blurb_en
        # French placeholder: likewise, fall back to English when untranslated.
        label_fr = (t.get("display_fr") or "").strip() or label_en
        blurb_fr = (t.get("blurb_fr") or "").strip() or blurb_en
        for value in (label, label_en, label_de, label_fr, blurb, blurb_en, blurb_de, blurb_fr):
            if '"' in value:
                raise ValueError(f"tool `{name}` 的用户面文案含双引号，codegen 不做转义")
        sigil = t.get("sigil") or "·"
        category = t.get("category") or "unknown"
        if t.get("kind") not in TOOL_KIND_VALUES:
            raise ValueError(f"tool `{name}` has no valid kind; run validate_kinds for details")
        lines.append(f"  {name}: {{")
        lines.append(f'    label: {{ "zh-CN": "{label}", "en-US": "{label_en}", "de-DE": "{label_de}", "fr-FR": "{label_fr}" }},')
        lines.append(f'    blurb: {{ "zh-CN": "{blurb}", "en-US": "{blurb_en}", "de-DE": "{blurb_de}", "fr-FR": "{blurb_fr}" }},')
        lines.append(f'    sigil: "{sigil}",')
        lines.append(f'    category: "{category}",')
        lines.append(f'    kind: "{t["kind"]}",')
        if t.get("risk"):
            lines.append(f'    risk: "{t["risk"]}",')
        lines.append("  },")
    lines.append("};")
    lines.append("")
    # Legacy aliases map for frontend trace renderer
    lines.append("export const LEGACY_TRACE_NAMES: Record<string, string> = {")
    for t in tools:
        for legacy in t.get("legacy_aliases") or []:
            lines.append(f'  "{legacy}": "{t["canonical"]}",')
    lines.append("};")
    lines.append("")
    # Rename map (runtime authority) and display normalization entry point. See docs/design/工具改名迁移.md
    lines.append("/** 旧名 → 现名。工具改名的读路径规范化表（后端同名表的 TS 投影）。 */")
    lines.append("export const TOOL_RENAMES: Record<string, string> = {")
    renames = collect_renames(tools)
    for old in sorted(renames):
        lines.append(f'  "{old}": "{renames[old]}",')
    lines.append("};")
    lines.append("")
    lines.append("/** 旧名 → 现名。历史 trace 里存的是当时的名字，查表前先过这里。 */")
    lines.append("/** 不吃 LEGACY_TRACE_NAMES —— 那是双注册旧名，语义不同（见后端同名注释）。 */")
    lines.append("export function canonicalToolName(name: string): string {")
    lines.append("  return TOOL_RENAMES[name] ?? name;")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


# ─── Main + check mode ──────────────────────────────────────────────────


def _write_or_check(path: Path, content: str, check: bool) -> bool:
    """Write `content` to `path`. If `check`, instead compare and return True on diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if check:
        return existing != content
    path.write_text(content, encoding="utf-8")
    return False


def _ensure_init(path: Path) -> None:
    init = path / "__init__.py"
    if not init.exists():
        init.write_text(_HEADER + '"""Auto-generated package — do not add hand-written code."""\n', encoding="utf-8")


def main(check: bool = False) -> int:
    tools = load_aliases()
    validate_kinds(tools)
    retired = load_retired()
    py_dir = _gen_dir_py()
    ts_dir = _gen_dir_ts()
    py_dir.mkdir(parents=True, exist_ok=True)
    ts_dir.mkdir(parents=True, exist_ok=True)
    _ensure_init(py_dir)

    outputs: list[tuple[Path, str]] = [
        (py_dir / "tool_names.py", render_tool_names_py(tools, retired)),
        (py_dir / "role_bindings.py", render_role_bindings_py(tools)),
        (py_dir / "legacy_trace_names.py", render_legacy_trace_names_py(tools)),
        (py_dir / "tool_renames.py", render_tool_renames_py(tools)),
        (py_dir / "tool_display.py", render_tool_display_py(tools)),
        (py_dir / "tool_kinds.py", render_tool_kinds_py(tools)),
        (ts_dir / "tool-display.ts", render_tool_display_ts(tools)),
    ]

    diff_count = 0
    for path, content in outputs:
        if _write_or_check(path, content, check):
            diff_count += 1

    if check and diff_count:
        return 1
    if not check:
        pass
    return 0


def cli() -> None:
    ap = argparse.ArgumentParser(description="Codegen tool_aliases.yaml → 7 outputs")
    ap.add_argument("--check", action="store_true", help="Verify generated files match yaml (CI mode)")
    args = ap.parse_args()
    sys.exit(main(check=args.check))


if __name__ == "__main__":
    cli()
