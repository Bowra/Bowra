"""skill-import 批1（tech-design r3 §3.4）：五条写路径重跑清点、四态状态机在 HTTP 面的表现、生效闸.

upload 红 → pending（enabled 不动、DTO 带 import_status/import_report）；编辑器 PUT /body 清红 → cleared；
promote 对 pending 409；import-override 只对 pending 放行 → overridden；copy-to-personal 同样过钩子。
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_upload import SkillUploadError
from bowhead_core.user_store import User, UserStore, _now
from bowhead_gateway.error_handler import _skill_upload_error_handler
from bowhead_gateway.routes import skills as skr
from bowhead_gateway.routes import team_skills as oskr
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import migrations

AUTHOR = User(id="u_author", team_id="team_a", email="author@x.com")
_SKILLS = "/api/v1/skills"
_TEAM = "/api/v1/team-skills"


def _run(coro):
    return asyncio.run(coro)


async def _seed_team(db_path: str) -> None:
    now = _now()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO teams(id, name, created_by_user_id, created_at) VALUES ('team_a','A','u_author',?)", (now,))
        await db.execute("INSERT INTO users(id, team_id, email, created_at) VALUES (?,?,?,?)", (AUTHOR.id, "team_a", AUTHOR.email, now))
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?,?,?,?,'active',?)",
            ("tm_author", AUTHOR.id, "team_a", "team_admin", now),
        )
        await db.commit()


def _app(tmp_path, monkeypatch, *, migrated: bool = False) -> FastAPI:
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oskr, "_TEAM_SKILLS_ROOT", tmp_path / "team_skills_data")
    if migrated:
        # Build DB through real migrations: `initialize()` tables lack team_skill.import_provenance_json added in 0064;
        # writes tolerate missing columns and return False, making ⑨ wiring falsely pass with a completed path but no write.
        config = Config()
        config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'u.db'}")
        config.attributes["bowhead_target"] = "core"
        command.upgrade(config, "head")
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    _run(_seed_team(str(tmp_path / "u.db")))
    sis = SkillInstanceStore(str(tmp_path / "u.db"))
    _run(sis.initialize())
    app = FastAPI()
    app.state.user_store = store
    app.state.skill_instance_store = sis
    app.state.skill_catalog = SimpleNamespace(list_names=list, list_specs=list, get=lambda name: None, refresh=lambda: None)
    # Resolution source: tool registry recognizes only these two.
    app.state.tool_registry = SimpleNamespace(list_names=lambda: ["bi_query", "python_sandbox"])
    app.include_router(oskr.router, prefix=_TEAM)
    app.include_router(skr.router, prefix=_SKILLS)
    app.add_exception_handler(SkillUploadError, _skill_upload_error_handler)  # type: ignore[arg-type]
    return app


def _client(app: FastAPI, user: User = AUTHOR) -> TestClient:
    app.dependency_overrides[oskr._current_user] = lambda: user
    app.dependency_overrides[skr._current_user] = lambda: user
    return TestClient(app)


def _skill_md(name: str, allowed_tools: str, body: str = "## steps\n1. go\n") -> str:
    return f"---\nname: {name}\ndescription: desc for {name}\nallowed-tools: {allowed_tools}\n---\n{body}"


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def _upload(c: TestClient, name: str, allowed_tools: str, **kw):
    data = _zip({"SKILL.md": _skill_md(name, allowed_tools, **kw)})
    return c.post(f"{_SKILLS}/upload?overwrite=1", files={"file": (f"{name}.zip", data, "application/zip")})


def test_upload_red_lands_pending_and_dto_exposes_report(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = _client(app)
    r = _upload(c, "foreign-skill", "Read, data_fetch_skill")
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["enabled"] is True and d["import_status"] == "pending"
    report = d["import_report"]
    assert report["grade"] == "red"
    codes = {(f["code"], f["subject"]) for f in report["findings"]}
    assert ("tool_ref_unresolved", "data_fetch_skill") in codes
    assert ("foreign_runtime_marker", "Read") in codes  # Foreign shape is yellow, not red.
    # List summary also includes status.
    listed = c.get(_SKILLS).json()
    row = next(x for x in listed["user_upload"] if x["name"] == "foreign-skill")
    assert row["import_status"] == "pending"


def test_upload_green_has_no_status(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = _client(app)
    r = _upload(c, "clean-skill", "bi_query")
    assert r.status_code == 201, r.text
    assert r.json()["import_status"] is None
    assert r.json()["import_report"]["grade"] in ("green", "yellow")


def test_editor_body_write_reruns_inventory_and_clears(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = _client(app)
    assert _upload(c, "fixme", "data_fetch_skill").json()["import_status"] == "pending"
    r = c.put(f"{_SKILLS}/fixme/body", json={"content": _skill_md("fixme", "bi_query")})
    assert r.status_code == 200, r.text
    assert c.get(f"{_SKILLS}/fixme").json()["import_status"] == "cleared"
    # Break it again → pending.
    r = c.put(f"{_SKILLS}/fixme/body", json={"content": _skill_md("fixme", "nope_tool")})
    assert r.status_code == 200, r.text
    assert c.get(f"{_SKILLS}/fixme").json()["import_status"] == "pending"


def test_promote_refuses_pending_and_override_transitions(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = _client(app)
    _upload(c, "shareme", "data_fetch_skill")
    spec_body = c.get(f"{_SKILLS}/shareme").json()["body_md"]
    r = c.post(f"{_SKILLS}/promote", json={"name": "shareme", "title": "shareme", "content": spec_body})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "skill_import_pending"
    # Cannot override non-pending.
    _upload(c, "fine", "bi_query")
    assert c.post(f"{_SKILLS}/fine/import-override").status_code == 409
    # pending → overridden; subsequent promote allowed (lifecycle prerequisite lifted).
    r = c.post(f"{_SKILLS}/shareme/import-override")
    assert r.status_code == 200 and r.json()["import_status"] == "overridden"
    assert c.get(f"{_SKILLS}/shareme").json()["import_status"] == "overridden"
    r = c.post(f"{_SKILLS}/promote", json={"name": "shareme", "title": "shareme", "content": spec_body})
    assert r.status_code == 200, r.text
    # Override pinned to fingerprint: identical reupload stays overridden; new red finding returns to pending.
    assert _upload(c, "shareme", "data_fetch_skill").json()["import_status"] == "overridden"
    assert _upload(c, "shareme", "data_fetch_skill, other_missing").json()["import_status"] == "pending"


def test_copy_to_personal_runs_the_hook(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = _client(app)
    created = c.post(_TEAM, json={"body_md": _skill_md("team-foreign", "data_fetch_skill")})
    assert created.status_code in (200, 201), created.text
    skill_id = created.json()["id"]
    r = c.post(f"{_TEAM}/{skill_id}/copy-to-personal", json={})
    assert r.status_code == 200, r.text
    inst = _run(app.state.skill_instance_store.get(user_id=AUTHOR.id, name="team-foreign"))
    assert inst is not None and inst.import_status == "pending"


def test_inventory_failure_never_fails_the_write(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = _client(app)
    monkeypatch.setattr(skr, "build_resolution_sources", _boom)
    r = _upload(c, "still-saves", "data_fetch_skill")
    assert r.status_code == 201, r.text
    assert r.json()["import_status"] is None  # Inventory failed ⇒ underreport, not block.


async def _boom(request, user):  # noqa: ARG001
    raise RuntimeError("sources unavailable")


def test_settings_store_upsert_on_toggle_keeps_status(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = _client(app)
    _upload(c, "toggled", "data_fetch_skill")
    r = c.patch(f"{_SKILLS}/update", json={"name": "toggled", "enabled": False})
    assert r.status_code == 200, r.text
    inst = _run(app.state.skill_instance_store.get(user_id=AUTHOR.id, name="toggled"))
    assert inst.enabled is False and inst.import_status == "pending"  # Preference toggles retain status.
    assert Path(inst.source_path).exists()


def test_generate_route_runs_the_hook(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = _client(app)
    r = c.post(f"{_SKILLS}/generate", json={"body_md": _skill_md("gen-red", "data_fetch_skill"), "overwrite": True})
    assert r.status_code in (200, 201), r.text
    assert r.json()["import_status"] == "pending"
    r = c.post(f"{_SKILLS}/generate", json={"body_md": _skill_md("gen-red", "bi_query"), "overwrite": True})
    assert r.status_code in (200, 201), r.text
    assert r.json()["import_status"] == "cleared"


def test_inventory_failure_preserves_existing_pending_review(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = _client(app)
    assert _upload(c, "unlock", "data_fetch_skill").json()["import_status"] == "pending"
    monkeypatch.setattr(skr, "build_resolution_sources", _boom)
    r = _upload(c, "unlock", "bi_query")
    assert r.status_code == 201, r.text
    assert r.json()["import_status"] == "pending"
    assert r.json()["import_report"] is not None  # A failed inspection does not establish that blockers were resolved.


def _team_provenance(app: FastAPI, skill_id: str) -> str | None:
    row = _run(app.state.user_store.get_team_skill("team_a", skill_id))
    assert row is not None
    return row.import_provenance_json


def test_promote_records_override_provenance_on_the_team_row(tmp_path, monkeypatch):
    """跨线 ⑨：强开过的个人技能 promote 上去时，团队候选带上「作者明知有阻断项仍启用」的痕迹。

    真迁移链建库（见 `_app(migrated=True)`）；出处**服务端现算**，请求体给不了。
    """
    app = _app(tmp_path, monkeypatch, migrated=True)
    c = _client(app)
    _upload(c, "shareme", "data_fetch_skill")
    body = c.get(f"{_SKILLS}/shareme").json()["body_md"]
    assert c.post(f"{_SKILLS}/shareme/import-override").json()["import_status"] == "overridden"
    r = c.post(f"{_SKILLS}/promote", json={"name": "shareme", "title": "shareme", "content": body})
    assert r.status_code == 200, r.text

    prov = json.loads(_team_provenance(app, r.json()["id"]))
    assert prov["status"] == "overridden"
    assert prov["red_count"] >= 1  # 0 renders '明知有 0 条仍启用' in approval UI; see skill_import_provenance.
    assert {f["code"] for f in prov["findings"]} == {"tool_ref_unresolved"}
    assert all(f["severity"] == "red" for f in prov["findings"])  # Yellow does not block, omit it.


def test_promote_without_override_leaves_no_provenance(tmp_path, monkeypatch):
    """没强开过就没什么可告诉审核者的——干净技能不该留痕（否则审批面每行都挂个空壳）。"""
    app = _app(tmp_path, monkeypatch, migrated=True)
    c = _client(app)
    _upload(c, "clean-one", "bi_query")
    body = c.get(f"{_SKILLS}/clean-one").json()["body_md"]
    r = c.post(f"{_SKILLS}/promote", json={"name": "clean-one", "title": "clean-one", "content": body})
    assert r.status_code == 200, r.text
    assert _team_provenance(app, r.json()["id"]) is None


def test_resubmission_after_clearing_blockers_drops_old_provenance(tmp_path, monkeypatch):
    """Provenance describes the latest submission; a cleared re-submission must not keep the old blocker record."""
    app = _app(tmp_path, monkeypatch, migrated=True)
    c = _client(app)
    _upload(c, "fixme", "data_fetch_skill")
    assert c.post(f"{_SKILLS}/fixme/import-override").json()["import_status"] == "overridden"
    body = c.get(f"{_SKILLS}/fixme").json()["body_md"]
    rid = c.post(f"{_SKILLS}/promote", json={"name": "fixme", "title": "fixme", "content": body}).json()["id"]
    assert _team_provenance(app, rid) is not None

    assert _upload(c, "fixme", "bi_query").json()["import_status"] == "cleared"
    body = c.get(f"{_SKILLS}/fixme").json()["body_md"]
    r = c.post(f"{_SKILLS}/promote", json={"name": "fixme", "title": "fixme", "content": body})
    assert r.status_code == 200, r.text
    assert _team_provenance(app, rid) is None


def test_readonly_missing_files_land_pending_and_clear_after_native_authoring(tmp_path, monkeypatch):
    """Real upload/store/tool/authoring flow, independently of script execution."""
    import yaml
    from bowhead_skill.loader import load_skill
    from bowhead_skill.readonly_entry import validate_read_only_entry_files
    from bowhead_skill.tools import build_skill_tools

    from bowhead_skill import SkillCatalog, SkillRuntime

    fixture = Path(__file__).resolve().parents[3] / "contracts/skill-readonly-entry/v1.json"
    for flag in ("0", "1"):
        for missing in ("references/method.md", "references/metrics.md"):
            root = tmp_path / flag / Path(missing).stem
            root.mkdir(parents=True)
            app = _app(root, monkeypatch)
            monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", flag)
            client = _client(app)
            declaration = json.loads(fixture.read_text())
            declaration["required_skill_refs"] = []
            metadata = {"name": "pending-method", "description": "Read-only method", "read_only_entry": declaration}
            body = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n# Read-only method\n"
            files = {"SKILL.md": body, "references/method.md": "Method", "references/metrics.md": "Metric"}
            del files[missing]
            response = client.post(f"{_SKILLS}/upload", files={"file": ("method.zip", _zip(files), "application/zip")})
            assert response.status_code == 201, response.text
            data = response.json()
            assert data["import_status"] == "pending" and data["enabled"] is True
            findings = data["import_report"]["findings"]
            assert [(f["code"], f["subject"]) for f in findings if f["severity"] == "red"] == [("file_ref_dangling", missing)]
            assert client.get(f"{_SKILLS}/pending-method").json()["body_raw"] == body
            ctx = SimpleNamespace(user_id=AUTHOR.id, team_id=AUTHOR.team_id, skill_instance_store=app.state.skill_instance_store)
            tools = {tool.name: tool for tool in build_skill_tools(SkillCatalog(roots=[]), SkillRuntime())}
            for name in ("skill_read", "skill_invoke"):
                denied = _run(tools[name].handler({"name": "pending-method", "script": "legacy.py"}, ctx))
                assert denied["error_kind"] == "skill_import_pending"
            assert _run(tools["skill_list"].handler({}, ctx)) == []
            response = client.post(f"{_SKILLS}/promote", json={"name": "pending-method", "title": "Method", "content": body})
            assert response.status_code == 409 and response.json()["detail"]["code"] == "skill_import_pending"
            response = client.post(
                f"{_SKILLS}/pending-method/files",
                json={"operation": "create", "kind": "references", "filename": Path(missing).name, "content": "Supplied method or metric"},
            )
            assert response.status_code == 200, response.text
            assert client.get(f"{_SKILLS}/pending-method").json()["import_status"] == "cleared"
            read = _run(tools["skill_read"].handler({"name": "pending-method"}, ctx))
            assert "error_kind" not in read
            instance = _run(app.state.skill_instance_store.get(user_id=AUTHOR.id, name="pending-method"))
            source = Path(instance.source_path)
            validate_read_only_entry_files(load_skill(source).read_only_entry, source)


@pytest.fixture(autouse=True)
def _prepare_usage_projection_databases(tmp_path, initialize_migrated_core_database, monkeypatch):
    original_app = _app

    def app_with_core_schema(directory, *args, **kwargs):
        database = directory / "u.db"
        if not database.exists():
            initialize_migrated_core_database(database)
        return original_app(directory, *args, **kwargs)

    monkeypatch.setitem(globals(), "_app", app_with_core_schema)
