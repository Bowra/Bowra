"""excel RC-1 fix — knowledge-kind skills are not executable; loader hygiene.

Root cause (prod): xlsx is a knowledge-reference skill but its Anthropic
office-bundle helpers (scripts/office/**, incl. two __init__.py) got rglob'd into
``spec.scripts`` → skill_invoke(name="xlsx") returned ``available=[__init__.py,…]``
→ LLM picked __init__.py → runtime "entry_script not found". Fix:
  1. loader parses ``kind: knowledge|tool`` + excludes __init__.py from scripts.
  2. tools._invoke redirects a knowledge-kind execution call to skill_read +
     python_sandbox (never dispatches its internal scripts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_skill.loader import load_skill
from bowhead_skill.tools import build_skill_tools

from bowhead_skill import SkillCatalog

if TYPE_CHECKING:
    from pathlib import Path


class _Rt:
    def __init__(self) -> None:
        self.ran = False

    async def run(self, spec, *, entry_script, input_data, extra_args):
        self.ran = True
        raise AssertionError("knowledge skill must not be executed")


def _mk_skill(root: Path, name: str, *, kind_fm: str = "", scripts: dict[str, str] | None = None) -> Path:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\nname: {name}\n{kind_fm}description: d\n---\n\nbody\n", encoding="utf-8")
    if scripts:
        for rel, content in scripts.items():
            p = d / "scripts" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    return d


def _invoke_tool(cat: SkillCatalog, rt: _Rt):
    return next(t for t in build_skill_tools(cat, rt) if t.name == "skill_invoke")


# ─── loader: kind parsing + __init__.py exclusion ──────────────────────


def test_loader_parses_kind_knowledge(tmp_path: Path) -> None:
    d = _mk_skill(tmp_path, "myknow", kind_fm="kind: knowledge\n")
    assert load_skill(d).kind == "knowledge"


def test_loader_kind_defaults_tool_and_unknown_falls_back(tmp_path: Path) -> None:
    assert load_skill(_mk_skill(tmp_path, "notool")).kind == "tool"
    assert load_skill(_mk_skill(tmp_path, "weird", kind_fm="kind: banana\n")).kind == "tool"


def test_loader_excludes_init_py_from_scripts(tmp_path: Path) -> None:
    d = _mk_skill(
        tmp_path,
        "withhelpers",
        scripts={
            "__init__.py": "",
            "office/__init__.py": "",
            "office/pack.py": "print('x')",
            "runner.py": "print('go')",
        },
    )
    names = [p.name for p in load_skill(d).scripts]
    assert "__init__.py" not in names
    assert set(names) == {"pack.py", "runner.py"}


# ─── tools._invoke: knowledge-kind is redirected, never executed ───────


async def test_invoke_knowledge_skill_is_redirected(tmp_path: Path) -> None:
    # xlsx-shaped: knowledge skill whose only scripts are office-bundle helpers.
    _mk_skill(
        tmp_path,
        "xlsx",
        kind_fm="kind: knowledge\n",
        scripts={"office/__init__.py": "", "office/pack.py": "print('x')"},
    )
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _Rt()
    inv = _invoke_tool(cat, rt)
    out = await inv.handler({"name": "xlsx", "input": {"code": "df.sum()"}}, None)
    assert rt.ran is False  # never dispatched a script
    assert "error" in out
    assert "python_sandbox" in out["error"]
    assert "skill_read" in out["error"]
    # not the old cryptic paths:
    assert out.get("error") != "script required"


async def test_knowledge_skill_is_still_readable(tmp_path: Path) -> None:
    """The gate is on _invoke ONLY — skill_read of a knowledge skill still returns
    its body (else xlsx knowledge would be unreadable = worse than the bug)."""
    _mk_skill(tmp_path, "xlsx", kind_fm="kind: knowledge\n", scripts={"office/__init__.py": ""})
    (tmp_path / "xlsx" / "SKILL.md").write_text(
        "---\nname: xlsx\nkind: knowledge\ndescription: d\n---\n\n教 pandas / openpyxl 最佳实践\n", encoding="utf-8"
    )
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    read = next(t for t in build_skill_tools(cat, _Rt()) if t.name == "skill_read")
    out = await read.handler({"name": "xlsx"}, None)
    assert "body" in out
    assert "pandas" in str(out["body"])


async def test_invoke_tool_skill_still_runs(tmp_path: Path) -> None:
    # A normal tool skill with a single script still auto-selects + would run
    # (rt raises only for the knowledge assertion; here we just confirm no redirect).
    _mk_skill(tmp_path, "python_sandbox", scripts={"runner.py": "print('x')"})
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()

    class _OkRt:
        async def run(self, spec, *, entry_script, input_data, extra_args):
            class _R:
                ok = True
                exit_code = 0
                stdout = stderr = ""
                result_json: dict = {}
                outputs: list = []
                workdir = None
                duration_ms = 0
                error = None

            return _R()

    inv = next(t for t in build_skill_tools(cat, _OkRt()) if t.name == "skill_invoke")
    out = await inv.handler({"name": "python_sandbox", "script": "runner.py", "input": {"code": "x"}}, None)
    # tool skill is NOT redirected (no knowledge error)
    assert not (isinstance(out, dict) and "知识参考" in str(out.get("error", "")))


# ─── skill-multiruntime S3-R: mcp-builder is a knowledge authoring guide ──────


def test_mcp_builder_is_knowledge_kind() -> None:
    """mcp-builder 的脚本 (connections.py / evaluation.py) 开外连、spawn 任意 stdio
    子进程、读 ANTHROPIC_API_KEY —— 它们构建 EXTERNAL MCP server, 绝不该经 skill_invoke
    在 host 跑。归 kind:knowledge 让 tools._invoke 重定向 (改走 skill_read)。"""
    from pathlib import Path as _P

    d = _P(__file__).resolve().parents[1] / "catalog" / "mcp-builder"
    assert d.is_dir(), f"mcp-builder catalog dir missing: {d}"
    assert load_skill(d).kind == "knowledge"
