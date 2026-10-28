"""Skill index 渲染 + $ARGUMENTS 替换.

Claude Code 的 "progressive disclosure" 核心：
    - system prompt 只放 name + desc 简表（每条 ≤ 150 字符）
    - LLM 需要详情时调 read_skill(name) 拉完整 SKILL.md
    - 运行时调 invoke_skill(name, script, args) 跑 scripts/*.py

这个模块提供：
    - render_index(catalog) -> str     # 给 system prompt 的简表
    - substitute_args(body, args) -> str   # $ARGUMENTS / $0 / $1... 替换
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bowhead_skill.catalog import SkillCatalog

INDEX_HEADER = """## Available Skills

Bowhead ships a skill library. Use `skill_read(name)` to load full instructions
for a skill, and `skill_invoke(name, script, args)` to run its helper scripts.
Skills are invoked **only when the user's request matches the description**:
"""

# Lead view header (2026-07-03, docs/skills-tools-ssot.md §5-G4):
# Lead has no skill_read/skill_invoke (role_bindings limits them to analysts); the old header misled it
# into calling unavailable tools (a remnant of the ω incident). Lead should discover capabilities and route requests.
LEAD_INDEX_HEADER = """## Available Skills (platform capability catalog — for routing)

These capabilities are loaded and executed by an analyst peer after it is dispatched; you (Lead) have no `skill_read` /
`skill_invoke`, so do not call them directly. When a capability is needed: route to a peer that can run it and write the skill
name into the `subagent_spawn` task description. Consider one only when the user's request matches its description:
"""


def render_index(
    catalog: SkillCatalog,
    *,
    max_desc_chars: int = 160,
    long_desc_chars: int = 300,
    audience_view: str = "peer",
) -> str:
    """渲染 (name, description) 索引 markdown.

    Sprint ω Phase 3 (r3.1 §2.10):
    - Default cap 160 chars per skill (r3.1 verified ≤ 80 token soft cap)
    - Skills with `description_long: true` frontmatter flag get the higher
      `long_desc_chars` cap (300 char ≈ 200 token) — for skills like
      `analyst-framework` and `xlsx` whose trigger semantics need extra room.
    - Deterministic-sorted by name (list_specs uses sorted keys) — required
      for DeepSeek prefix-cache stability (Spike Q5 invariant).

    ``audience_view``（2026-07-03，取代原 exclude_prefixes 硬编码）：
    - "lead" = 过滤掉 frontmatter ``audience: peer`` 的条目（analyst-method-*/
      analyst-sop-*/python_sandbox 等 peer 执行细节）+
      路由语义 header。谁进 Lead 目录由 skill 自己的 frontmatter 声明（配置
      驱动，原 ROUTE-5 的前缀硬编码退役）。**这是唯一有 prod 消费者的分支**
      （skill_access.render_lead_skill_index）。
    - "peer"（默认）= 全量目录 + INDEX_HEADER。⚠️ review-F7：G3/G6 之后 peer 的
      skill 感知改由 prompt 文本 + skill_list 承载，**本分支当前无 prod 消费者**
      （仅测试用）。保留为 bowhead_skill 的公共 API + 未来 peer-catalog 复用点；
      改 INDEX_HEADER / peer 分支不影响任何 live peer 行为，别误以为动了线上。
    """
    specs = catalog.list_specs()
    if not specs:
        return ""
    lead_view = audience_view == "lead"
    lines = [LEAD_INDEX_HEADER if lead_view else INDEX_HEADER, ""]
    for s in specs:
        if lead_view and getattr(s, "audience", "all") == "peer":
            continue
        desc = s.description
        is_long = bool(s.frontmatter.get("description_long", False))
        cap = long_desc_chars if is_long else max_desc_chars
        if len(desc) > cap:
            desc = desc[: cap - 1] + "…"
        lines.append(f"- **{s.name}** — {desc}")
    return "\n".join(lines) + "\n"


_ARG_PLACEHOLDER = re.compile(r"\$ARGUMENTS|\$\{ARGUMENTS\}|\$(\d+)")


def substitute_args(body: str, args: str | list[str], *, max_output_bytes: int | None = None) -> str:
    """按 Claude Code 约定替换 $ARGUMENTS / $0 / $1 ....

    $ARGUMENTS / ${ARGUMENTS}  → 整串 args
    $0 $1 $2 ...              → 按 shlex 切分后的第 N 个
    """
    if isinstance(args, list):
        full = " ".join(shlex.quote(a) for a in args)
        parts = args
    else:
        full = args
        try:
            parts = shlex.split(args) if args else []
        except ValueError:
            parts = args.split()

    def _repl(m: re.Match[str]) -> str:
        num = m.group(1)
        if num is None:
            return full
        i = int(num)
        return parts[i] if 0 <= i < len(parts) else ""

    if max_output_bytes is not None:
        if type(max_output_bytes) is not int or max_output_bytes < 0:
            raise ValueError("skill_argument_output_limit_invalid")
        size = 0
        previous = 0
        for match in _ARG_PLACEHOLDER.finditer(body):
            size += len(body[previous : match.start()].encode("utf-8")) + len(_repl(match).encode("utf-8"))
            if size > max_output_bytes:
                raise ValueError("skill_argument_output_limit_exceeded")
            previous = match.end()
        if size + len(body[previous:].encode("utf-8")) > max_output_bytes:
            raise ValueError("skill_argument_output_limit_exceeded")
    return _ARG_PLACEHOLDER.sub(_repl, body)
