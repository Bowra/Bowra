"""skill-import 批1b：聊天拖 zip 的两个 KB 门——清点、能力形状组件不进 KB（拍板④）、skill 组件落成待处理导入、
清点结果按根包 artifact 落库；清点失败 fail-open（KB 入库与今天完全一样）."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.artifact_store import LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.skill_import.bundle_inventory_store import get_bundle_inventory
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.user_store import User
from bowhead_gateway.routes.kb import _process_zip_bundle
from fastapi import BackgroundTasks

from bowhead_core import ArtifactStore

_SKILL_MD = """---
name: acme-qbr
description: build QBR decks from JSON
allowed-tools: Read, data_fetch_skill
---
Read [brief](05_full_report/brief.md). Use `apply_patch`.
"""


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class _Worker:
    def __init__(self) -> None:
        self.many: list[list[str]] = []

    async def parse_and_index_many(self, ids: list[str]) -> None:
        self.many.append(list(ids))


@pytest.fixture(autouse=True)
def _migrated_kb_files(tmp_path: Path, initialize_migrated_kb_files_database) -> None:
    """_world 的 KbFilesStore 建在 tmp_path/kb_files.db；先铺 Alembic head 结构。"""
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")


async def _world(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # Skill disk paths use CWD-relative .data/user_skills.
    db = tmp_path / "bowhead.db"
    with sqlite3.connect(db) as c:
        c.executescript(initial_schema("core"))
        c.commit()
    upgrade(f"sqlite+aiosqlite:///{db}", target="core")
    artifact_store = ArtifactStore(db_path=str(db), backend=LocalBackend(root_dir=tmp_path / "blobs"))
    await artifact_store.initialize()
    files_store = KbFilesStore(tmp_path / "kb_files.db")
    await files_store.initialize()
    sis = SkillInstanceStore(str(db))
    await sis.initialize()
    state = SimpleNamespace(
        artifact_store=artifact_store,
        kb_files_store=files_store,
        kb_worker=_Worker(),
        skill_instance_store=sis,
        skill_catalog=SimpleNamespace(list_names=list, list_specs=list, get=lambda n: None, refresh=lambda: None),
        tool_registry=SimpleNamespace(list_names=lambda: ["bi_query", "python_sandbox"]),
    )
    # Real route uses fastapi.Request; persistence gate reads query_params/headers (personal skill scope checks).
    request = SimpleNamespace(app=SimpleNamespace(state=state), query_params={}, headers={})
    user = User(id="u_alice", email="alice@test.com", team_id="team_test", tier="ultra", email_verified=True)
    ctx = ScopeContext(user_id="u_alice", workspace_id="ws_test", team_id="team_test", role="user")
    return request, user, ctx, artifact_store, sis


async def _ingest(request, user, ctx, raw: bytes, *, scope: str = "personal", scope_id: str = "u_alice"):
    return await _process_zip_bundle(
        raw, "proj.zip", request=request, background_tasks=BackgroundTasks(), user=user, ctx=ctx, scope=scope, scope_id=scope_id, use_llm=False
    )


def _dup_zip(first: bytes, second: bytes) -> bytes:
    """同一成员名写两次（zip 允许；extractall 留最后一份）."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("sk/SKILL.md", first)
        zf.writestr("sk/SKILL.md", second)
    return buf.getvalue()


def _grant_only(monkeypatch, allowed: set[str]) -> None:
    """走真 `_require_skill_grant_for_request` 分支：access ctx 存在，grant 只放行 allowed 里的名字."""
    from types import SimpleNamespace as NS

    from bowhead_gateway.routes import skills as skr

    async def _ctx(request, user):
        return NS(access_control_store=None, access_context={})

    async def _visible(ctx, name, **kw):
        return name in allowed

    monkeypatch.setattr(skr, "_skill_access_ctx", _ctx)
    monkeypatch.setattr(skr, "skill_visible_by_grants", _visible)


