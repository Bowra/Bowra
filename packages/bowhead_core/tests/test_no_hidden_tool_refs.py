"""H1 守卫（tool-surfacing scoping, docs/tool-surfacing-scoping-2026-07-14.md）——

防「已隐藏的内置工具」被 prompt / skill 目录继续引用（引用腐烂复发）。

背景：BI 内置工具弃用时不删除、只在 tool_registry.py 标 ``visible_to_llm=False``
（handler 保留、对 LLM 隐藏，见 ``_OMICRON_DEPRECATED_BI``）。但没有任何机制拦截
prompt / skill catalog 里对它的**文字引用**——结果 ``bi_fetch_rows`` /
``bi_list_measures`` 等一批工具在 20 文件 60+ 处广告了 14 个月无人察觉，直到人工清理
（main ``c0a0dc76``）。本守卫是那次清理该留下的「机制性成果」，把不变量锁进 CI：

    prompt / skill 目录里出现的工具名  ∉  registry 里 visible_to_llm=False 的工具集

以后任何人把工具标 ``visible_to_llm=False`` 却漏清引用，CI 直接红，报文件+行号+工具名。

v1 覆盖面（60 处里的 58 处）：只扫 analyst prompts + skill catalog 这两处**纯散文/
配置**面。故意**不扫** tool_registry.py（弃用表本身必须写真名）与 subagent.py
（handler / deny 列表 + 结果解析必须保留真名——混合文件，v1 不硬啃，留 follow-up）。

匹配用**精确注册名 + 词边界**（``\\bbi_fetch_rows\\b``）：隐藏名都带 ``bi_`` 前缀，而
compute.md 的血统说明写的是**裸词**（"合并了 ask_question / fetch_rows …"，描述 bi_query
内部合并了哪些旧能力），精确名 + 词边界天然不误伤它们，故无需 allowlist。

v2 覆盖面（chart-surfacing-fix, 2026-07-16）：**加扫 tool schema description 本身**。
v1 只扫「散文面」（prompt / catalog 文件），漏掉了**离决策点最近的那一面**——工具自己的
``description`` + 各参数 ``description``，那是 LLM 决定「要不要用这个工具」时真正读的字节。
真实代价：``add_chart`` 的描述 14 个月写着「先调 ``bi_fetch_rows`` 拿 artifact_id」
（外加 4 处运行时 error/hint 同样指向它）——模型没有这工具 → chart 路径无法自我修复 →
转投 ``add_image`` 挂 matplotlib PNG。116 实测 7/13 起 image section 反超 chart 3-4 倍
（结构化数据 + 交互 + 数字溯源全丢）。v1 扫不到它，因为它在 canvas_tools.py 里、不在 prompts/ 下。

v2 有两条扫描面，覆盖「LLM 真读到的两处字节」：
- **结构化面**：``registry.to_schemas()`` 的 description（含 input_schema 里任意深度的
  description，见 ``_walk_descriptions``）——工具定义本身。**覆盖范围 = 本测试构建的 registry**：
  ``build_default_registry()`` + 两处 canvas 工具族。这**不是** gateway ``main.py`` 的完整
  ~12 registrar 链（web_search / memory / clarification / subagent 等独立 registrar 的 schema
  不在此面）；要纳入更多工具，在 ``_llm_visible_schemas`` 显式补注册。别把它当「未来任何工具自动进面」。
- **文本面**：canvas 工具 handler 源文件（``_canvas_tool_sources``）——锁住 4 处**回灌给 LLM 的
  运行时 error/hint** 串（它们不在 to_schemas 出口，只有 text-scan 抓得到）。仍**不扫**
  tool_registry.py / subagent.py（混合文件，弃用表/dispatch 必须写真名）。
"""

from __future__ import annotations

import re
from pathlib import Path

import bowhead_core.agents.prompts as _prompts_pkg
from bowhead_core.canvas_tools import register_canvas_tools
from bowhead_core.tool_registry import ToolRegistry, build_default_registry

from bowhead_skill.catalog import builtin_catalog_dir


def _hidden_tool_names() -> frozenset[str]:
    """Registry 里 ``visible_to_llm=False`` 的工具名集合（动态取，绝不硬编码）。

    ``build_default_registry()`` 内部已跑弃用 pass（omicron Phase C2），
    ``_OMICRON_DEPRECATED_BI`` 里**已注册**的名字会带上 ``visible_to_llm=False``。
    registry 是唯一真源——弃用表增删时本守卫自动跟随，无需改测试。
    """
    reg = build_default_registry()
    return frozenset(name for name, spec in reg._specs.items() if not spec.visible_to_llm)


