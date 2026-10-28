"""KB routes helper unit tests.

Full TestClient endpoint tests require app lifespan + auth cookie setup (V2 effort).
Day 17-18 covers helpers + schema validation; full e2e in Day 19 Playwright.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.user_store import User
from bowhead_gateway.routes.kb import (
    _ALLOWED_MIMES,
    _enrich_dto,
    _make_scope_context,
    _resolve_kb_mime,
    _resolve_role,
)


def _user(email: str = "alice@test.com", team_id: str = "team_test") -> User:
    return User(id="u_alice", email=email, team_id=team_id, tier="ultra", email_verified=True)


def _file(scope: str = "personal", scope_id: str = "u_alice", uploaded_by: str = "u_alice") -> KbFileDTO:
    return KbFileDTO(
        file_id="kbf_test",
        artifact_id="art_x",
        filename="t.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        scope=scope,  # type: ignore[arg-type]
        scope_id=scope_id,
        workspace_id="ws_42",
        team_id="team_test",
        uploaded_by=uploaded_by,
        uploaded_at="2026-05-04T00:00:00Z",
    )


# ─── _resolve_role (P1-O5 V1 mock via BOWHEAD_AUTO_ADMIN_DOMAINS) ──


def test_resolve_role_default_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_AUTO_ADMIN_DOMAINS", raising=False)
    assert _resolve_role(_user()) == "user"


def test_resolve_role_admin_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com,datarc.cn")
    assert _resolve_role(_user(email="alice@test.com")) == "team_admin"


def test_resolve_role_non_admin_domain_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "datarc.cn")
    assert _resolve_role(_user(email="alice@othercorp.com")) == "user"


def test_resolve_role_no_email_user() -> None:
    user = User(id="u_x", email="", team_id="team", tier="ultra")
    assert _resolve_role(user) == "user"


# ─── _make_scope_context ───────────────────────────────────


def test_make_scope_context_uses_workspace_header() -> None:
    user = _user()
    request = MagicMock()
    request.headers.get = MagicMock(return_value="ws_explicit")
    ctx = _make_scope_context(user, request)
    assert ctx.user_id == "u_alice"
    assert ctx.workspace_id == "ws_explicit"
    assert ctx.team_id == "team_test"


def test_make_scope_context_missing_header_resolves_empty() -> None:
    """批1/c4: X-Workspace-Id missing → workspace_id="" (team id is NOT a workspace;
    workspace-scope operations 400 explicitly via _require_workspace_context)."""
    user = _user()
    request = MagicMock()
    request.headers.get = MagicMock(return_value=None)
    ctx = _make_scope_context(user, request)
    assert ctx.workspace_id == ""
    assert ctx.team_id == "team_test"  # Team scope as usual.


# ─── _enrich_dto (can_delete / can_change_scope flags) ──────


def test_enrich_dto_owner_can_delete() -> None:
    file = _file(scope="personal", scope_id="u_alice", uploaded_by="u_alice")
    ctx = ScopeContext(user_id="u_alice", workspace_id="ws_42", team_id="team_test")
    enriched = _enrich_dto(file, ctx)
    assert enriched.can_delete is True
    assert enriched.can_change_scope is True


def test_enrich_dto_other_user_cannot_delete_personal() -> None:
    file = _file(scope="personal", scope_id="u_alice", uploaded_by="u_alice")
    ctx = ScopeContext(user_id="u_bob", workspace_id="ws_42", team_id="team_test")
    enriched = _enrich_dto(file, ctx)
    assert enriched.can_delete is False
    assert enriched.can_change_scope is False


# ─── _ALLOWED_MIMES ───────────────────────────────────────


def test_allowed_mimes_includes_pdf_docx_md() -> None:
    assert "application/pdf" in _ALLOWED_MIMES
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in _ALLOWED_MIMES
    assert "text/markdown" in _ALLOWED_MIMES
    assert "text/x-markdown" in _ALLOWED_MIMES
    assert "text/plain" in _ALLOWED_MIMES  # P1-S1 fallback


def test_single_upload_size_cap_matches_frontend_promise() -> None:
    """服务端硬闸必须 ≥ 前端承诺的 200MB(否则用户被前端放行、被后端拒)，
    且必须存在 —— 此前本路由**零 size 闸**(bundle/presign 都有，是漏了)。
    """
    from bowhead_gateway.routes.kb import _SINGLE_MAX_SIZE

    assert _SINGLE_MAX_SIZE == 200 * 1024 * 1024  # Match kb-upload-modal.tsx MAX_SIZE.


@pytest.mark.asyncio
async def test_upload_size_gate_rejects_before_read(monkeypatch) -> None:
    """⚠ 真调 handler(非复刻逻辑): 超限文件必须在 `file.read()` **之前**被 413 拦。

    用一个 read() 会抛的 UploadFile —— 若闸放到 read 之后, read() 先炸(证明内存已被吃);
    只有闸在 read 之前, 才会先抛 413。这正是「验证要穿过闸本身」。
    """
    from bowhead_gateway.routes import kb
    from bowhead_gateway.routes.kb import _SINGLE_MAX_SIZE, upload_file
    from fastapi import HTTPException

    # Allow auth/scope before the size gate (this test verifies only size).
    async def _ctx(user, request):
        return SimpleNamespace(workspace_id="ws", team_id="team", role="admin", user_id="u")

    async def _grant(*a, **k):
        return None

    monkeypatch.setattr(kb, "_make_scope_context_checked", _ctx)
    monkeypatch.setattr(kb.KbPermission, "can_write_to_scope", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(kb, "_require_kb_scope_grant", _grant)

    class _ExplodingFile:
        size = _SINGLE_MAX_SIZE + 1
        filename = "big.pdf"
        content_type = "application/pdf"

        async def read(self):
            raise AssertionError("read() 被调 = 闸没挡住, 内存已被吃")

    with pytest.raises(HTTPException) as exc:
        await upload_file(
            request=MagicMock(),
            background_tasks=MagicMock(),
            file=_ExplodingFile(),  # type: ignore[arg-type]
            scope="personal",
            scope_id="u",
            use_llm=False,
            doc_type_id=None,
            user=SimpleNamespace(id="u", email="u@x.cn"),
            _feat=None,
        )
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_single_upload_rejects_text_with_unsupported_extension(monkeypatch) -> None:
    from bowhead_gateway.routes import kb
    from fastapi import HTTPException

    async def _ctx(user, request):
        return SimpleNamespace(workspace_id="ws", team_id="team", role="admin", user_id="u")

    async def _grant(*args, **kwargs):
        return None

    monkeypatch.setattr(kb, "_make_scope_context_checked", _ctx)
    monkeypatch.setattr(kb.KbPermission, "can_write_to_scope", staticmethod(lambda *args, **kwargs: True))
    monkeypatch.setattr(kb, "_require_kb_scope_grant", _grant)
    monkeypatch.setattr(kb, "_get_artifact_store", lambda request: pytest.fail("unsupported file reached storage"))

    class _TextFile:
        size = 25
        filename = "notes.bin"
        content_type = "text/plain"

        async def read(self):
            pytest.fail("unsupported file was read")

    with pytest.raises(HTTPException) as exc:
        await kb.upload_file(
            request=MagicMock(),
            background_tasks=MagicMock(),
            file=_TextFile(),  # type: ignore[arg-type]
            scope="personal",
            scope_id="u",
            use_llm=False,
            doc_type_id=None,
            user=SimpleNamespace(id="u", email="u@x.cn"),
            _feat=None,
        )
    assert exc.value.status_code == 415
    assert exc.value.detail["error"]["code"] == "kb_file_type_unsupported"


def test_allowed_mimes_excludes_dangerous() -> None:
    assert "application/x-msdownload" not in _ALLOWED_MIMES  # .exe
    assert "application/javascript" not in _ALLOWED_MIMES
    assert "text/html" not in _ALLOWED_MIMES  # Prevent HTML script injection.


def test_allowed_mimes_includes_pptx_but_not_legacy_ppt() -> None:
    """pptx-loader sprint: KB 摄入放行 .pptx.

    ⚠ 这条闸曾是本功能的 P0: PptxLoader 接进了 dispatch, 但 `_ALLOWED_MIMES` 没加
    pptx → 用户上传直接 415, loader 在产品里够不着(当时误看成 routes/files.py 的
    ALLOWED_MIME —— 那是**聊天附件**路由, 不是 KB 摄入路由).
    .ppt(旧版二进制)故意不放: python-pptx 读不了, 放进来只会让用户传完再 parse_failed.
    """
    assert "application/vnd.openxmlformats-officedocument.presentationml.presentation" in _ALLOWED_MIMES
    assert "application/vnd.ms-powerpoint" not in _ALLOWED_MIMES  # .ppt deliberately unsupported.


def test_allowed_mimes_all_have_a_loader() -> None:
    """结构闸: 上传闸放行的每个 mime, dispatch 都得有 loader 能处理.

    防的正是本 sprint 踩的那类漂移 —— 两份清单各自维护, 一边加了另一边没加:
    放行但无 loader = 用户传得进却 parse_failed; 有 loader 但没放行 = 功能够不着.
    """
    from bowhead_core.kb.errors import UnsupportedMimeError
    from bowhead_core.kb.loader import get_loader

    for mime in _ALLOWED_MIMES:
        try:
            assert get_loader(mime) is not None
        except UnsupportedMimeError:  # pragma: no cover - readable information on failure.
            raise AssertionError(f"_ALLOWED_MIMES 放行了 {mime!r} 但 dispatch 无 loader → 用户传得进却必 parse_failed") from None


def test_resolve_kb_mime_recovers_pptx_sniffed_as_zip() -> None:
    """OOXML 容器常被 libmagic 嗅成 application/zip —— pptx 要有和 docx/xlsx 对称的兜底.

    缺这条: 加进 _ALLOWED_MIMES 也没用, zip 嗅探的 deck 照样 415.
    """
    pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    for sniffed in ("application/zip", "application/octet-stream", "application/x-zip-compressed"):
        assert _resolve_kb_mime(sniffed, "deck.pptx") == pptx, f"{sniffed} 未被还原成 pptx"
    # Correct sniffing passes through unchanged.
    assert _resolve_kb_mime(pptx, "deck.pptx") == pptx
    # Must not restore .ppt as pptx (python-pptx cannot read it).
    assert _resolve_kb_mime("application/zip", "deck.ppt") != pptx


def test_resolve_kb_mime_recovers_csv_and_allows_it() -> None:
    """CSV 常被嗅成 text/plain 或 Excel 别名：按扩展名收成 text/csv 并放行；非 .csv 的纯文本不受影响。"""
    from bowhead_gateway.routes.kb import _ALLOWED_MIMES

    for sniffed in ("text/plain", "text/csv", "application/csv", "application/vnd.ms-excel", "application/octet-stream"):
        assert _resolve_kb_mime(sniffed, "门店销售.CSV") == "text/csv", sniffed
    assert "text/csv" in _ALLOWED_MIMES
    assert _resolve_kb_mime("text/plain", "notes.txt") == "text/plain"
