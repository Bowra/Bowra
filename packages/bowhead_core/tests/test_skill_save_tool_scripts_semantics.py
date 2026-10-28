"""★闸装在**真实路径**上：skill_save_generated 工具层的 scripts 三态.

上一批我只在 `validate_and_save_generated` 上测了 `scripts=None ⇒ 保留旧脚本`，绿了；
但真实调用方（本工具、`POST /skills/generate`）都写着 `args.get("scripts") or []`，
把 None 压成了 []，于是那条语义在生产上**从来没生效过**——2026-09-02 在 116 实测：
agent 改正文只传 `body_md`，脚本照样全没（`script_count: 0`）。

教训：测函数入参时用了真实调用方产不出的值，等于测了一条不存在的路。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.skill_save_tool import register_skill_save_tool
from bowhead_core.tool_registry import ToolRegistry

from bowhead_core import skill_upload

_BODY = """---
name: gm-recipe
description: 渠道毛利核对
version: 1.0.0
---

## 步骤

1. {step}
"""


class _FakeStore:
    def __init__(self):
        self.rows: list[SimpleNamespace] = []

    async def list_for_user(self, *, user_id):
        return self.rows

    async def get(self, *, user_id, name):
        # SkillInstanceStore.get: the save tool rereads the row after inspection (21f57385f).
        return next((row for row in self.rows if row.name == name), None)

    async def upsert(self, **kw):
        self.rows = [r for r in self.rows if r.name != kw["name"]]
        self.rows.append(SimpleNamespace(**{"source_type": "user_generated", "size_bytes": 0, **kw}))
        return self.rows[-1]


def _handler():
    reg = ToolRegistry()
    register_skill_save_tool(reg)
    return reg.get("skill_save_generated").handler


def _ctx(store):
    return SimpleNamespace(skill_instance_store=store, user_id="alice", turn_id=None, skill_catalog=None)


async def _seed(tmp_path, handler, store):
    """先建一版带脚本的，再在盘上补 references（references 只经上传/promote 进来）。"""
    await handler({"body_md": _BODY.format(step="原始"), "scripts": [{"name": "s1.py", "content": "x = 1\n"}]}, _ctx(store))
    d = Path(".data") / "user_skills" / "alice" / "gm-recipe"
    (d / "references").mkdir(parents=True, exist_ok=True)
    (d / "references" / "notes.md").write_text("口径备注", encoding="utf-8")
    return d


@pytest.mark.asyncio
async def test_omitting_scripts_keeps_them(tmp_path, monkeypatch):
    """★ 只改正文（不带 scripts 键）⇒ 旧脚本原样留着。这是对话编辑的真实形状。"""
    monkeypatch.chdir(tmp_path)
    handler, store = _handler(), _FakeStore()
    d = await _seed(tmp_path, handler, store)

    out = await handler({"body_md": _BODY.format(step="改过了"), "overwrite": True}, _ctx(store))

    assert out.get("ok") is True, out
    assert out["script_count"] == 1, "工具层把「没提 scripts」压成了空表 ⇒ 脚本被删"
    assert (d / "scripts" / "s1.py").read_text(encoding="utf-8") == "x = 1\n"
    assert "改过了" in (d / "SKILL.md").read_text(encoding="utf-8")
    assert (d / "references" / "notes.md").exists()


@pytest.mark.asyncio
async def test_explicit_empty_scripts_clears_them(tmp_path, monkeypatch):
    """负对照：显式传空表 = 「删光所有脚本」，这条语义不能被上面那条吃掉。"""
    monkeypatch.chdir(tmp_path)
    handler, store = _handler(), _FakeStore()
    d = await _seed(tmp_path, handler, store)

    out = await handler({"body_md": _BODY.format(step="改过了"), "scripts": [], "overwrite": True}, _ctx(store))

    assert out.get("ok") is True, out
    assert out["script_count"] == 0
    assert not (d / "scripts").exists() or not any((d / "scripts").iterdir())
    assert (d / "references" / "notes.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", ["s1.py", 42, True])
async def test_malformed_scripts_arg_does_not_delete_files(tmp_path, monkeypatch, junk):
    """★ 模型写错参数不该等于「删光所有脚本」——不可逆盘操作必须 fail-safe。

    只有 list / dict 算「提供了 scripts」；其余一律按「没提」处理（保留旧脚本）。
    """
    monkeypatch.chdir(tmp_path)
    handler, store = _handler(), _FakeStore()
    d = await _seed(tmp_path, handler, store)

    out = await handler({"body_md": _BODY.format(step="改过了"), "scripts": junk, "overwrite": True}, _ctx(store))

    assert out.get("ok") is True, out
    assert out["script_count"] == 1, f"scripts={junk!r} 被当成了「删光」"
    assert (d / "scripts" / "s1.py").exists()


@pytest.mark.asyncio
async def test_bad_frontmatter_never_adopts_from_the_user_root(tmp_path, monkeypatch):
    """★ frontmatter 非法时**根本不进 adopt**，而不是拿用户根目录当 old_dir。

    `peek_skill_name` 对非法 frontmatter 返 None；若据此拼 `dest_root/<user>/""`，那就是
    **用户 skill 根目录**——实测 adopt 会把该用户所有兄弟 skill 的目录整个拷进本 bundle。
    今天最终盘态看不出差别（validate 随后必抛、临时目录被删），所以这里**钉的是调用参数
    本身**：只断言"最后没坏"会让这条改动无声退化（撤掉守卫最终盘态照样干净）。
    """
    monkeypatch.chdir(tmp_path)
    handler, store = _handler(), _FakeStore()
    await _seed(tmp_path, handler, store)  # Sibling skill: gm-recipe.

    seen: list[Path] = []
    real = skill_upload._adopt_unmanaged_files
    monkeypatch.setattr(
        skill_upload,
        "_adopt_unmanaged_files",
        lambda tmp, old, **kw: (seen.append(old), real(tmp, old, **kw))[1],
    )

    out = await handler({"body_md": "---\nname: Bad Name!\ndescription: x\n---\nbody\n", "overwrite": True}, _ctx(store))

    assert out.get("ok") is not True  # Validation must reject when required.
    user_root = (Path(".data") / "user_skills" / "alice").resolve()
    assert user_root not in [p.resolve() for p in seen], f"拿用户根目录当 old_dir 了: {seen}"
    # Sibling skill remains intact (fallback assertion on final disk state).
    assert (user_root / "gm-recipe" / "scripts" / "s1.py").exists()


@pytest.mark.asyncio
async def test_new_scripts_replace_wholesale(tmp_path, monkeypatch):
    """传了就是完整集合：整体替换，旧脚本不残留。"""
    monkeypatch.chdir(tmp_path)
    handler, store = _handler(), _FakeStore()
    d = await _seed(tmp_path, handler, store)

    out = await handler(
        {"body_md": _BODY.format(step="改过了"), "scripts": [{"name": "s2.py", "content": "y = 2\n"}], "overwrite": True},
        _ctx(store),
    )

    assert out.get("ok") is True, out
    assert sorted(p.name for p in (d / "scripts").iterdir()) == ["s2.py"]


@pytest.mark.asyncio
async def test_file_patches_preserve_other_scripts_and_references(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    handler, store = _handler(), _FakeStore()
    d = await _seed(tmp_path, handler, store)
    out = await handler(
        {
            "body_md": _BODY.format(step="文件修改"),
            "overwrite": True,
            "files": [
                {"path": "scripts/nested/run.py", "content": "x = 2\n"},
                {"path": "references/nested/rules.json", "content": '{"unit":"CNY"}'},
            ],
        },
        _ctx(store),
    )
    assert out.get("ok") is True, out
    assert (d / "scripts/s1.py").read_text() == "x = 1\n"
    assert (d / "references/notes.md").read_text() == "口径备注"
    assert (d / "scripts/nested/run.py").read_text() == "x = 2\n"
    out = await handler(
        {
            "body_md": _BODY.format(step="删除文件"),
            "overwrite": True,
            "files": [
                {"path": "scripts/nested/run.py", "content": None},
            ],
        },
        _ctx(store),
    )
    assert out.get("ok") is True, out
    assert not (d / "scripts/nested/run.py").exists()
    assert (d / "references/nested/rules.json").is_file()


@pytest.mark.asyncio
async def test_invalid_script_list_keeps_entire_bundle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    handler, store = _handler(), _FakeStore()
    d = await _seed(tmp_path, handler, store)
    before = skill_upload.bundle_sha256(d)
    out = await handler({"body_md": _BODY.format(step="改写"), "scripts": [{"name": "bad.py"}], "overwrite": True}, _ctx(store))
    assert out.get("ok") is False
    assert skill_upload.bundle_sha256(d) == before


@pytest.mark.asyncio
async def test_team_edit_requires_complete_personal_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    handler, store = _handler(), _FakeStore()
    ctx = _ctx(store)

    class Teams:
        async def get_by_name(self, team_id, name, only_enabled):
            return SimpleNamespace(name=name)

    ctx.team_skill_store = Teams()
    ctx.team_id = "team_a"
    out = await handler({"body_md": _BODY.format(step="改团队版"), "overwrite": True}, ctx)
    assert out.get("error") == "team_skill_copy_required"
    assert not (Path(".data") / "user_skills" / "alice" / "gm-recipe").exists()