def _prompts_dir() -> Path:
    """analyst prompt 根目录（锚定在真实包位置，随包移动而跟随）。"""
    init_path = _prompts_pkg.__file__
    assert init_path is not None, "bowhead_core.agents.prompts 无 __file__——包结构异常"
    return Path(init_path).resolve().parent


def _canvas_tool_sources() -> list[Path]:
    """canvas 写入工具的 handler 源文件（chart-surfacing-fix WP5 补）。

    为什么 text-scan 这个文件、却仍不扫 tool_registry.py / subagent.py：后两者是
    「混合文件」——弃用表 / dispatch / 结果解析**必须**写真名（隐藏工具名合法出现）。
    canvas_tools 是纯 canvas 写入器，**没有任何正当理由**提及某个隐藏 BI 工具；而它的
    handler body 里恰恰有 4 处**回灌给 LLM 的运行时 error/hint**（`_add_chart` 里
    「data_artifact_id required …」等），这些字节 LLM 真读得到，却不在 `to_schemas()` 出口
    → v2 结构化扫描扫不到。text-scan 源文件是锁住这 4 处的务实办法。
    """
    import bowhead_core.canvas_tools as _canvas

    out: list[Path] = []
    for mod in (_canvas,):
        f = getattr(mod, "__file__", None)
        assert f is not None, f"{mod.__name__} 无 __file__——包结构异常"
        out.append(Path(f).resolve())
    return out


def _scan_files() -> list[Path]:
    """textual 覆盖面：analyst prompts（*.py + *.md）+ skill catalog（*.md + evals/*.json）
    + canvas 工具 handler 源文件（含回灌给 LLM 的运行时 error/hint 串）。"""
    prompts_dir = _prompts_dir()
    catalog_dir = builtin_catalog_dir()
    # Directories must exist: rglob silently returns nothing otherwise, leaving the guard silent-dark.
    assert prompts_dir.is_dir(), f"prompts 目录不存在: {prompts_dir}"
    assert catalog_dir.is_dir(), f"skill catalog 目录不存在: {catalog_dir}"

    files: set[Path] = set()
    for pattern in ("*.py", "*.md"):
        files.update(prompts_dir.rglob(pattern))
    files.update(catalog_dir.rglob("*.md"))
    files.update(catalog_dir.glob("**/evals/*.json"))
    files.update(_canvas_tool_sources())
    return sorted(files)


def _repo_root() -> Path:
    # tests → bowhead_core → packages → <repo root> (only for relative paths in errors)
    return Path(__file__).resolve().parents[3]


def test_hidden_tools_have_a_nonempty_guard_target() -> None:
    """Sanity：registry 至少隐藏了 1 个工具，否则本守卫会 vacuously pass（哑火）。

    若这里失败，多半是 build_default_registry / ToolSpec.visible_to_llm 的语义漂了
    （弃用 pass 没跑、字段改名等）——先修那边，别让守卫悄悄失效。
    """
    hidden = _hidden_tool_names()
    assert hidden, (
        "registry 产出 0 个 hidden 工具——H1 守卫会空转。检查 tool_registry.py 的 _OMICRON_DEPRECATED_BI pass 与 ToolSpec.visible_to_llm 是否还在。"
    )


def test_no_hidden_tool_refs_in_prompts_or_catalog() -> None:
    """prompt / skill catalog / canvas 工具源文件 不得出现任何已隐藏工具的精确名。"""
    hidden = _hidden_tool_names()
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(hidden)) + r")\b")
    repo_root = _repo_root()

    hits: list[str] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                try:
                    shown: Path | str = path.relative_to(repo_root)
                except ValueError:
                    shown = path
                hits.append(f"{shown}:{lineno}: {match.group(1)}")

    assert not hits, (
        "以下位置引用了已 visible_to_llm=False 的内置工具（LLM 看不到、也调不到）——\n"
        "请改用 bi_query / bi_schema 替代（详见 docs/tool-surfacing-scoping-2026-07-14.md · H1）：\n" + "\n".join(hits)
    )


