"""SKILL.md 加载器.

解析 Anthropic skills 规范兼容的 SKILL.md：
    ---
    name: pdf
    description: …
    allowed-tools: Bash(python3), Read, Write
    version: 1.2.0
    ---

    # PDF Skill
    （markdown body）

production 约束：
- 严格 frontmatter 必需字段：name, description
- allowed-tools 可选，我们不用来限制（我们用自己的沙箱）
- body_md 不执行，只作为 LLM 可读文档
- scripts 目录扫描：skill_dir/scripts/*.py 全部列出
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml

from bowhead_skill.builtin_display import builtin_display_titles, builtin_listing_descriptions
from bowhead_skill.readonly_entry import ReadOnlyEntry, parse_read_only_entry
from bowhead_skill.script_langs import entry_extensions

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)


@dataclass
class SkillSpec:
    """一份已加载的 skill 元数据 + 磁盘位置."""

    name: str
    description: str
    source_dir: Path  # Skill root directory (contains SKILL.md).
    body_md: str  # SKILL.md body (for the LLM to read).
    scripts: list[Path] = field(default_factory=list)
    version: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    # Other Skills that must be granted with this Skill. This is authoring
    # metadata; runtime access still comes from exact grants.
    required_skills: list[str] = field(default_factory=list)
    # pptx-from-template M2 (wheelhouse) — long-tail python deps a skill declares
    # once in SKILL.md frontmatter (`sandbox_requirements:`), installed at
    # python_sandbox invoke time from the offline hash-pinned wheelhouse
    # (--no-index --require-hashes, --network=none never relaxed). Additive,
    # defaults empty → backward compatible. Threaded by tools.py into input.json.
    sandbox_requirements: list[str] = field(default_factory=list)
    # Examples of actual user requests. ⚠️ Current state (G19, docs/skills-tools-ssot.md §5-G19): parsed and
    # shown on the /api/v1/skills detail page; **no runtime matching/recall consumer exists**. Skill triggering
    # actually means the LLM reads description. The planned vector-recall pipeline was never built; once team/user skill volume
    # grows, decide whether to implement recall or remove the field.
    trigger_examples: list[str] = field(default_factory=list)
    # audience (2026-07-03, docs/skills-tools-ssot.md §5-G4): whose system-prompt catalog
    # shows this skill. "all" (default) = visible to the Lead routing catalog and peers; "peer" =
    # peer execution details only (analyst-method-*/analyst-sop-*/python_sandbox, etc.), excluded from
    # the Lead catalog. This controls presentation, not capability access; skill_read/invoke availability depends on
    # role_bindings + grants. Unknown values fall back to "all".
    audience: str = "all"
    # Display metadata: `title` is the default user-facing title; `title_zh` is
    # an explicit Chinese title. Builtin defaults use English while authored titles
    # retain their original language. `category` is a functional category slug (four built-in categories:
    # analysis/documents/design/platform). These are presentation-only fields; runtime routing/
    # capabilities do not consume them. When absent, the frontend infers a fallback from the slug prefix.
    title: str = ""
    category: str = ""
    # kind (excel RC-1 fix, 2026-07): "tool" (default, has an executable entry script) vs
    # "knowledge" (reference-only, e.g. xlsx; read with skill_read, execute code through python_sandbox).
    # tools.py `_invoke` redirects skill_invoke execution calls for knowledge skills,
    # eliminating misrouting such as invoking xlsx as executable → available contains only office bundle __init__.py files →
    # LLM selects __init__.py → entry_script not found. Unknown values fall back to "tool".
    kind: str = "tool"
    # Original frontmatter, retained for callers to access extension fields.
    frontmatter: dict = field(default_factory=dict)
    # Opt-in declaration only; read/invoke authority still belongs to the runtime.
    read_only_entry: ReadOnlyEntry | None = None
    # Append optional display metadata to preserve positional construction compatibility.
    title_zh: str = ""
    # User-facing picker text for installed built-ins, keyed by interface locale (builtin_listings.py).
    # Presentation only: the model reads `description` and the body, never this text.
    listing_descriptions: dict[str, str] = field(default_factory=dict)

    @property
    def script_dir(self) -> Path:
        return self.source_dir / "scripts"

    @property
    def script_names(self) -> list[str]:
        """执行身份必须保留相对于 scripts/ 的路径，不得压成 basename。"""
        return [path.relative_to(self.script_dir).as_posix() for path in self.scripts]


def split_skill_markdown(raw: str) -> tuple[str, str]:
    """Split the standard manifest without loading YAML or accessing files."""
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError("missing YAML frontmatter (--- ... ---)")
    return match.group(1), match.group(2)


def load_skill(skill_dir: Path) -> SkillSpec:
    """从 skill 目录加载 SkillSpec。失败抛 ValueError."""
    skill_dir = skill_dir.resolve()
    md_path = skill_dir / "SKILL.md"
    if not md_path.is_file():
        raise ValueError(f"SKILL.md not found under {skill_dir}")

    raw = md_path.read_text(encoding="utf-8")
    try:
        frontmatter_text, body_text = split_skill_markdown(raw)
    except ValueError as exc:
        raise ValueError(f"{md_path}: {exc}") from exc

    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{md_path}: YAML parse error: {e}") from e

    if not isinstance(frontmatter, dict):
        raise ValueError(f"{md_path}: frontmatter must be a mapping")

    name = str(frontmatter.get("name") or "").strip()
    desc = str(frontmatter.get("description") or "").strip()
    if not name:
        raise ValueError(f"{md_path}: frontmatter.name is required")
    if not desc:
        raise ValueError(f"{md_path}: frontmatter.description is required")

    scripts_dir = skill_dir / "scripts"
    scripts: list[Path] = []
    if scripts_dir.is_dir():
        # Top-level and subdirectories (Anthropic skills include paths such as scripts/office/*.py).
        # excel RC-1 fix: exclude __init__.py, a package-internal mechanism that is never an entry
        # script but appears in `available` and often sorts first, prompting the LLM to execute it →
        # entry_script not found. Actual executable scripts are not named __init__.py.
        # skill-multiruntime S1 (D3): scan multiple extensions from the single-source-of-truth allowlist (flag-aware:
        # when OFF, entry_extensions() narrows back to {'.py'}, preserving byte-identical behavior).
        allowed = entry_extensions()
        for p in sorted(scripts_dir.rglob("*")):
            if p.is_file() and p.name != "__init__.py" and p.suffix in allowed:
                scripts.append(p)

    allowed_tools_raw = frontmatter.get("allowed-tools") or frontmatter.get("allowed_tools") or []
    if isinstance(allowed_tools_raw, str):
        allowed_tools = [s.strip() for s in allowed_tools_raw.split(",") if s.strip()]
    elif isinstance(allowed_tools_raw, list):
        allowed_tools = [str(s) for s in allowed_tools_raw]
    else:
        allowed_tools = []

    required_skills_raw = frontmatter.get("required_skills") or frontmatter.get("required-skills") or []
    if isinstance(required_skills_raw, str):
        required_skills = [value.strip() for value in required_skills_raw.split(",") if value.strip()]
    elif isinstance(required_skills_raw, list):
        required_skills = [str(value).strip() for value in required_skills_raw if str(value).strip()]
    else:
        required_skills = []
    required_skills = list(dict.fromkeys(value for value in required_skills[:32] if value != name and len(value) <= 128))

    # pptx-from-template M2 (wheelhouse) — parse `sandbox_requirements`
    # (list OR comma-string, exactly like allowed-tools above). LLM-supplied
    # values are never trusted; the runner + sandbox_bootstrap re-validate each
    # entry (strict regex + wheelhouse lock-set membership) before any install.
    sandbox_reqs_raw = frontmatter.get("sandbox_requirements") or frontmatter.get("sandbox-requirements") or []
    if isinstance(sandbox_reqs_raw, str):
        sandbox_requirements = [s.strip() for s in sandbox_reqs_raw.split(",") if s.strip()]
    elif isinstance(sandbox_reqs_raw, list):
        sandbox_requirements = [str(s).strip() for s in sandbox_reqs_raw if str(s).strip()]
    else:
        sandbox_requirements = []

    audience_raw = str(frontmatter.get("audience") or "all").strip().lower()
    audience = audience_raw if audience_raw in ("all", "peer") else "all"

    title = str(frontmatter.get("title") or "").strip()
    title_zh = str(frontmatter.get("title_zh") or "").strip()
    title, title_zh = builtin_display_titles(skill_dir, name=name, title=title, title_zh=title_zh)
    listing_descriptions = builtin_listing_descriptions(skill_dir, name=name)
    category = str(frontmatter.get("category") or "").strip().lower()

    kind_raw = str(frontmatter.get("kind") or "tool").strip().lower()
    kind = kind_raw if kind_raw in ("tool", "knowledge") else "tool"

    examples_raw = frontmatter.get("trigger_examples") or frontmatter.get("trigger-examples") or []
    if isinstance(examples_raw, str):
        trigger_examples = [examples_raw.strip()] if examples_raw.strip() else []
    elif isinstance(examples_raw, list):
        trigger_examples = [str(s).strip() for s in examples_raw if str(s).strip()]
    else:
        trigger_examples = []

    read_only_entry = None
    if "read_only_entry" in frontmatter:
        read_only_entry = parse_read_only_entry(frontmatter["read_only_entry"])
        # Loading metadata must remain possible while an import awaits missing files.
        # Read-only admission must separately validate the complete current bundle.

    return SkillSpec(
        name=name,
        description=desc,
        source_dir=skill_dir,
        body_md=body_text,
        scripts=scripts,
        version=(str(frontmatter["version"]) if frontmatter.get("version") else None),
        allowed_tools=allowed_tools,
        required_skills=required_skills,
        sandbox_requirements=sandbox_requirements,
        trigger_examples=trigger_examples,
        audience=audience,
        title=title,
        title_zh=title_zh,
        listing_descriptions=listing_descriptions,
        category=category,
        kind=kind,
        frontmatter=frontmatter,
        read_only_entry=read_only_entry,
    )
