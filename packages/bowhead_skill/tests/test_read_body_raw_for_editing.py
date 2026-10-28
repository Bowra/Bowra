"""★ skill_read 要给「可编辑的用户 skill」回一份**整份 SKILL.md 原文**（`body_raw`）.

2026-09-02 在 116 实测「用对话改 skill」：`skill_read` 只回 `body`（剥掉 frontmatter、
且过了 substitute_args 的展示形态）+ `scripts` 的**文件名**。agent 只能照返回里的
name/description/version 把 frontmatter **自己拼回来**再存回去 —— 于是 frontmatter 里
`title` / `category` / `trigger_examples` / `kind` / `sandbox_requirements` 这些它看不见的
字段，改一次正文就没了。给 `body_raw` 才让「原样改」成为可能。

内置 skill 改不动（写侧 403 `skill_not_editable`），不给，省 token。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_skill.runtime import SkillRuntime
from bowhead_skill.tools import build_skill_tools

from bowhead_skill.catalog import SkillCatalog

_FULL = """---
name: gm-recipe
description: 渠道毛利核对
version: 1.0.0
title: 渠道毛利
category: analysis
trigger_examples:
  - 渠道毛利怎么算
---

## 步骤

1. 取数
"""


class _FakeUserStore:
    def __init__(self, insts):
        self._insts = list(insts)

    async def list_for_user(self, *, user_id):
        return self._insts

    async def get(self, *, user_id, name):
        return next((i for i in self._insts if i.name == name), None)


def _user_skill(tmp_path: Path, uid: str, name: str) -> SimpleNamespace:
    d = tmp_path / ".data" / "user_skills" / uid / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_FULL, encoding="utf-8")
    return SimpleNamespace(name=name, source_type="user_upload", source_path=str(d), enabled=True)


def _ctx(**kw):
    base = {"user_id": "u1", "team_id": "t1", "skill_instance_store": None, "team_skill_store": None, "granted_team_skill_ids": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _read_tool(tmp_path: Path):
    cat = SkillCatalog(roots=[tmp_path / "builtin"])
    (tmp_path / "builtin").mkdir(exist_ok=True)
    cat.refresh()
    return next(t for t in build_skill_tools(cat, SkillRuntime()) if t.name == "skill_read")


@pytest.mark.asyncio
async def test_user_skill_read_returns_the_full_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inst = _user_skill(tmp_path, "u1", "gm-recipe")
    out = await _read_tool(tmp_path).handler({"name": "gm-recipe"}, _ctx(skill_instance_store=_FakeUserStore([inst])))

    assert out["body_raw"] == _FULL  # Exact original text on disk.
    assert not out["body"].startswith("---")  # `body` remains the display form with frontmatter removed.
    # ★ Distinguishing check: these fields exist only in frontmatter and are lost when the agent reconstructs it from the returned display body.
    for lost_if_reconstructed in ("title: 渠道毛利", "category: analysis", "trigger_examples"):
        assert lost_if_reconstructed in out["body_raw"]
    assert "edit_hint" in out  # Tell the model which version to edit.


@pytest.mark.asyncio
async def test_builtin_skill_read_has_no_body_raw(tmp_path, monkeypatch):
    """负对照：内置 skill 编辑不了（写侧 403），不必多发一份全文。"""
    monkeypatch.chdir(tmp_path)
    b = tmp_path / "builtin" / "gm-recipe"
    b.mkdir(parents=True)
    (b / "SKILL.md").write_text(_FULL, encoding="utf-8")

    out = await _read_tool(tmp_path).handler({"name": "gm-recipe"}, _ctx())
    assert "body_raw" not in out
