"""Skill index (progressive disclosure) + tool handlers 单元测试."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from bowhead_skill.tools import build_skill_tools

from bowhead_skill import SkillCatalog, SkillRuntime, render_index, substitute_args

if TYPE_CHECKING:
    from pathlib import Path


def _mk(
    root: Path,
    name: str,
    desc: str,
    script: str = "import json, argparse\np=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--workdir');a=p.parse_args()\nopen(a.workdir+'/result.json','w').write(json.dumps({'echo': json.loads(open(a.input).read())}))\n",
) -> Path:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nBody for {name} with $ARGUMENTS\n",
        encoding="utf-8",
    )
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text(script, encoding="utf-8")
    return d


def test_render_index_empty(tmp_path: Path) -> None:
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    assert render_index(cat) == ""


def test_render_index_lists_skills(tmp_path: Path) -> None:
    _mk(tmp_path, "pdf", "Create & extract PDFs")
    _mk(tmp_path, "pptx", "Create slide decks")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    idx = render_index(cat)
    assert "pdf" in idx and "pptx" in idx
    assert "skill_read(name)" in idx
    assert "skill_invoke(name" in idx


def test_render_index_truncates_long_desc(tmp_path: Path) -> None:
    _mk(tmp_path, "x", "a" * 500)
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    idx = render_index(cat, max_desc_chars=50)
    assert "…" in idx
    lines = [line for line in idx.splitlines() if line.startswith("- **x**")]
    assert len(lines[0]) < 100


def test_substitute_args_simple() -> None:
    assert substitute_args("hi $ARGUMENTS", "world") == "hi world"
    assert substitute_args("hi ${ARGUMENTS}", "world") == "hi world"


def test_substitute_args_positional() -> None:
    s = substitute_args("$0 and $1 then $ARGUMENTS", "apple pear banana")
    assert s == "apple and pear then apple pear banana"


def test_substitute_args_missing_index() -> None:
    assert substitute_args("x=$3", "one two") == "x="


# ─── tools ──────────────────────────────────────────────────
async def test_build_skill_tools_shape(tmp_path: Path) -> None:
    _mk(tmp_path, "pdf", "Create PDFs")
    _mk(tmp_path, "xlsx", "Create XLSX")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    tools = build_skill_tools(cat, SkillRuntime())
    names = {t.name for t in tools}
    # Batch 5ⓑ adds a flat `python_exec` by default; the test below separately covers flag OFF.
    # Together they ensure rollback removes only this tool and no others.
    assert names == {"skill_list", "skill_read", "skill_invoke", "python_exec"}

    list_tool = next(t for t in tools if t.name == "skill_list")
    listed = await list_tool.handler({}, None)
    assert {s["name"] for s in listed} == {"pdf", "xlsx"}


async def test_skill_list_includes_granted_creator_only(tmp_path: Path) -> None:
    """#4: skill_list 与 resolve_skill 同款可见性——creator_only 但被分享(prefetch grant
    命中 oskill_* id)的 team_skill 要进 list,否则 Lead 见不到名、无法点名调用。"""
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    list_tool = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_list")

    rec = SimpleNamespace(name="secret-recipe", visibility="creator_only", created_by="u_alice", id="oskill_1", description="d")

    class _OStore:
        async def list(self, team_id, only_enabled=True):
            return [rec]

    base = {"team_id": "team_1", "team_skill_store": _OStore(), "user_id": "u_bob"}
    # bob without a grant → creator_only is invisible.
    no_grant = await list_tool.handler({}, SimpleNamespace(**base, granted_team_skill_ids=None))
    assert "secret-recipe" not in {s["name"] for s in no_grant}
    # bob with a grant matching oskill_1 → included in the list (consistent with resolve_skill).
    granted = await list_tool.handler({}, SimpleNamespace(**base, granted_team_skill_ids=frozenset({"oskill_1"})))
    assert "secret-recipe" in {s["name"] for s in granted}
    # A grant matching another ID → still invisible (exact ID matching, not blanket access).
    other = await list_tool.handler({}, SimpleNamespace(**base, granted_team_skill_ids=frozenset({"oskill_x"})))
    assert "secret-recipe" not in {s["name"] for s in other}


async def test_read_substitutes_args(tmp_path: Path) -> None:
    _mk(tmp_path, "pdf", "Create PDFs")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    tools = build_skill_tools(cat, SkillRuntime())
    read = next(t for t in tools if t.name == "skill_read")
    out = await read.handler({"name": "pdf", "args": "Q1"}, None)
    assert out["name"] == "pdf"
    assert "Q1" in out["body"]


async def test_invoke_runs_script(tmp_path: Path) -> None:
    _mk(tmp_path, "echo", "echo")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    tools = build_skill_tools(cat, SkillRuntime())
    inv = next(t for t in tools if t.name == "skill_invoke")
    result = await inv.handler({"name": "echo", "input": {"hello": "world"}}, None)
    assert result["ok"] is True
    assert result["result_json"]["echo"] == {"hello": "world"}


async def test_invoke_missing_skill(tmp_path: Path) -> None:
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    tools = build_skill_tools(cat, SkillRuntime())
    inv = next(t for t in tools if t.name == "skill_invoke")
    r = await inv.handler({"name": "nope"}, None)
    assert "error" in r


# ─── M137 follow-up: skill.read ref subdocuments ──────────────────────


def _mk_with_refs(root: Path, name: str, refs: dict[str, str]) -> Path:
    """造一个含 references/ 子目录的 skill."""
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: with references\n---\n\nMain body of {name}\n",
        encoding="utf-8",
    )
    refs_dir = d / "references"
    refs_dir.mkdir()
    for ref_name, ref_body in refs.items():
        (refs_dir / ref_name).write_text(ref_body, encoding="utf-8")
    return d


async def test_read_lists_references(tmp_path: Path) -> None:
    _mk_with_refs(tmp_path, "x", {"alpha.md": "alpha body", "beta.md": "beta body"})
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    tools = build_skill_tools(cat, SkillRuntime())
    read = next(t for t in tools if t.name == "skill_read")
    out = await read.handler({"name": "x"}, None)
    assert out.get("references") == ["alpha.md", "beta.md"]
    assert "Main body" in out["body"]


async def test_read_reference_returns_specific_file(tmp_path: Path) -> None:
    _mk_with_refs(tmp_path, "x", {"alpha.md": "alpha content"})
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    tools = build_skill_tools(cat, SkillRuntime())
    read = next(t for t in tools if t.name == "skill_read")
    out = await read.handler({"name": "x", "ref": "alpha"}, None)
    assert out["ref"] == "alpha.md"
    assert out["body"] == "alpha content"


async def test_read_reference_with_md_suffix(tmp_path: Path) -> None:
    """传 'alpha.md' 也可以."""
    _mk_with_refs(tmp_path, "x", {"alpha.md": "alpha"})
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    read = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_read")
    out = await read.handler({"name": "x", "ref": "alpha.md"}, None)
    assert out["body"] == "alpha"


async def test_read_nested_json_and_script_source(tmp_path: Path) -> None:
    _mk_with_refs(tmp_path, "x", {})
    nested = tmp_path / "x" / "references" / "metrics"
    nested.mkdir()
    (nested / "rules.json").write_text('{"margin": 0.2}')
    scripts = tmp_path / "x" / "scripts" / "nested"
    scripts.mkdir(parents=True)
    (scripts / "run.py").write_text("print('nested')")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    read = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_read")
    detail = await read.handler({"name": "x"}, None)
    assert detail["scripts"] == ["nested/run.py"]
    assert detail["references"] == ["metrics/rules.json"]
    assert (await read.handler({"name": "x", "ref": "metrics/rules.json"}, None))["body"] == '{"margin": 0.2}'
    assert (await read.handler({"name": "x", "file": "scripts/nested/run.py"}, None))["body"] == "print('nested')"


async def test_read_reference_missing(tmp_path: Path) -> None:
    _mk_with_refs(tmp_path, "x", {"alpha.md": "x"})
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    read = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_read")
    out = await read.handler({"name": "x", "ref": "nope"}, None)
    assert "not found" in out["error"]
    assert out["available"] == ["alpha.md"]


async def test_read_reference_path_traversal_blocked(tmp_path: Path) -> None:
    """不允许 .. / / 等路径分隔符防爬其他目录."""
    _mk_with_refs(tmp_path, "x", {"alpha.md": "x"})
    # Also create a sibling file that an attempted traversal could expose.
    (tmp_path / "secret.md").write_text("SECRET", encoding="utf-8")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    read = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_read")
    out = await read.handler({"name": "x", "ref": "../secret"}, None)
    assert "invalid ref" in out["error"]


async def test_read_no_references_dir(tmp_path: Path) -> None:
    """没 references/ 目录的 skill：references 字段为空数组."""
    _mk(tmp_path, "x", "no refs")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    read = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_read")
    out = await read.handler({"name": "x"}, None)
    assert out["references"] == []


async def test_read_returns_pipes_output_to_hint(tmp_path: Path) -> None:
    """frontmatter 含 pipes_output_to → skill.read 返结果含 next_step_hint."""
    d = tmp_path / "src_skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: src_skill\ndescription: pipes test\npipes_output_to: target_skill\n---\n\nbody\n",
        encoding="utf-8",
    )
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    read = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_read")
    out = await read.handler({"name": "src_skill"}, None)
    assert out.get("pipes_output_to") == "target_skill"
    assert "skill_read" in out.get("next_step_hint", "")
    assert "target_skill" in out.get("next_step_hint", "")


async def test_read_no_pipes_output_to_no_hint(tmp_path: Path) -> None:
    _mk(tmp_path, "x", "no pipes")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    read = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_read")
    out = await read.handler({"name": "x"}, None)
    assert "pipes_output_to" not in out
    assert "next_step_hint" not in out


class _FakeSkillStore:
    def __init__(self, instances: list[SimpleNamespace]) -> None:
        self._instances = {(i.user_id, i.name): i for i in instances}

    async def get(self, *, user_id: str, name: str):
        return self._instances.get((user_id, name))

    async def list_for_user(self, *, user_id: str):
        return [i for (uid, _name), i in self._instances.items() if uid == user_id]


def _mk_user_skill(root: Path, user_id: str, name: str, description: str) -> Path:
    d = root / ".data" / "user_skills" / user_id / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1.0.0\n---\n\nUser body {name}\n",
        encoding="utf-8",
    )
    return d


async def test_skill_tools_resolve_only_current_user_skills(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    _mk(builtin_root, "builtin", "Builtin")
    own_dir = _mk_user_skill(tmp_path, "u1", "own-sop", "Own SOP")
    other_dir = _mk_user_skill(tmp_path, "u2", "other-sop", "Other SOP")
    store = _FakeSkillStore(
        [
            SimpleNamespace(
                user_id="u1",
                name="own-sop",
                source_type="user_upload",
                source_path=str(own_dir),
                enabled=True,
            ),
            SimpleNamespace(
                user_id="u2",
                name="other-sop",
                source_type="user_upload",
                source_path=str(other_dir),
                enabled=True,
            ),
        ]
    )
    ctx = SimpleNamespace(user_id="u1", skill_instance_store=store)
    cat = SkillCatalog(roots=[builtin_root])
    cat.refresh()
    tools = build_skill_tools(cat, SkillRuntime())
    list_tool = next(t for t in tools if t.name == "skill_list")
    read = next(t for t in tools if t.name == "skill_read")

    listed = await list_tool.handler({}, ctx)
    assert {s["name"] for s in listed} == {"builtin", "own-sop"}
    own = await read.handler({"name": "own-sop"}, ctx)
    assert own["name"] == "own-sop"
    other = await read.handler({"name": "other-sop"}, ctx)
    assert other == {"error": "skill 'other-sop' not found"}


async def test_user_skill_collision_does_not_disable_or_shadow_global(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    _mk(builtin_root, "public", "Builtin Public")
    collision_dir = _mk_user_skill(tmp_path, "u1", "public", "User Public")
    store = _FakeSkillStore(
        [
            SimpleNamespace(
                user_id="u1",
                name="public",
                source_type="user_upload",
                source_path=str(collision_dir),
                enabled=False,
            ),
        ]
    )
    ctx = SimpleNamespace(user_id="u1", skill_instance_store=store)
    cat = SkillCatalog(roots=[builtin_root])
    cat.refresh()
    read = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_read")

    out = await read.handler({"name": "public"}, ctx)

    assert out["name"] == "public"
    assert out["description"] == "Builtin Public"
    assert "Body for public" in out["body"]


async def test_skill_list_filters_team_creator_only(tmp_path: Path) -> None:
    """P1（review）：skill_list 的 team 分支按 _visibility_ok 过滤——team_wide 全 team 成员见、
    creator_only 只 created_by 见。原 adapter 缺 .list() → team skill 永不进 skill_list（发现断），
    且若不过滤会把 creator_only 的名/描述泄漏给全 team。"""
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()  # Empty catalog; test the team branch.
    list_tool = next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_list")

    class _TeamStore:
        async def list(self, team_id: str, only_enabled: bool = True):
            return [
                SimpleNamespace(name="team-sop", description="团队 SOP", visibility="team_wide", created_by="u_alice"),
                SimpleNamespace(name="alice-private", description="她的私有", visibility="creator_only", created_by="u_alice"),
            ]

    ctx = SimpleNamespace(user_id="u_bob", team_id="team_a", skill_instance_store=None, team_skill_store=_TeamStore())
    names = {s["name"] for s in await list_tool.handler({}, ctx)}
    assert "team-sop" in names  # team_wide → visible to bob.
    assert "alice-private" not in names  # creator_only by alice → invisible to bob.
