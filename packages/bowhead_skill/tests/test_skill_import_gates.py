"""skill-import 批1（tech-design r3 §3.4）：待处理导入在普通轮的四处生效闸——
skill_read / skill_invoke 一律拒（结构化 error_kind，不当瞬时故障重试）、skill_list 与 Lead 目录不列。
个人 skill 对本人恒解析，「本人可读」是空门，所以闸不区分调用者。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from bowhead_skill.effective import list_effective_skills
from bowhead_skill.tools import build_skill_tools

from bowhead_skill.catalog import SkillCatalog


@dataclass
class _Rec:
    name: str
    source_path: str
    source_type: str = "user_upload"
    enabled: bool = True
    import_status: str | None = None
    size_bytes: int = 1


class _Store:
    def __init__(self, recs: list[_Rec]) -> None:
        self._recs = {r.name: r for r in recs}

    async def get(self, *, user_id: str, name: str) -> _Rec | None:
        return self._recs.get(name)

    async def list_for_user(self, *, user_id: str) -> list[_Rec]:
        return list(self._recs.values())


class _Ctx:
    def __init__(self, store: _Store) -> None:
        self.user_id = "u1"
        self.team_id = None
        self.skill_instance_store = store
        self.team_skill_store = None
        self.session_id = "s_x"
        self.turn_id = "t_x"


class _Runtime:
    async def run(self, spec, *, entry_script, input_data, extra_args):  # noqa: ANN001
        raise AssertionError("pending skill must be denied before runtime")


def _mk_user_skill(root: Path, name: str) -> Path:
    d = root / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nbody\n", encoding="utf-8")
    (d / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    return d


def _tool(cat: SkillCatalog, name: str) -> Any:
    return next(t for t in build_skill_tools(cat, _Runtime()) if t.name == name)


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "1")
    monkeypatch.chdir(tmp_path)  # The personal bundle path guard uses CWD-relative .data/user_skills/<uid>.
    (tmp_path / "catalog").mkdir()
    user_root = tmp_path / ".data" / "user_skills" / "u1"
    pending_dir = _mk_user_skill(user_root, "pending-skill")
    ok_dir = _mk_user_skill(user_root, "ok-skill")
    store = _Store(
        [
            _Rec(name="pending-skill", source_path=str(pending_dir), import_status="pending"),
            _Rec(name="ok-skill", source_path=str(ok_dir), import_status="cleared"),
        ]
    )
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    return cat, _Ctx(store)


async def test_skill_read_denies_pending_with_structured_error(world) -> None:
    cat, ctx = world
    res = await _tool(cat, "skill_read").handler({"name": "pending-skill"}, ctx)
    assert res["error_kind"] == "skill_import_pending"
    assert "待处理" in res["error"] and "不要重试" in res["error"]
    ok = await _tool(cat, "skill_read").handler({"name": "ok-skill"}, ctx)
    assert "error" not in ok and ok["name"] == "ok-skill"


async def test_skill_invoke_denies_pending_before_any_runtime_call(world) -> None:
    cat, ctx = world
    res = await _tool(cat, "skill_invoke").handler({"name": "pending-skill", "script": "run.py", "input": {}}, ctx)
    assert res["error_kind"] == "skill_import_pending"


async def test_skill_list_and_lead_catalog_skip_pending(world) -> None:
    cat, ctx = world
    names = {s.name for s in await list_effective_skills(ctx, cat)}
    assert names == {"ok-skill"}
    listed = await _tool(cat, "skill_list").handler({}, ctx)
    assert [s["name"] for s in listed] == ["ok-skill"]