@pytest.mark.asyncio
async def test_capability_shaped_bundle_lands_skill_and_keeps_components_out_of_kb(tmp_path: Path, monkeypatch) -> None:
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)
    raw = _zip(
        {
            "proj/acme-qbr/SKILL.md": _SKILL_MD.encode(),
            "proj/acme-qbr/references/contract.md": b"# contract",
            "proj/agents/analyst.md": b"---\nname: analyst\ndescription: a\n---\nprompt",
            "proj/docs/brief.pdf": b"%PDF-1.4\nbrief",
            "proj/data/sales.pdf": b"%PDF-1.4\nrows",
        }
    )
    resp = await _ingest(request, user, ctx, raw)
    # Skip capability-shaped components; documents still enter KB.
    skipped = {s.filename: s.reason_code for s in resp.skipped}
    assert skipped["proj/acme-qbr/SKILL.md"] == "kb_bundle_entry_capability_component"
    assert skipped["proj/acme-qbr/references/contract.md"] == "kb_bundle_entry_capability_component"
    assert skipped["proj/agents/analyst.md"] == "kb_bundle_entry_capability_component"
    assert {f.filename for f in resp.uploaded} == {"brief.pdf", "sales.pdf"}
    # Inventory in response and persisted on root bundle artifact.
    assert resp.inventory and resp.inventory["grade"] == "red"
    stored = await get_bundle_inventory(artifact_store.db_path, bundle_artifact_id=resp.bundle_artifact_id, created_by="u_alice")
    assert stored and stored["grade"] == "red" and any(c["kind"] == "skill_bundle" for c in stored["components"])
    assert await get_bundle_inventory(artifact_store.db_path, bundle_artifact_id=resp.bundle_artifact_id, created_by="u_bob") is None
    # Skill component becomes a personal pending import.
    assert resp.landed_skills == [
        {
            "name": "acme-qbr",
            "declared_name": "acme-qbr",  # Inventoried name: rendering matches it when landing renames.
            "path": "acme-qbr/SKILL.md",
            "status": "landed",
            "import_status": "pending",
            "red_count": 2,  # This skill's own red count, not whole bundle.
        }
    ]
    inst = await sis.get(user_id="u_alice", name="acme-qbr")
    assert inst is not None and inst.import_pending and inst.source_type == "user_upload"
    assert (Path(inst.source_path) / "references" / "contract.md").exists()
    # Drag again: do not overwrite same name.
    resp2 = await _ingest(request, user, ctx, raw)
    assert resp2.landed_skills[0]["status"] == "exists"


@pytest.mark.asyncio
async def test_plain_document_bundle_is_untouched(tmp_path: Path, monkeypatch) -> None:
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)
    resp = await _ingest(request, user, ctx, _zip({"a.pdf": b"%PDF-1.4\nx", "b.pdf": b"%PDF-1.4\ny"}))
    assert len(resp.uploaded) == 2 and not resp.skipped and resp.landed_skills == []
    assert resp.inventory and not any(c["kind"] in ("skill_bundle", "agent_md") for c in resp.inventory["components"])
    assert await sis.list_for_user(user_id="u_alice") == []


@pytest.mark.asyncio
async def test_inventory_failure_fails_open(tmp_path: Path, monkeypatch) -> None:
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)

    async def _boom(*a, **k):
        raise RuntimeError("sources down")

    monkeypatch.setattr("bowhead_gateway.skill_import_sources.build_resolution_sources", _boom)
    raw = _zip({"acme-qbr/SKILL.md": _SKILL_MD.encode(), "docs/brief.pdf": b"%PDF-1.4\nbrief"})
    resp = await _ingest(request, user, ctx, raw)
    assert resp.inventory is None and resp.landed_skills == []
    assert not any(s.reason_code == "kb_bundle_entry_capability_component" for s in resp.skipped)
    assert {f.filename for f in resp.uploaded} == {"SKILL.md", "brief.pdf"}  # As today: md enters KB.


@pytest.mark.asyncio
async def test_inventory_row_survives_when_table_missing_only_as_warning(tmp_path: Path, monkeypatch) -> None:
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)
    async with aiosqlite.connect(artifact_store.db_path) as db:
        await db.execute("DROP TABLE chat_attachment_bundle_inventory")
        await db.commit()
    resp = await _ingest(request, user, ctx, _zip({"acme-qbr/SKILL.md": _SKILL_MD.encode()}))
    assert resp.inventory is not None and resp.landed_skills[0]["status"] == "landed"  # Persistence failure loses only that row.


@pytest.mark.asyncio
async def test_landing_denied_when_write_capability_refused(tmp_path: Path, monkeypatch) -> None:
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)
    from bowhead_gateway.routes import skills as skr
    from fastapi import HTTPException

    async def _deny(*a, **k):
        raise HTTPException(403, "no")

    monkeypatch.setattr(skr, "_require_skill_capability_for_request", _deny)
    resp = await _ingest(request, user, ctx, _zip({"acme-qbr/SKILL.md": _SKILL_MD.encode()}))
    assert resp.landed_skills[0]["status"] == "denied"
    assert await sis.get(user_id="u_alice", name="acme-qbr") is None


@pytest.mark.asyncio
async def test_landing_persists_risk_report_like_settings_upload(tmp_path: Path, monkeypatch) -> None:
    """review P1：落库漏了 risk_report_json ⇒ 依赖编译器把每个 invoke 授权判成 risk=unknown/available=False."""
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "1")
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)
    raw = _zip({"acme-qbr/SKILL.md": _SKILL_MD.encode(), "acme-qbr/scripts/run.py": b"print(1)\n"})
    resp = await _ingest(request, user, ctx, raw)
    assert resp.landed_skills[0]["status"] == "landed"
    inst = await sis.get(user_id="u_alice", name="acme-qbr")
    assert inst is not None and inst.risk_report_json
    import json

    assert json.loads(inst.risk_report_json)["langs"] == {".py": 1}


