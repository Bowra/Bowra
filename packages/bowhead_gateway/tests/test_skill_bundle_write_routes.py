"""配置页直接编辑 skill —— 个人 /skills 与团队 /team-skills 的写入面.

两条不变量必须钉死：
① **编辑正文不许吃掉 references/**：不走 validate_and_save_generated（其 overwrite 语义
   是 rmtree 整个目录再 move），只覆写一个文件；
② **校验不过就回滚**：写坏的 bundle 一个字节都不许留在盘上。
团队侧还多一层治理门——已提交/已批的 team_wide 内容只有 admin 能改（PRD §3.1）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_upload import SkillUploadError, bundle_sha256
from bowhead_core.user_store import User, UserStore, _now
from bowhead_gateway.error_handler import _skill_upload_error_handler
from bowhead_gateway.routes import skills as skr
from bowhead_gateway.routes import team_skills as oskr
from fastapi import FastAPI
from fastapi.testclient import TestClient

OWNER = User(id="u_owner", team_id="team_a", email="owner@x.com")
ADMIN = User(id="u_admin", team_id="team_a", email="admin@x.com")
AUTHOR = User(id="u_author", team_id="team_a", email="author@x.com")

_BASE = "/api/v1/team-skills"
_SKILLS = "/api/v1/skills"


def _run(coro):
    return asyncio.run(coro)


def _skill_md(name="channel-gm-recipe", desc="渠道毛利核对 触发：渠道毛利/口径", body="## 执行步骤\n1. 取数\n") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}"


async def _seed_team(db_path: str) -> None:
    now = _now()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, created_by_user_id, created_at) VALUES ('team_a','A','u_owner',?)",
            (now,),
        )
        for u in (OWNER, ADMIN, AUTHOR):
            await db.execute("INSERT INTO users(id, team_id, email, created_at) VALUES (?,?,?,?)", (u.id, "team_a", u.email, now))
        for uid, role in (("u_admin", "team_admin"), ("u_author", "member")):
            await db.execute(
                "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?,?,?,?,'active',?)",
                (f"tm_{uid}", uid, "team_a", role, now),
            )
        await db.commit()


def _app(tmp_path, monkeypatch) -> FastAPI:
    monkeypatch.chdir(tmp_path)  # Personal bundle validation uses CWD-relative .data.
    monkeypatch.setattr(oskr, "_TEAM_SKILLS_ROOT", tmp_path / "team_skills_data")
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    _run(_seed_team(str(tmp_path / "u.db")))
    sis = SkillInstanceStore(str(tmp_path / "u.db"))
    _run(sis.initialize())
    app = FastAPI()
    app.state.user_store = store
    app.state.skill_instance_store = sis
    app.state.skill_catalog = SimpleNamespace(
        list_names=list,
        list_specs=list,
        get=lambda name: None,
        refresh=lambda: None,
    )
    app.include_router(oskr.router, prefix=_BASE)
    app.include_router(skr.router, prefix=_SKILLS)
    # Mount only the real SkillUploadError handler (personal writes, like upload/generate, send bundle
    # validation errors here → 400 + coded body). Do not mount all register_error_handlers:
    # its Exception fallback converts real bugs into 500 responses, hiding tracebacks from tests.
    app.add_exception_handler(SkillUploadError, _skill_upload_error_handler)  # type: ignore[arg-type]
    return app


def _client(app: FastAPI, user: User) -> TestClient:
    app.dependency_overrides[oskr._current_user] = lambda: user
    return TestClient(app)


# ─────────────────────────── Personal side ──────────────────


def _personal_skill(tmp_path, app, uid="u_author", name="channel-gm-recipe") -> Path:
    d = tmp_path / ".data" / "user_skills" / uid / name
    (d / "references").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_md(name=name), encoding="utf-8")
    (d / "references" / "notes.md").write_text("口径备注 v1", encoding="utf-8")
    _run(
        app.state.skill_instance_store.upsert(
            user_id=uid,
            name=name,
            source_type="user_generated",
            source_path=str(d),
            description="",  # No override → reads fall back to disk frontmatter.
            enabled=True,
            size_bytes=1,
        )
    )
    return d


def test_personal_file_write_roundtrip(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    d = _personal_skill(tmp_path, app)
    c = _client(app, AUTHOR)

    r = c.put(f"{_SKILLS}/channel-gm-recipe/files/references/notes.md", json={"content": "口径备注 v2"})
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "口径备注 v2"
    assert (d / "references" / "notes.md").read_text(encoding="utf-8") == "口径备注 v2"
    # Reads immediately see new content (same read surface).
    assert c.get(f"{_SKILLS}/channel-gm-recipe/files/references/notes.md").json()["content"] == "口径备注 v2"
    # Refresh size_bytes too (not creation-time placeholder 1).
    inst = _run(app.state.skill_instance_store.get(user_id="u_author", name="channel-gm-recipe"))
    assert inst.size_bytes > 1


def test_personal_write_reports_conflict_without_changing_the_locked_package(tmp_path, monkeypatch):
    from bowhead_core.skill_bundle_lock import skill_bundle_lock

    app = _app(tmp_path, monkeypatch)
    directory = _personal_skill(tmp_path, app)
    original = bundle_sha256(directory)
    with skill_bundle_lock(directory):
        response = _client(app, AUTHOR).put(f"{_SKILLS}/channel-gm-recipe/files/references/notes.md", json={"content": "Concurrent edit"})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "skill_write_conflict"
    assert bundle_sha256(directory) == original


def test_detail_body_raw_is_the_editable_manifest(tmp_path, monkeypatch):
    """★ 展示面 body_md（剥 frontmatter）与编辑面 body_raw（整份 SKILL.md）必须分开.

    116 真机复现过：FE 拿 body_md 当草稿 → 存回去是一份没有 frontmatter 的清单 →
    400 E_SKILL_NAME_INVALID，用户看到「skill 名称无效」却根本没碰过名字。
    """
    app = _app(tmp_path, monkeypatch)
    _personal_skill(tmp_path, app)
    sid, _ = _team_skill(app, ADMIN, name="team-gm-recipe")

    personal = _client(app, AUTHOR).get(f"{_SKILLS}/channel-gm-recipe").json()
    assert personal["body_raw"].startswith("---\nname: channel-gm-recipe")
    assert not personal["body_md"].startswith("---")

    team = _client(app, ADMIN).get(f"{_BASE}/{sid}").json()
    assert team["body_raw"].startswith("---\nname: team-gm-recipe")
    assert not team["body_md"].startswith("---")  # Description tab must not render name:/description: as body.

    # Both sides must allow opening unchanged and saving back (minimum requirement for editing).
    assert _client(app, AUTHOR).put(f"{_SKILLS}/channel-gm-recipe/body", json={"content": personal["body_raw"]}).status_code == 200
    assert _client(app, ADMIN).put(f"{_BASE}/{sid}/body", json={"content": team["body_raw"]}).status_code == 200
    # Saving display form must be rejected (positive control: proves this gate is live).
    assert _client(app, AUTHOR).put(f"{_SKILLS}/channel-gm-recipe/body", json={"content": personal["body_md"]}).status_code == 400


def test_personal_body_write_keeps_references(tmp_path, monkeypatch):
    """★ 编辑正文不许吃掉 references —— generate 的 overwrite 语义会 rmtree 整个 bundle。"""
    app = _app(tmp_path, monkeypatch)
    d = _personal_skill(tmp_path, app)
    c = _client(app, AUTHOR)

    r = c.put(f"{_SKILLS}/channel-gm-recipe/body", json={"content": _skill_md(body="## 执行步骤\n1. 改过的取数\n")})
    assert r.status_code == 200, r.text
    assert "改过的取数" in (d / "SKILL.md").read_text(encoding="utf-8")
    assert (d / "references" / "notes.md").read_text(encoding="utf-8") == "口径备注 v1"
    assert c.get(f"{_SKILLS}/channel-gm-recipe").json()["references"] == [
        {"name": "notes.md", "size": (d / "references" / "notes.md").stat().st_size}
    ]


def test_uploaded_manifest_description_updates_ui_and_agent(tmp_path, monkeypatch):
    from bowhead_skill.effective import list_effective_skills

    app = _app(tmp_path, monkeypatch)
    c = _client(app, AUTHOR)
    uploaded = c.post(f"{_SKILLS}/upload", files={"file": ("SKILL.md", _skill_md(desc="Original"), "text/markdown")})
    assert uploaded.status_code == 201, uploaded.text
    saved = c.put(f"{_SKILLS}/channel-gm-recipe/body", json={"content": _skill_md(desc="New routing description")})
    assert saved.status_code == 200, saved.text
    assert c.get(f"{_SKILLS}/channel-gm-recipe").json()["description"] == "New routing description"
    ctx = SimpleNamespace(user_id=AUTHOR.id, skill_instance_store=app.state.skill_instance_store)
    assert _run(list_effective_skills(ctx, app.state.skill_catalog))[0].description == "New routing description"


def test_personal_body_write_rejects_rename_and_rolls_back(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    d = _personal_skill(tmp_path, app)
    before = (d / "SKILL.md").read_text(encoding="utf-8")
    c = _client(app, AUTHOR)

    r = c.put(f"{_SKILLS}/channel-gm-recipe/body", json={"content": _skill_md(name="other-recipe")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "skill_body_name_immutable"
    assert (d / "SKILL.md").read_text(encoding="utf-8") == before  # Rollback.


def test_personal_body_write_rejects_invalid_bundle_and_rolls_back(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    d = _personal_skill(tmp_path, app)
    before = (d / "SKILL.md").read_text(encoding="utf-8")
    c = _client(app, AUTHOR)

    # Frontmatter missing description → rejected by upload's same gate (same
    # SkillUploadError handler as upload/generate → 400 + coded body).
    r = c.put(f"{_SKILLS}/channel-gm-recipe/body", json={"content": "---\nname: channel-gm-recipe\n---\n正文\n"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "E_SKILL_MISSING_DESCRIPTION"
    assert (d / "SKILL.md").read_text(encoding="utf-8") == before  # Rollback.


def test_personal_write_rejects_builtin(tmp_path, monkeypatch):
    """内置 skill 落在 git catalog 目录里，编辑会被下次升级覆盖 —— 一律 403。"""
    app = _app(tmp_path, monkeypatch)
    builtin_dir = tmp_path / "builtin" / "forecast"
    (builtin_dir / "references").mkdir(parents=True)
    (builtin_dir / "SKILL.md").write_text(_skill_md(name="forecast", desc="内置"), encoding="utf-8")
    (builtin_dir / "references" / "notes.md").write_text("内置备注", encoding="utf-8")
    spec = SimpleNamespace(
        name="forecast",
        description="内置",
        version=None,
        source_dir=builtin_dir,
        body_md="# f",
        allowed_tools=[],
        scripts=[],
        trigger_examples=[],
        title="",
        category="",
    )
    app.state.skill_catalog = SimpleNamespace(
        list_names=lambda: ["forecast"],
        list_specs=lambda: [spec],
        get=lambda name: spec if name == "forecast" else None,
        refresh=lambda: None,
    )
    c = _client(app, AUTHOR)

    r = c.put(f"{_SKILLS}/forecast/files/references/notes.md", json={"content": "被改了"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "skill_not_editable"
    assert (builtin_dir / "references" / "notes.md").read_text(encoding="utf-8") == "内置备注"
    assert c.put(f"{_SKILLS}/forecast/body", json={"content": _skill_md(name="forecast", desc="改")}).status_code == 403


# ─────────────────────────── Team side ──────────────────────


def _team_skill(app, user: User, *, visibility="team_wide", name="channel-gm-recipe") -> tuple[str, Path]:
    d = (
        _client(app, user)
        .post(
            _BASE,
            json={"body_md": _skill_md(name=name), "visibility": visibility, "scripts": [{"name": "step_1.py", "content": "x = 1\n"}]},
        )
        .json()
    )
    row = _run(app.state.user_store.get_team_skill("team_a", d["id"]))
    sk_dir = Path(row.source_path)
    (sk_dir / "references").mkdir(exist_ok=True)
    (sk_dir / "references" / "notes.md").write_text("团队口径 v1", encoding="utf-8")
    return d["id"], sk_dir


def test_team_creator_only_author_can_edit(tmp_path, monkeypatch):
    """creator_only 不进审批 —— 创建者自己随便改。"""
    app = _app(tmp_path, monkeypatch)
    sid, sk_dir = _team_skill(app, AUTHOR, visibility="creator_only")
    c = _client(app, AUTHOR)

    r = c.put(f"{_BASE}/{sid}/files/references/notes.md", json={"content": "团队口径 v2"})
    assert r.status_code == 200, r.text
    assert (sk_dir / "references" / "notes.md").read_text(encoding="utf-8") == "团队口径 v2"

    r = c.put(f"{_BASE}/{sid}/body", json={"content": _skill_md(body="## 执行步骤\n1. 团队改过\n")})
    assert r.status_code == 200, r.text
    assert "团队改过" in (sk_dir / "SKILL.md").read_text(encoding="utf-8")
    assert (sk_dir / "references" / "notes.md").read_text(encoding="utf-8") == "团队口径 v2"  # references retained.


def test_team_wide_author_cannot_edit_content(tmp_path, monkeypatch):
    """★ 治理门：作者不能单方面改已提交/已批的广播内容（否则全团队立刻跑没过审的版本）。"""
    app = _app(tmp_path, monkeypatch)
    sid, sk_dir = _team_skill(app, AUTHOR)  # Non-admin creates team_wide → pending.
    assert _run(app.state.user_store.get_team_skill("team_a", sid)).approval == "pending"
    c = _client(app, AUTHOR)

    r = c.put(f"{_BASE}/{sid}/files/references/notes.md", json={"content": "偷改"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "team_skill_content_admin_required"
    assert c.put(f"{_BASE}/{sid}/body", json={"content": _skill_md(body="偷改\n")}).status_code == 403
    assert (sk_dir / "references" / "notes.md").read_text(encoding="utf-8") == "团队口径 v1"


def test_team_admin_edit_refreshes_approved_fingerprint(tmp_path, monkeypatch):
    """admin 建的 team_wide 是直批态 —— admin 改完，已批指纹必须跟着盘走，否则指纹说谎。"""
    app = _app(tmp_path, monkeypatch)
    sid, sk_dir = _team_skill(app, ADMIN)
    row = _run(app.state.user_store.get_team_skill("team_a", sid))
    assert row.approval == "approved"  # Admin-created team_wide = directly approved.

    r = _client(app, ADMIN).put(f"{_BASE}/{sid}/files/references/notes.md", json={"content": "团队口径 v2"})
    assert r.status_code == 200, r.text
    after = _run(app.state.user_store.get_team_skill("team_a", sid))
    assert after.approved_sha256 == bundle_sha256(sk_dir) != row.approved_sha256
    assert after.size_bytes > 0


def test_team_content_edit_refreshes_risk_and_invalidates_verdict(tmp_path, monkeypatch):
    from bowhead_core.skill_upload import _validate_skill_dir

    app = _app(tmp_path, monkeypatch)
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "1")
    sid, sk_dir = _team_skill(app, ADMIN)
    _run(app.state.user_store.set_team_skill_verdict("team_a", sid, verdict="pass", verdict_json='{"ok":true}'))
    saved = _client(app, ADMIN).put(f"{_BASE}/{sid}/body", json={"content": _skill_md(desc="Changed", body="Run subprocess and curl\n")})
    assert saved.status_code == 200, saved.text
    after = _run(app.state.user_store.get_team_skill("team_a", sid))
    import json

    assert after.description == "Changed"
    assert json.loads(after.risk_report_json) == _validate_skill_dir(sk_dir).risk_report
    assert after.verdict is None and after.verdict_json is None


def test_team_write_is_team_scoped(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    sid, sk_dir = _team_skill(app, ADMIN)
    outsider = User(id="u_x", team_id="team_b", email="x@y.com")

    r = _client(app, outsider).put(f"{_BASE}/{sid}/files/references/notes.md", json={"content": "越权"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "team_skill_not_found"
    assert (sk_dir / "references" / "notes.md").read_text(encoding="utf-8") == "团队口径 v1"


def test_team_body_write_rejects_rename(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    sid, sk_dir = _team_skill(app, ADMIN)
    before = (sk_dir / "SKILL.md").read_text(encoding="utf-8")

    r = _client(app, ADMIN).put(f"{_BASE}/{sid}/body", json={"content": _skill_md(name="other-recipe")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "skill_body_name_immutable"
    assert (sk_dir / "SKILL.md").read_text(encoding="utf-8") == before


def test_personal_nested_file_lifecycle_and_validation(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    d = _personal_skill(tmp_path, app)
    c = _client(app, AUTHOR)
    base = f"{_SKILLS}/channel-gm-recipe/files"
    assert c.get(f"{_SKILLS}/channel-gm-recipe").json()["authoring_enabled"] is True
    create = {"operation": "create", "kind": "scripts", "filename": "nested/run.py", "content": "print('ok')\n"}
    r = c.post(base, json=create)
    assert r.status_code == 200, r.text
    assert c.get(f"{base}/scripts/nested/run.py").json()["content"] == "print('ok')\n"
    assert c.get(f"{_SKILLS}/channel-gm-recipe").json()["scripts"][0]["name"] == "nested/run.py"
    before = bundle_sha256(d)
    assert c.post(base, json=create).status_code == 409
    assert c.post(base, json={**create, "filename": "bad.py", "content": "def broken("}).status_code == 400
    assert c.put(f"{base}/scripts/nested/run.py", json={"content": "def broken("}).status_code == 400
    assert c.post(base, json={**create, "filename": "binary.py", "content": "\x00"}).status_code == 415
    assert bundle_sha256(d) == before
    renamed = c.post(base, json={"operation": "rename", "kind": "scripts", "filename": "nested/run.py", "target": "deep/main.py"})
    assert renamed.status_code == 200, renamed.text
    assert not (d / "scripts/nested/run.py").exists()
    assert c.get(f"{base}/scripts/deep/main.py").status_code == 200
    assert c.post(base, json={"operation": "delete", "kind": "scripts", "filename": "deep/main.py"}).status_code == 200
    assert (d / "references/notes.md").read_text() == "口径备注 v1"


def test_file_authoring_flag_and_team_governance(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    sid, directory = _team_skill(app, AUTHOR, name="author-recipe")
    base = f"{_BASE}/{sid}/files"
    change = {"operation": "create", "kind": "references", "filename": "nested/rules.json", "content": '{"unit": "CNY"}'}
    # Pending team-wide bundles still require an administrator to edit content.
    assert _client(app, AUTHOR).post(base, json=change).status_code == 403
    result = _client(app, ADMIN).post(base, json=change)
    assert result.status_code == 200, result.text
    assert _client(app, ADMIN).get(f"{base}/references/nested/rules.json").json()["content"] == change["content"]
    assert directory.joinpath("references/nested/rules.json").is_file()


def test_file_creation_enforces_personal_storage_before_publishing(tmp_path, monkeypatch):
    from bowhead_core import skill_upload

    app = _app(tmp_path, monkeypatch)
    directory = _personal_skill(tmp_path, app)
    monkeypatch.setattr(skill_upload, "MAX_USER_STORAGE", 5)
    before = bundle_sha256(directory)
    result = _client(app, AUTHOR).post(
        f"{_SKILLS}/channel-gm-recipe/files",
        json={
            "operation": "create",
            "kind": "references",
            "filename": "large.txt",
            "content": "too much",
        },
    )
    assert result.status_code == 400, result.text
    assert result.json()["error"]["code"] == "E_SKILL_STORAGE_LIMIT_REACHED"
    assert bundle_sha256(directory) == before


def test_script_supporting_data_is_visible_but_not_executable(tmp_path, monkeypatch):
    from bowhead_skill.tools import build_skill_tools

    from bowhead_skill import SkillCatalog, SkillRuntime

    app = _app(tmp_path, monkeypatch)
    directory = _personal_skill(tmp_path, app)
    (directory / "scripts/schema").mkdir(parents=True)
    (directory / "scripts/schema/template.json").write_text('{"type":"object"}')
    detail = _client(app, AUTHOR).get(f"{_SKILLS}/channel-gm-recipe").json()
    assert detail["scripts"][0]["name"] == "schema/template.json"
    assert detail["script_execution"] == "none"
    catalog = SkillCatalog(roots=[])
    tool = next(t for t in build_skill_tools(catalog, SkillRuntime()) if t.name == "skill_read")
    result = _run(
        tool.handler({"name": "channel-gm-recipe"}, SimpleNamespace(user_id=AUTHOR.id, skill_instance_store=app.state.skill_instance_store))
    )
    assert result["scripts"] == []
    assert result["files"]["scripts"] == ["schema/template.json"]


def test_readonly_entry_body_and_file_updates_keep_valid_packages_on_both_routes(tmp_path, monkeypatch):
    """The opt-in contract reaches existing authoring routes and their rollback."""
    import yaml
    from bowhead_skill.loader import load_skill

    app = _app(tmp_path, monkeypatch)
    personal = _personal_skill(tmp_path, app)
    team_id, team = _team_skill(app, ADMIN, name="team-readonly-method")
    for user, url, root, name, invalid_status in (
        (AUTHOR, f"{_SKILLS}/channel-gm-recipe", personal, "channel-gm-recipe", 400),
        (ADMIN, f"{_BASE}/{team_id}", team, "team-readonly-method", 422),
    ):
        client = _client(app, user)
        declaration = {
            "schema_version": 1,
            "entry_file": "references/notes.md",
            "required_files": [],
            "parameter_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "required_skill_refs": [],
            "requires_script": False,
            "requires_new_tools": False,
            "requires_new_connections": False,
        }
        metadata = {"name": name, "description": "A read-only method", "read_only_entry": declaration}
        body = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n# Method\n"
        response = client.put(f"{url}/body", json={"content": body})
        assert response.status_code == 200, response.text
        assert load_skill(root).read_only_entry.to_dict() == declaration
        assert client.get(url).json()["body_raw"] == body
        before = bundle_sha256(root)
        declaration["requires_new_connections"] = True
        invalid = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n# Method\n"
        rejected = client.put(f"{url}/body", json={"content": invalid})
        assert rejected.status_code == invalid_status, rejected.text
        assert "E_SKILL_READ_ONLY_ENTRY_INVALID" in rejected.text
        assert bundle_sha256(root) == before
        rejected = client.put(f"{url}/files/references/notes.md", json={"content": " "})
        assert rejected.status_code == invalid_status, rejected.text
        assert bundle_sha256(root) == before


@pytest.fixture(autouse=True)
def _prepare_usage_projection_databases(tmp_path, initialize_migrated_core_database):
    for filename in ("u.db",):
        initialize_migrated_core_database(tmp_path / filename)
