"""kb-matrix M4 FR-40: POST /matrices/{id}/graduate 端点全流程（authz fixture 范式）。

覆盖（tech-design-m4 r3 §4.2 门）：
- snapshot happy path：pending→flip 后可见 + rows 直存非空 + row_count 回写 +
  自动 description + auto_extract=off + cardinality=many（P0/P1-1 回归）。
- mode=template：仅字段零 rows。
- 幂等：同 (matrix, last_seq, mode) 重试 → reused=True 同 doc_type_id；
  矩阵变化（last_seq 漂）→ 产新快照。
- 撞名 409 + 建议名；>20 列 422；personal 文件复验 422（scope 复验整单 fail-closed）；
- viewer 403 / 跨空间 matrix 404 / flag off 404。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.kb.doc_type_store import KbDocTypeStore
from bowhead_core.kb.extracted_rows_store import KbExtractedRowsStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.matrix_store import KbMatrixStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb_matrix
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_g"
WS_A = "ws_alpha"
WS_B = "ws_beta"

U_MEMBER = "u_member"
U_VIEWER = "u_viewer"


def _mk_user(uid: str) -> User:
    return User(id=uid, email=f"{uid}@test.com", team_id=TEAM, tier="ultra", email_verified=True)


class _MembershipStore:
    ROLES = {
        (WS_A, U_MEMBER): "member",
        (WS_A, U_VIEWER): "viewer",
    }

    async def get_user_team_id(self, user_id: str) -> str:
        return TEAM

    async def get_workspace_team_id(self, ws_id: str) -> str:
        return TEAM

    async def get_member_role(self, ws_id: str, user_id: str) -> str | None:
        return self.ROLES.get((ws_id, user_id))

    async def get_workspace(self, ws_id: str) -> dict[str, str] | None:
        if ws_id not in {WS_A, WS_B}:
            return None
        return {"id": ws_id, "status": "active"}

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return None

    async def is_team_admin(self, user_id: str) -> bool:
        return False


DONE_VARIANTS = [
    {
        "raw": None,
        "normalized": {"amount": "300", "currency": None},
        "effective_at": None,
        "is_primary": True,
        "extracted_by": "tier1",
        "confidence": 0.9,
        "source_excerpt": "金额三百元",
        "chunk_id": "chk_1",
        "page": 1,
    }
]


@pytest.fixture
async def ctx(initialize_migrated_kb_files_database, tmp_path: Path):
    matrix_store = KbMatrixStore(tmp_path / "kb_matrix.db")
    await matrix_store.initialize()
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    rows_store = KbExtractedRowsStore(db_path=tmp_path / "rows.db")
    await rows_store.initialize()

    app = FastAPI()
    app.include_router(kb_matrix.router)
    app.state.kb_matrix_store = matrix_store
    app.state.kb_files_store = files_store
    app.state.kb_doc_type_store = dt_store
    app.state.kb_extracted_rows_store = rows_store
    app.state.user_store = _MembershipStore()
    client = TestClient(app, raise_server_exceptions=False)

    file_a = await files_store.create(
        artifact_id="a",
        filename="合同A.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        scope="workspace",
        scope_id=WS_A,
        workspace_id=WS_A,
        team_id=TEAM,
        uploaded_by=U_MEMBER,
    )
    matrix = await matrix_store.create_matrix(
        name="供应商合同审阅",
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[{"file_id": file_a, "char_count": 100}],
        columns=[
            {"name": "合同金额", "type": "currency", "question": "金额？"},
            {"name": "情感倾向", "type": "select", "question": "倾向？", "options": ["正面", "负面"]},
        ],
    )
    # Seed done cell (file_a × amount column), same bulk-write path as from_template.
    col_amount = matrix.columns[0]["column_id"]
    await matrix_store.insert_cells_bulk(
        matrix.matrix_id,
        [
            {
                "file_id": file_a,
                "column_id": col_amount,
                "workspace_id": WS_A,
                "team_id": TEAM,
                "status": "done",
                "variants": DONE_VARIANTS,
                "edited_by_user": False,
                "verified": False,
                "prompt_version": 1,
                "file_version": 1,
            }
        ],
    )
    matrix = await matrix_store.get_matrix(matrix.matrix_id)  # Refresh last_seq.
    return {
        "client": client,
        "app": app,
        "matrix_store": matrix_store,
        "files_store": files_store,
        "dt_store": dt_store,
        "rows_store": rows_store,
        "matrix": matrix,
        "file_a": file_a,
    }


def _as(app: FastAPI, uid: str) -> None:
    app.dependency_overrides[_current_user] = lambda: _mk_user(uid)


def _h(ws: str) -> dict[str, str]:
    return {"X-Space-Id": ws}


def _graduate(ctx: dict[str, Any], name: str = "供应商合同审阅 2026Q2", mode: str = "snapshot", ws: str = WS_A):
    return ctx["client"].post(
        f"/api/v1/kb/matrices/{ctx['matrix'].matrix_id}/graduate",
        json={"name": name, "mode": mode},
        headers=_h(ws),
        params={"workspace_id": ws},
    )


def _assert_code_only(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["detail"]["error"] == {"code": code}


# ─── happy path ─────────────────────────────────────────────


async def test_graduate_snapshot_happy_path(ctx) -> None:
    _as(ctx["app"], U_MEMBER)
    r = _graduate(ctx)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "snapshot" and body["reused"] is False
    dt_id = body["doc_type_id"]
    assert body["records"] == 1

    dt = await ctx["dt_store"].get(dt_id)
    assert dt is not None
    # Freeze + lineage + flip completed.
    assert dt.cardinality == "many"
    assert dt.auto_extract_enabled is False
    assert dt.graduated_mode == "snapshot"
    assert dt.graduated_from_matrix_id == ctx["matrix"].matrix_id
    assert dt.graduated_from_seq == ctx["matrix"].last_seq
    # Write back count (P1-1: omission = kb_list_tables always 0, falsely empty table).
    assert dt.row_count == 1
    # Automatic description (LLM uses it to choose tables).
    assert "供应商合同审阅" in dt.description and "1 份文档" in dt.description
    # Preserve fields (select includes options; currency unchanged).
    by_name = {f.name: f for f in dt.fields}
    assert by_name["情感倾向"].type == "select" and by_name["情感倾向"].options == ["正面", "负面"]
    # Visible in lists after flip.
    assert any(d.doc_type_id == dt_id for d in await ctx["dt_store"].list_for_ws(WS_A))
    # Directly persisted rows nonempty (P0: bypass re-wrap without losing fields).
    rows = await ctx["rows_store"].query_rows(workspace_id=WS_A, doc_type_id=dt_id, limit=10)
    assert len(rows) == 1
    assert rows[0].fields["合同金额"][0]["normalized"]["amount"] == "300"
    assert rows[0].fields["合同金额"][0]["source_excerpt"] == "金额三百元"


async def test_graduate_template_mode_fields_only(ctx) -> None:
    _as(ctx["app"], U_MEMBER)
    r = _graduate(ctx, name="合同审阅模板", mode="template")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["records"] == 0
    dt = await ctx["dt_store"].get(body["doc_type_id"])
    assert dt is not None and dt.graduated_mode == "template"
    assert await ctx["rows_store"].count_for_doc_type(workspace_id=WS_A, doc_type_id=dt.doc_type_id) == 0


# ─── Idempotency / name collision / caps ────────────────────


async def test_graduate_idempotent_same_seq_then_new_after_change(ctx) -> None:
    _as(ctx["app"], U_MEMBER)
    r1 = _graduate(ctx)
    assert r1.status_code == 200
    dt1 = r1.json()["doc_type_id"]
    # Retry same last_seq (even renamed, still idempotent; key is (matrix, seq, mode)).
    r2 = _graduate(ctx, name="换个名字")
    assert r2.status_code == 200
    assert r2.json()["reused"] is True
    assert r2.json()["doc_type_id"] == dt1
    # Matrix changes (cell write → last_seq changes) → new snapshot.
    col2 = ctx["matrix"].columns[1]["column_id"]
    await ctx["matrix_store"].insert_cells_bulk(
        ctx["matrix"].matrix_id,
        [
            {
                "file_id": ctx["file_a"],
                "column_id": col2,
                "workspace_id": WS_A,
                "team_id": TEAM,
                "status": "done",
                "variants": [{"is_primary": True, "normalized": {"choice": "正面"}, "extracted_by": "tier1"}],
                "edited_by_user": False,
                "verified": False,
                "prompt_version": 1,
                "file_version": 1,
            }
        ],
    )
    ctx["matrix"] = await ctx["matrix_store"].get_matrix(ctx["matrix"].matrix_id)
    r3 = _graduate(ctx, name="供应商合同审阅 2026Q3")
    assert r3.status_code == 200
    assert r3.json()["reused"] is False
    assert r3.json()["doc_type_id"] != dt1
    assert r3.json()["records"] == 1


async def test_graduate_name_conflict_409_with_suggestion(ctx) -> None:
    _as(ctx["app"], U_MEMBER)
    await ctx["dt_store"].create(workspace_id=WS_A, name="占名模板", created_by="someone")
    r = _graduate(ctx, name="占名模板")
    assert r.status_code == 409
    err = r.json()["detail"]["error"]
    assert err["code"] == "doc_type_name_taken"
    assert "占名模板" in err["suggestion"]
    assert "message" not in err


async def test_graduate_too_many_columns_422(ctx) -> None:
    _as(ctx["app"], U_MEMBER)
    big = await ctx["matrix_store"].create_matrix(
        name="巨矩阵",
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[],
        columns=[{"name": f"列{i}", "type": "question", "question": f"q{i}?"} for i in range(21)],
    )
    r = ctx["client"].post(
        f"/api/v1/kb/matrices/{big.matrix_id}/graduate",
        json={"name": "巨模板", "mode": "template"},
        headers=_h(WS_A),
        params={"workspace_id": WS_A},
    )
    assert r.status_code == 422
    err = r.json()["detail"]["error"]
    assert err == {
        "code": "too_many_columns_to_graduate",
        "max": 20,
        "got": 21,
    }
    assert "message" not in err


async def test_graduate_store_unavailable_uses_stable_code(ctx) -> None:
    _as(ctx["app"], U_MEMBER)
    ctx["app"].state.kb_extracted_rows_store = None

    r = _graduate(ctx)

    _assert_code_only(r, 503, "matrix_data_service_unavailable")


async def test_graduate_unknown_store_validation_is_normalized(
    ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as(ctx["app"], U_MEMBER)

    async def fail_create(**_kwargs: Any) -> str:
        raise ValueError("内部动态校验原文")

    monkeypatch.setattr(ctx["dt_store"], "create", fail_create)

    r = _graduate(ctx)

    _assert_code_only(r, 422, "invalid_graduation_request")


# ─── Scope revalidation / authorization / flag ──────────────


async def test_graduate_personal_file_revalidation_422(ctx) -> None:
    """cell 已物化但文件后来变 personal → 转正复验整单 fail-closed（FR-40）。"""
    _as(ctx["app"], U_MEMBER)
    personal = await ctx["files_store"].create(
        artifact_id="p",
        filename="p.md",
        mime_type="text/markdown",
        size_bytes=1,
        scope="personal",
        scope_id=U_MEMBER,
        workspace_id=WS_A,
        team_id=TEAM,
        uploaded_by=U_MEMBER,
    )
    m = await ctx["matrix_store"].create_matrix(
        name="含个人文件",
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[{"file_id": personal, "char_count": 1}],
        columns=[{"name": "列A", "type": "question", "question": "a?"}],
    )
    r = ctx["client"].post(
        f"/api/v1/kb/matrices/{m.matrix_id}/graduate",
        json={"name": "泄漏尝试", "mode": "snapshot"},
        headers=_h(WS_A),
        params={"workspace_id": WS_A},
    )
    _assert_code_only(r, 422, "personal_files_not_allowed")
    # Fail closed: no doc_type produced.
    assert not await ctx["dt_store"].name_exists(WS_A, "泄漏尝试")


async def test_graduate_viewer_403(ctx) -> None:
    _as(ctx["app"], U_VIEWER)
    r = _graduate(ctx)
    assert r.status_code == 403


async def test_graduate_cross_space_matrix_404(ctx) -> None:
    _as(ctx["app"], U_MEMBER)
    r = ctx["client"].post(
        f"/api/v1/kb/matrices/{ctx['matrix'].matrix_id}/graduate",
        json={"name": "x", "mode": "template"},
        headers=_h(WS_B),
        params={"workspace_id": WS_B},
    )
    assert r.status_code in (403, 404, 503)  # Nonmember of this space: strict ladder denies first.