@pytest.mark.asyncio
async def test_duplicate_skill_md_members_cannot_bypass_per_name_grant(tmp_path: Path, monkeypatch) -> None:
    """review P1：清点按第一份 SKILL.md 的 name 过闸，extractall 留最后一份 ⇒ 真落地的是没过闸的名字。
    现在同名成员直接判 invalid，不解压；对照：单份被拒名 ⇒ denied，同样不落盘."""
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)
    _grant_only(monkeypatch, {"allowed"})
    allowed = b"---\nname: allowed\ndescription: d\n---\nbody"
    forbidden = b"---\nname: forbidden\ndescription: d\n---\nbody"
    resp = await _ingest(request, user, ctx, _dup_zip(allowed, forbidden))
    # Inventory sees both as skill components: allowed passes gate but duplicate subtree member⇒invalid; forbidden denied outright. Neither lands.
    assert [(x["status"], x.get("code")) for x in resp.landed_skills] == [("invalid", "E_SKILL_DUPLICATE_MEMBER"), ("denied", None)]
    assert await sis.list_for_user(user_id="u_alice") == []
    assert not (tmp_path / ".data" / "user_skills" / "u_alice" / "forbidden").exists()
    # Positive control: grant really checked; single forbidden denied, single allowed lands.
    resp2 = await _ingest(request, user, ctx, _zip({"sk/SKILL.md": forbidden}))
    assert resp2.landed_skills[0]["status"] == "denied"
    resp3 = await _ingest(request, user, ctx, _zip({"sk/SKILL.md": allowed}))
    assert resp3.landed_skills[0]["status"] == "landed"


@pytest.mark.asyncio
async def test_team_scope_bundle_skips_components_but_lands_nothing(tmp_path: Path, monkeypatch) -> None:
    """review P2：知识页往团队域传的知识包——能力组件仍不进 KB（拍板④），但不替上传者悄悄建个人 skill."""
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)
    raw = _zip({"acme-qbr/SKILL.md": _SKILL_MD.encode(), "docs/brief.pdf": b"%PDF-1.4\nbrief"})
    resp = await _ingest(request, user, ctx, raw, scope="team", scope_id="team_test")
    assert {s.filename: s.reason_code for s in resp.skipped} == {"acme-qbr/SKILL.md": "kb_bundle_entry_capability_component"}
    assert [f.filename for f in resp.uploaded] == ["brief.pdf"]
    assert resp.landed_skills == [] and await sis.list_for_user(user_id="u_alice") == []
    assert resp.inventory is not None  # Still return and persist inventory.


@pytest.mark.asyncio
async def test_lying_file_size_only_invalidates_that_skill(tmp_path: Path, monkeypatch) -> None:
    """review P3：一个成员声明大小撒谎 ⇒ BadZipFile 只让那个 skill 记 invalid，包里其他 skill 照常落地."""
    import struct

    # Actual 400KB, declared 300KB: inventory reads first 256KB (no EOF/CRC check)⇒skill; persistence reads declared full length⇒CRC fails.
    raw = _zip(
        {"lie/SKILL.md": b"---\nname: lie\ndescription: d\n---\n" + b"x" * 400_000, "ok/SKILL.md": b"---\nname: ok\ndescription: d\n---\nbody"}
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        info = zf.getinfo("lie/SKILL.md")
    cd_off = raw.find(b"PK\x01\x02")
    assert raw[cd_off + 46 : cd_off + 46 + len(info.filename)] == info.filename.encode()
    patched = bytearray(raw)
    struct.pack_into("<I", patched, cd_off + 24, 300_000)
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)
    resp = await _ingest(request, user, ctx, bytes(patched))
    by_name = {x["name"]: x for x in resp.landed_skills}
    assert by_name["lie"]["status"] == "invalid" and by_name["lie"]["code"] == "E_SKILL_INVALID_ZIP"
    assert by_name["ok"]["status"] == "landed"


@pytest.mark.asyncio
async def test_landed_skills_survive_into_the_persisted_inventory_row(tmp_path: Path, monkeypatch) -> None:
    """批2 review P1-2：`<bundle_import>` 全靠这一列说「落没落地」，而它此前零覆盖——
    两处 `extra=` 一起删掉，242 个测试全绿。这里从真上传一路读到库里的那一行。"""
    request, user, ctx, artifact_store, sis = await _world(tmp_path, monkeypatch)
    raw = _zip({"acme-qbr/SKILL.md": _SKILL_MD.encode(), "docs/brief.pdf": b"%PDF-1.4\nbrief"})
    resp = await _ingest(request, user, ctx, raw)
    stored = await get_bundle_inventory(artifact_store.db_path, bundle_artifact_id=resp.bundle_artifact_id, created_by="u_alice")
    assert stored is not None
    landed = stored["landed_skills"]
    assert [x["name"] for x in landed] == ["acme-qbr"]
    assert landed[0]["status"] == "landed" and landed[0]["import_status"] == "pending"
    # This skill's own red count (not bundle's): rendering uses it for 'N 条阻断项'.
    assert landed[0]["red_count"] == stored["red_count"] > 0
    assert landed[0]["declared_name"] == "acme-qbr"
    # Response inventory and persisted row share the same source.
    assert resp.inventory["landed_skills"] == landed
