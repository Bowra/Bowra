"""skill-team-governance §3.8 — 个人 PATCH description 死字段修（flag 外）.

此前 PATCH 把 description 写进 skill_instances 行，而 list/detail 读侧恒返盘上
frontmatter——写了永远读不到。修法=读侧 DB override 优先、盘上 frontmatter 兜底；
清空 description 显式回落 frontmatter。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import skills as skr
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


U = User(id="u1", team_id="team_a", email="u1@x.com")

_BASE = "/api/v1/skills"


class _Catalog:
    def __init__(self, specs=()):
        self._specs = {s.name: s for s in specs}

    def list_specs(self):
        return [self._specs[k] for k in sorted(self._specs)]

    def list_names(self):
        return sorted(self._specs)

    def get(self, name):
        return self._specs.get(name)


def _mk_user_skill(tmp_path, name="my-recipe", desc="盘上原描述 触发：毛利") -> Path:
    d = tmp_path / ".data" / "user_skills" / "u1" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n# body\n", encoding="utf-8")
    return d


def _client(tmp_path, monkeypatch, catalog=None) -> tuple[TestClient, SkillInstanceStore]:
    monkeypatch.chdir(tmp_path)  # _user_dir_for uses relative .data.
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "0")
    sis = SkillInstanceStore(str(tmp_path / "s.db"))
    _run(sis.initialize())
    app = FastAPI()
    app.include_router(skr.router, prefix=_BASE)
    user_store = UserStore(str(tmp_path / "s.db"))
    _run(user_store.initialize())
    app.state.user_store = user_store
    app.state.skill_instance_store = sis
    app.state.skill_catalog = catalog or _Catalog()
    app.dependency_overrides[skr._current_user] = lambda: U
    return TestClient(app), sis


def test_patch_description_reads_back_and_clears(tmp_path, monkeypatch):
    d = _mk_user_skill(tmp_path)
    client, sis = _client(tmp_path, monkeypatch)
    _run(
        sis.upsert(
            user_id="u1",
            name="my-recipe",
            source_type="user_generated",
            source_path=str(d),
            description="盘上原描述 触发：毛利",
            enabled=True,
        )
    )
    r = client.patch(f"{_BASE}/update", json={"name": "my-recipe", "description": "改进后的路由描述 触发：口径"})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "改进后的路由描述 触发：口径"
    # Both detail/list read DB override (previously always frontmatter, leaving a dead field).
    assert client.get(f"{_BASE}/my-recipe").json()["description"] == "改进后的路由描述 触发：口径"
    got = {s["name"]: s["description"] for s in client.get(_BASE).json()["user_generated"]}
    assert got["my-recipe"] == "改进后的路由描述 触发：口径"
    # Clear = explicitly fall back to frontmatter.
    client.patch(f"{_BASE}/update", json={"name": "my-recipe", "description": ""})
    assert client.get(f"{_BASE}/my-recipe").json()["description"] == "盘上原描述 触发：毛利"
    # Personal editing always receives a content baseline, independently of Team publication.
    from bowhead_core.skill_upload import bundle_sha256

    assert client.get(f"{_BASE}/my-recipe").json()["content_sha256"] == bundle_sha256(d)


def test_builtin_description_override(tmp_path, monkeypatch):
    spec_dir = tmp_path / "cat" / "forecast"
    spec_dir.mkdir(parents=True)
    spec = SimpleNamespace(
        name="forecast",
        description="builtin desc",
        version="1.0",
        source_dir=spec_dir,
        body_md="# f",
        allowed_tools=[],
        scripts=[],
        trigger_examples=[],
        title="",
        category="",
    )
    client, _sis = _client(tmp_path, monkeypatch, catalog=_Catalog([spec]))
    r = client.patch(f"{_BASE}/update", json={"name": "forecast", "description": "自定义内置描述"})
    assert r.status_code == 200, r.text
    assert client.get(f"{_BASE}/forecast").json()["description"] == "自定义内置描述"
    lst = client.get(_BASE).json()
    by_name = {s["name"]: s["description"] for g in ("builtin", "user_upload", "user_generated") for s in lst[g]}
    assert by_name["forecast"] == "自定义内置描述"


def test_disable_only_patch_does_not_freeze_frontmatter(tmp_path, monkeypatch):
    """review P3 回归：首次只 disable（不带 description）不得把当时的 frontmatter 冻成
    伪 override——内置描述随版本升级后应继续读到活 frontmatter。"""
    spec_dir = tmp_path / "cat" / "forecast"
    spec_dir.mkdir(parents=True)
    spec = SimpleNamespace(
        name="forecast",
        description="v1 desc",
        version="1.0",
        source_dir=spec_dir,
        body_md="# f",
        allowed_tools=[],
        scripts=[],
        trigger_examples=[],
        title="",
        category="",
    )
    client, _sis = _client(tmp_path, monkeypatch, catalog=_Catalog([spec]))
    assert client.patch(f"{_BASE}/update", json={"name": "forecast", "enabled": False}).status_code == 200
    spec.description = "v2 desc（升级后的 frontmatter）"
    assert client.get(f"{_BASE}/forecast").json()["description"] == "v2 desc（升级后的 frontmatter）"


def test_builtin_skill_titles_preserve_explicit_chinese_metadata(tmp_path, monkeypatch):
    spec_dir = tmp_path / "catalog" / "example"
    spec_dir.mkdir(parents=True)
    spec = SimpleNamespace(
        name="example",
        title="Example Skill",
        title_zh="示例技能",
        description="Example description",
        version="1.0",
        source_dir=spec_dir,
        body_md="# Example",
        allowed_tools=[],
        scripts=[],
        trigger_examples=[],
        category="platform",
    )
    client, _store = _client(tmp_path, monkeypatch, catalog=_Catalog([spec]))
    summary = client.get(_BASE).json()["builtin"][0]
    detail = client.get(f"{_BASE}/example").json()
    for result in (summary, detail):
        assert result["title"] == "Example Skill"
        assert result["title_zh"] == "示例技能"
    personal = skr._spec_to_summary(spec, source_type="user_upload", enabled=True)
    assert personal.title == "Example Skill"
    assert personal.title_zh == ""


def test_builtin_listing_descriptions_are_localized_until_personally_overridden(tmp_path, monkeypatch):
    spec_dir = tmp_path / "catalog" / "period-compare"
    spec_dir.mkdir(parents=True)
    spec = SimpleNamespace(
        name="period-compare",
        title="Period comparison",
        title_zh="周期对比分析",
        description="Compare metric across periods (YoY/QoQ). Use for '同比'.",
        listing_descriptions={
            "en-US": "Compares a metric across periods.",
            "zh-CN": "对比指标在不同周期的表现。",
            "de-DE": "Vergleicht eine Kennzahl über Zeiträume.",
            "fr-FR": "Compare un indicateur entre périodes.",
        },
        version="1.0",
        source_dir=spec_dir,
        body_md="# Example",
        allowed_tools=[],
        scripts=[],
        trigger_examples=[],
        category="analysis",
    )
    client, _store = _client(tmp_path, monkeypatch, catalog=_Catalog([spec]))
    summary = client.get(_BASE).json()["builtin"][0]
    detail = client.get(f"{_BASE}/period-compare").json()
    for result in (summary, detail):
        # The model-facing description is unchanged; listing text travels separately.
        assert result["description"] == spec.description
        assert result["description_en"] == "Compares a metric across periods."
        assert result["description_zh"] == "对比指标在不同周期的表现。"
        assert result["description_de"] == "Vergleicht eine Kennzahl über Zeiträume."
        assert result["description_fr"] == "Compare un indicateur entre périodes."
    personal = skr._spec_to_summary(spec, source_type="user_upload", enabled=True)
    assert (personal.description_en, personal.description_zh) == ("", "")

    # A personal description override is shown as written in every language.
    assert client.patch(f"{_BASE}/update", json={"name": "period-compare", "description": "我的周期对比"}).status_code == 200
    for result in (client.get(_BASE).json()["builtin"][0], client.get(f"{_BASE}/period-compare").json()):
        assert result["description"] == "我的周期对比"
        assert (result["description_en"], result["description_zh"], result["description_de"], result["description_fr"]) == ("", "", "", "")


@pytest.fixture(autouse=True)
def _prepare_usage_projection_databases(tmp_path, initialize_migrated_core_database):
    for filename in ("s.db",):
        initialize_migrated_core_database(tmp_path / filename)