def _llm_visible_schemas() -> list[tuple[str, str, str]]:
    """LLM 真正收到的每段工具文案：``(工具名, 位置标签, 文本)``。

    走 ``to_schemas()``——已按 ``visible_to_llm`` 过滤，所以隐藏工具自身的 description
    天然不在扫描面（它们可以、也必须提自己的名字）。

    ⚠ 覆盖范围 = ``build_default_registry()`` + 两处 canvas 工具族，**不是** gateway
    ``main.py`` 的完整 registrar 链（web_search / memory / clarification / subagent /
    scheduled_job 等独立 registrar 的 schema 不在此）。要把某个新工具纳入守卫，在这里补一行注册。
    """
    reg: ToolRegistry = build_default_registry()
    register_canvas_tools(reg)

    out: list[tuple[str, str, str]] = []
    for schema in reg.to_schemas():
        out.append((schema.name, "description", schema.description or ""))
        # Recursively collect description strings at **any depth** in input_schema (properties / items /
        # nested objects / $defs may contain references), beyond top-level properties[*].description.
        for where, text in _walk_descriptions(schema.input_schema or {}):
            out.append((schema.name, where, text))
    return out


def _walk_descriptions(node: object, path: str = "input_schema") -> list[tuple[str, str]]:
    """深度遍历 JSON schema，产出每个 ``description`` 字符串及其路径标签。"""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "description" and isinstance(val, str):
                found.append((path, val))
            else:
                found.extend(_walk_descriptions(val, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found.extend(_walk_descriptions(item, f"{path}[{i}]"))
    return found


def test_llm_visible_tool_schemas_have_a_nonempty_guard_target() -> None:
    """Sanity：注册面至少产出 1 个可见工具，否则 v2 守卫会 vacuously pass（哑火）。"""
    schemas = _llm_visible_schemas()
    assert schemas, "to_schemas() 产出 0 段文案——注册链或 to_schemas() 语义漂了，先修那边，别让守卫空转。"
    names = {name for name, _, _ in schemas}
    assert "add_chart" in names, "add_chart 不在扫描面——register_canvas_tools 注册链断了，v2 守卫会漏掉它（正是 chart-surfacing-fix 的 gap 所在）。"


def test_no_hidden_tool_refs_in_llm_visible_tool_schemas() -> None:
    """工具 description / 参数 description 不得引用已隐藏（visible_to_llm=False）的工具。

    这一面比 prompt 更贴近决策点：模型就是读这段文字决定「要不要用这个工具、怎么用」。
    指向一个它没有的工具 = 教它走死路（chart-surfacing-fix 根因 A/B）。
    """
    hidden = _hidden_tool_names()
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(hidden)) + r")\b")

    hits: list[str] = []
    for tool_name, where, text in _llm_visible_schemas():
        for match in pattern.finditer(text):
            hits.append(f"{tool_name}.{where}: 引用了隐藏工具 {match.group(1)!r}")

    assert not hits, (
        "以下**LLM 可见工具**的文案引用了已 visible_to_llm=False 的工具（模型读得到、却调不到）——\n"
        "改用 bi_query / bi_schema，或改写成不依赖该工具的说法\n"
        "（详见 docs/sprints/chart-surfacing-fix/tech-design.md · WP5）：\n" + "\n".join(hits)
    )


# ─── Stale references to renamed tools (docs/design/工具改名迁移.md) ────────────────────
#
# Same issue and scan scope as hidden tools above: old names **still work** via read-path normalization,
# so prompts using them produce no errors or symptoms; the model merely sees a name inconsistent
# with the tool list (`to_schemas()` shows only current names) throughout the release batch.
#
# ★ This gate addresses an observed issue: during batch 1 renaming, upstream added text to `compute.md`
#   still using an old name; only a coincidental rebase conflict exposed it. Luck is insufficient.


def test_prompts_and_catalog_do_not_reference_renamed_old_names() -> None:
    import re

    from bowhead_core.agents._generated.tool_renames import TOOL_RENAMES

    assert TOOL_RENAMES, "改名表为空 ⇒ 本守卫 vacuously pass（哑火）"

    offenders: list[str] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        for old, new in TOOL_RENAMES.items():
            for m in re.finditer(rf"\b{re.escape(old)}\b", text):
                line_no = text.count("\n", 0, m.start()) + 1
                rel = path.relative_to(_repo_root())
                offenders.append(f"{rel}:{line_no}  {old} → 应写 {new}")
    assert not offenders, (
        "提示词 / skill 目录里还写着**改名前**的工具名。旧名仍可调通，所以这不会报错，"
        "但模型收到的名字与 `to_schemas()` 给它的工具清单不一致：\n" + "\n".join(offenders)
    )
