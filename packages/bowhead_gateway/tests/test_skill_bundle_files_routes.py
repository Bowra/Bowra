"""Skill bundle 附属文件读取面 —— 个人 /skills 与团队 /team-skills 两侧同契约.

背景：团队副本盘上带着 references（promote 是整目录拷贝），但团队侧 detail 不带
references、也没有单文件端点 → 用户报「skill 发布到团队后关联的 skill 没了」。本文件
把两侧的读取面钉成同一份契约：列举字段名一致、kind 白名单一致、traversal/上限/非文本
的错误码一致，且团队侧的可见性门与详情同一条（跨 team 连文件名都探不到）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.user_store import TeamSkill, User, UserStore, _id, _now
from bowhead_gateway.routes import skills as skills_routes
from bowhead_gateway.routes import team_skills as oskr
from bowhead_gateway.skill_bundle_files import validate_file_ref
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────── Personal side ──────────────────


class _Catalog:
    def __init__(self, source_dir: Path) -> None:
        self._spec = SimpleNamespace(
            name="forecast",
            description="Forecast pipeline",
            version="1.0.0",
            source_dir=source_dir,
            body_md="# Forecast",
            allowed_tools=[],
            scripts=[],
            trigger_examples=[],
            title="",
            category="",
        )

    def list_specs(self):
        return [self._spec]

    def list_names(self):
        return [self._spec.name]

    def get(self, name: str):
        return self._spec if name == self._spec.name else None


def _personal_client(tmp_path, monkeypatch) -> tuple[TestClient, Path]:
    monkeypatch.chdir(tmp_path)
    source_dir = tmp_path / "builtin" / "forecast"
    (source_dir / "references").mkdir(parents=True)
    (source_dir / "references" / "metrics.md").write_text("# 口径\n毛利=收入-成本\n", encoding="utf-8")
    store = SkillInstanceStore(tmp_path / "s.db")
    _run(store.initialize())
    app = FastAPI()
    user_store = UserStore(str(tmp_path / "s.db"))
    _run(user_store.initialize())
    app.state.user_store = user_store
    app.state.skill_instance_store = store
    app.state.skill_catalog = _Catalog(source_dir)
    app.include_router(skills_routes.router, prefix="/api/v1/skills")
    app.dependency_overrides[skills_routes._current_user] = lambda: User(id="u_a", team_id="team_a", email="a@x.com")
    return TestClient(app), source_dir


def test_personal_detail_lists_references_and_reads_file(tmp_path, monkeypatch):
    client, source_dir = _personal_client(tmp_path, monkeypatch)
    (source_dir / "evals").mkdir()
    (source_dir / "evals" / "evals.json").write_text(json.dumps({"evals": [{"q": "a"}, {"q": "b"}]}), encoding="utf-8")

    d = client.get("/api/v1/skills/forecast").json()
    assert d["references"] == [{"name": "metrics.md", "size": (source_dir / "references" / "metrics.md").stat().st_size}]
    assert d["evals_count"] == 2 and d["has_evals"] is True

    r = client.get("/api/v1/skills/forecast/files/references/metrics.md")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "content_sha256": None,
        "path": "references/metrics.md",
        "content": "# 口径\n毛利=收入-成本\n",
        "size": (source_dir / "references" / "metrics.md").stat().st_size,
        "mime": "text/markdown",
    }


def test_filename_guard_rejects_traversal_shapes():
    """URL 路径里的 ../ 会先被路由规整掉（打不到 handler），所以 traversal 闸必须直测.

    正对照：摘掉 validate_file_ref 里任一条，下面对应的 pytest.raises 立刻转红。
    """
    for bad in ("../outside.md", "..", "a/../b.md", "/etc/passwd", ".hidden.md"):
        with pytest.raises(HTTPException) as ei:
            validate_file_ref("references", bad)
        assert ei.value.status_code == 400
        assert ei.value.detail["code"] == "skill_filename_invalid"
    with pytest.raises(HTTPException) as ei:
        validate_file_ref("secrets", "metrics.md")
    assert ei.value.detail["code"] == "skill_file_kind_invalid"
    validate_file_ref("references", "metrics.md")  # Valid name does not raise.
    validate_file_ref("references", "nested/metrics.md")


def test_personal_file_guards(tmp_path, monkeypatch):
    client, source_dir = _personal_client(tmp_path, monkeypatch)
    # kind allowlist.
    r = client.get("/api/v1/skills/forecast/files/secrets/metrics.md")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "skill_file_kind_invalid"
    # Leading dot (only traversal shape reaching handler through route normalization).
    (source_dir / "references" / ".secret.md").write_text("leak", encoding="utf-8")
    r = client.get("/api/v1/skills/forecast/files/references/.secret.md")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "skill_filename_invalid"
    assert "leak" not in r.text
    # Encoded ../ normalized to a path outside this route → no handler or content returned.
    (tmp_path / "outside.md").write_text("leak", encoding="utf-8")
    r = client.get("/api/v1/skills/forecast/files/references/..%2F..%2F..%2Foutside.md")
    assert r.status_code != 200 and "leak" not in r.text
    # Missing.
    r = client.get("/api/v1/skills/forecast/files/references/nope.md")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "skill_file_not_found"
    # Exceeds 1MB preview limit.
    (source_dir / "references" / "big.md").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    r = client.get("/api/v1/skills/forecast/files/references/big.md")
    assert r.status_code == 413 and r.json()["detail"]["code"] == "skill_file_too_large"
    # Nontext.
    (source_dir / "references" / "blob.md").write_bytes(b"\xff\xfe\x00binary")
    r = client.get("/api/v1/skills/forecast/files/references/blob.md")
    assert r.status_code == 415 and r.json()["detail"]["code"] == "skill_file_not_text"


# ─────────────────────────── Team side ──────────────────────


_BASE = "/api/v1/team-skills"


def _skill_md(name="channel-gm-recipe", desc="渠道毛利核对 触发：渠道毛利/口径") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n## 执行步骤\n1. 取数\n2. 算毛利\n"


def _team_client(store: UserStore, user: User, monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(oskr, "_TEAM_SKILLS_ROOT", tmp_path / "team_skills_data")
    app = FastAPI()
    app.include_router(oskr.router, prefix=_BASE)
    app.state.user_store = store
    app.dependency_overrides[oskr._current_user] = lambda: user
    return TestClient(app)


def _seed_team_skill(tmp_path, monkeypatch) -> tuple[TestClient, str, Path]:
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _team_client(store, User(id="u_a", team_id="team_a", email="a@x.com"), monkeypatch, tmp_path)
    sid = client.post(_BASE, json={"body_md": _skill_md(), "scripts": [{"name": "step_1.py", "content": "x = 1\n"}]}).json()["id"]
    sk_dir = tmp_path / "team_skills_data" / "team_a" / "channel-gm-recipe"
    # references arrive only through promote/copy_validated_bundle whole-directory copies (create does not accept them);
    # write directly to disk to simulate a copy published to the team.
    (sk_dir / "references").mkdir()
    (sk_dir / "references" / "metrics.md").write_text("# 口径\n毛利=收入-成本\n", encoding="utf-8")
    return client, sid, sk_dir


def test_team_detail_lists_bundle_files(tmp_path, monkeypatch):
    client, sid, sk_dir = _seed_team_skill(tmp_path, monkeypatch)
    d = client.get(f"{_BASE}/{sid}").json()
    assert d["references"] == [{"name": "metrics.md", "size": (sk_dir / "references" / "metrics.md").stat().st_size}]
    assert [s["name"] for s in d["scripts"]] == ["step_1.py"]
    assert d["evals_count"] == 0 and d["has_evals"] is False
    assert "source_path" not in d  # Still no disk-path exposure.


def test_team_single_file_read(tmp_path, monkeypatch):
    client, sid, _ = _seed_team_skill(tmp_path, monkeypatch)
    r = client.get(f"{_BASE}/{sid}/files/references/metrics.md")
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "# 口径\n毛利=收入-成本\n" and r.json()["mime"] == "text/markdown"
    r = client.get(f"{_BASE}/{sid}/files/scripts/step_1.py")
    assert r.status_code == 200 and r.json()["content"] == "x = 1\n"


def test_team_file_guards_and_idor(tmp_path, monkeypatch):
    client, sid, _ = _seed_team_skill(tmp_path, monkeypatch)
    r = client.get(f"{_BASE}/{sid}/files/secrets/metrics.md")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "skill_file_kind_invalid"
    r = client.get(f"{_BASE}/{sid}/files/references/.secret.md")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "skill_filename_invalid"
    r = client.get(f"{_BASE}/{sid}/files/references/nope.md")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "skill_file_not_found"


def test_team_file_read_is_team_scoped(tmp_path, monkeypatch):
    """跨 team 的行：文件端点与详情同一条可见性门 → 404，不泄露存在性。"""
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    other = TeamSkill(
        id=_id("oskill"),
        team_id="team_b",
        name="other-recipe",
        source_path=str(tmp_path / "team_skills_data" / "team_b" / "other-recipe"),
        created_by="u_b",
        created_at=_now(),
        updated_at=_now(),
    )
    _run(store.create_team_skill(other))
    ref = Path(other.source_path) / "references"
    ref.mkdir(parents=True)
    (ref / "metrics.md").write_text("team_b 机密口径", encoding="utf-8")

    client = _team_client(store, User(id="u_a", team_id="team_a", email="a@x.com"), monkeypatch, tmp_path)
    r = client.get(f"{_BASE}/{other.id}/files/references/metrics.md")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "team_skill_not_found"
    assert "机密" not in r.text


@pytest.fixture(autouse=True)
def _prepare_usage_projection_databases(tmp_path, initialize_migrated_core_database):
    for filename in ("s.db", "u.db"):
        initialize_migrated_core_database(tmp_path / filename)
