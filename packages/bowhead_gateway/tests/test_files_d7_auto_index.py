"""file-management-v2 D7 —— 自动索引判据与晋升管线。

核心断言是 PRD §4.4.1-B 的**定义性用例**：报价单 xlsx 自动进知识库、
本月销售明细 xlsx 不进。**两者同为 xlsx** —— 这条断言本身就在证明判据是
语义（retention_hint）而不是文件格式，防止实现哪天退回按扩展名判断。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.artifact_classifier import _parse_retention
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_gateway.kb_promotion import (
    AGENT_PRODUCED_KINDS,
    PromotionSkipped,
    is_kb_promotable,
    promote_artifact_to_kb,
    should_auto_index,
)

if TYPE_CHECKING:
    from pathlib import Path

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PDF = "application/pdf"
# Legacy binary PowerPoint: python-pptx cannot read it, deliberately excluded from KB allowlist (routes/kb.py _ALLOWED_MIMES).
# Previously used text/csv as the negative example; CSV is KB-eligible since 98f05509a.
_UNPARSABLE = "application/vnd.ms-powerpoint"


def _art(**kw):
    base = {
        "id": "art_x",
        "filename": "f.xlsx",
        "mime": _XLSX,
        "size": 10,
        "source": "upload",
        "path": "/tmp/f",
        "canvas_id": None,
        "created_by": "u1",
        "created_at": "2026-07-25T00:00:00Z",
        "kind": "user_upload",
        "retention_hint": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# ── D7 defining cases ──────────────────────────────────────


def test_quote_sheet_indexes_but_sales_detail_does_not_same_mime() -> None:
    """报价单 xlsx 进、销售明细 xlsx 不进 —— 同格式不同命运。"""
    quote = _art(id="art_quote", filename="渠道报价单-2026H2.xlsx", retention_hint="long_term")
    detail = _art(id="art_detail", filename="华东区销售明细.xlsx", retention_hint="one_off")

    ok_q, _ = should_auto_index(quote)
    ok_d, reason_d = should_auto_index(detail)

    assert ok_q is True, "长期参照资料应自动索引"
    assert ok_d is False, "一次性任务输入不应自动索引"
    assert "retention_hint" in reason_d
    # Criterion is not MIME: both have identical MIME types.
    assert quote.mime == detail.mime == _XLSX


@pytest.mark.parametrize(
    "hint,expected",
    [("long_term", True), ("one_off", False), (None, False), ("garbage", False)],
)
def test_retention_hint_drives_decision(hint, expected) -> None:
    ok, _ = should_auto_index(_art(retention_hint=hint))
    assert ok is expected


def test_null_retention_hint_fails_safe() -> None:
    """分类失败/未跑 → NULL → 不索引（宁可少索引，不可乱索引）。"""
    ok, reason = should_auto_index(_art(retention_hint=None))
    assert ok is False
    assert "unset" in reason


def test_agent_products_never_auto_index() -> None:
    """自引用污染防线：agent 产物即便判成 long_term 也不自动进。"""
    for kind in sorted(AGENT_PRODUCED_KINDS):
        ok, reason = should_auto_index(_art(kind=kind, mime=_PDF, retention_hint="long_term"))
        assert ok is False, f"{kind} 不得自动索引"
        assert reason == "agent_produced_needs_confirmation"


def test_run_data_never_auto_index() -> None:
    for kind in ("tool_data", "sandbox_output", "web_clip"):
        ok, _ = should_auto_index(_art(kind=kind, retention_hint="long_term"))
        assert ok is False


def test_unparsable_mime_blocked_technically_not_semantically() -> None:
    # .ppt is outside the KB allowlist (technical filtering), even if semantically classified as long-term.
    ok, reason = should_auto_index(_art(mime=_UNPARSABLE, retention_hint="long_term"))
    assert ok is False
    assert reason.startswith("mime_not_parsable")


def test_manual_promotion_capability_uses_kind_and_backend_mime_truth() -> None:
    assert is_kb_promotable(_art(kind="deliverable", mime=_PDF)) is True
    assert is_kb_promotable(_art(kind="user_upload", mime=_XLSX)) is True
    assert is_kb_promotable(_art(kind="deliverable", mime="application/json")) is False
    assert is_kb_promotable(_art(kind="tool_data", mime=_PDF)) is False


# ── retention_hint parsing ─────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("参考资料\nlong_term", "long_term"),
        ("数据明细\none_off", "one_off"),
        ("口径定义\nLONG_TERM", "long_term"),
        ("参考资料\nlong-term", "long_term"),
        ("其他", None),
        ("", None),
    ],
)
def test_parse_retention(raw, expected) -> None:
    assert _parse_retention(raw) == expected


def test_parse_retention_prefers_one_off_when_both_present() -> None:
    # Models sometimes write '不是 long_term，是 one_off'; searching only long_term would misclassify it as long-term.
    assert _parse_retention("参考资料\n不是 long_term，是 one_off") == "one_off"


# ── Promotion pipeline ─────────────────────────────────────


class _FakeKbStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.parsed: list[str] = []
        self._seq = 0

    async def artifact_ids_in_kb(self, ids):
        return {r["artifact_id"] for r in self.rows.values() if r["artifact_id"] in ids}

    async def find_active_by_artifact(self, artifact_id):
        for fid, r in self.rows.items():
            if r["artifact_id"] == artifact_id:
                return fid
        return None

    async def create(self, **kw):
        self._seq += 1
        fid = f"kbf_{self._seq}"
        self.rows[fid] = kw
        return fid


class _FakeWorker:
    def __init__(self, store: _FakeKbStore) -> None:
        self.store = store

    async def parse_and_index(self, file_id: str) -> None:
        self.store.parsed.append(file_id)


async def test_promotion_reuses_same_blob_no_copy() -> None:
    kb = _FakeKbStore()
    art = _art(id="art_manual", mime=_PDF, filename="手册.pdf")
    fid = await promote_artifact_to_kb(
        artifact=art,
        files_store=kb,
        user_id="u1",
        trigger="manual",
        worker=_FakeWorker(kb),
    )
    # Key point: kb_files row references the **same** artifact_id, with no second copy of the bytes.
    assert kb.rows[fid]["artifact_id"] == "art_manual"
    assert kb.parsed == [fid]


async def test_promotion_is_idempotent() -> None:
    kb = _FakeKbStore()
    art = _art(id="art_dup", mime=_PDF)
    a = await promote_artifact_to_kb(artifact=art, files_store=kb, user_id="u1", trigger="manual")
    b = await promote_artifact_to_kb(artifact=art, files_store=kb, user_id="u1", trigger="auto")
    assert a == b, "重复晋升应返回同一行，不建重复"
    assert len(kb.rows) == 1


async def test_promotion_guard_blocks_auto_for_agent_products() -> None:
    """防线在函数内部——任何新调用点都自动受约束，不靠调用方自觉。"""
    kb = _FakeKbStore()
    art = _art(id="art_deck", kind="deliverable", mime=_PDF)
    with pytest.raises(PromotionSkipped) as ei:
        await promote_artifact_to_kb(artifact=art, files_store=kb, user_id="u1", trigger="auto")
    assert "agent_produced" in ei.value.reason
    assert kb.rows == {}
    # Manual path allowed (explicit user endorsement).
    fid = await promote_artifact_to_kb(artifact=art, files_store=kb, user_id="u1", trigger="manual")
    assert kb.rows[fid]["artifact_id"] == "art_deck"


async def test_promotion_rejects_unparsable_mime() -> None:
    kb = _FakeKbStore()
    with pytest.raises(PromotionSkipped):
        await promote_artifact_to_kb(
            artifact=_art(mime=_UNPARSABLE),
            files_store=kb,
            user_id="u1",
            trigger="manual",
        )


# ── Persistence: retention_hint column ──────────────────────


async def test_retention_hint_persists_and_user_edit_does_not_clobber(tmp_path: Path) -> None:
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=tmp_path / "a.db")
    await store.initialize()
    art = await store.put_bytes(
        content=b"x",
        filename="a.pdf",
        mime=_PDF,
        source="upload",
        created_by="u1",
    )
    await store.set_category(art.id, category="metric_definition", source="llm", retention_hint="long_term")
    got = await store.get(art.id)
    assert got is not None and got.retention_hint == "long_term"

    # User edits to business categories must not erase system retention decisions (orthogonal dimensions).
    await store.set_category(art.id, category="reference", source="user")
    got2 = await store.get(art.id)
    assert got2 is not None
    assert got2.category == "reference"
    assert got2.retention_hint == "long_term", "用户改分类不应清空 retention_hint"


# ── Cost guard: retention_hint must not introduce a second LLM call ──


async def test_category_and_retention_come_from_one_llm_call(tmp_path: Path) -> None:
    """PRD §4.4.1-B2：零新增调用成本。两个字段搭同一趟车。"""
    from bowhead_core.artifact_classifier import classify_artifact

    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f2"), db_path=tmp_path / "b.db")
    await store.initialize()
    art = await store.put_bytes(
        content=("本手册规定各产品线标准报价与折扣审批权限。" * 20).encode(),
        filename="价格政策.txt",
        mime="text/plain",
        source="upload",
        created_by="u1",
    )

    calls: list[str] = []

    class _CountingRouter:
        async def invoke(self, agent_type, req):  # noqa: ANN001
            calls.append(agent_type)
            return SimpleNamespace(content="口径定义\nlong_term")

    cat, hint = await classify_artifact(artifact=art, store=store, router=_CountingRouter())

    assert len(calls) == 1, f"分类应只调一次 LLM，实际 {len(calls)} 次"
    assert cat == "metric_definition", "模型回中文类别名，落库与返回都是代码值"
    assert hint == "long_term"
    fresh = await store.get(art.id)
    assert fresh is not None and fresh.retention_hint == "long_term"


async def test_image_rule_path_costs_no_llm_and_leaves_hint_null(tmp_path: Path) -> None:
    """截图走规则分支：不烧 LLM，retention 留 NULL（fail-safe 不索引）。"""
    from bowhead_core.artifact_classifier import classify_artifact

    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f3"), db_path=tmp_path / "c.db")
    await store.initialize()
    art = await store.put_bytes(
        content=b"\x89PNG\r\n\x1a\n" + b"0" * 40,
        filename="s.png",
        mime="image/png",
        source="upload",
        created_by="u1",
    )

    calls: list[str] = []

    class _Router:
        async def invoke(self, agent_type, req):  # noqa: ANN001
            calls.append(agent_type)
            raise AssertionError("图像分支不该调 LLM")

    cat, hint = await classify_artifact(artifact=art, store=store, router=_Router())
    assert calls == []
    assert cat == "screenshot"
    assert hint is None
    ok, _ = should_auto_index(_art(kind="user_upload", mime="image/png", retention_hint=hint))
    assert ok is False


# ── Authorization guard: promotion endpoint must use the KB upload chain ──


def test_promote_endpoint_has_full_kb_authz_chain() -> None:
    """晋升端点写的是知识库，授权必须与 `upload_file` 逐项一致。

    历史事故（本 sprint code review 自查）：端点最初写在 `routes/files.py`，
    那里没有 KB 的 scope 授权 helper，导致任何用户都能
    `POST {"scope":"team"}` 把自己的文件塞进团队知识库——**越权写入**。
    修法是把端点搬到 `routes/kb.py` 复用完整链。本守卫钉住这条链不被拆掉。
    """
    import re
    from pathlib import Path as _P

    src = _P(__file__).resolve().parents[1] / "src" / "bowhead_gateway" / "routes" / "kb.py"
    text = src.read_text(encoding="utf-8")
    m = re.search(r"async def promote_artifact\(.*?\n(?=@router|\Z)", text, re.S)
    assert m, "promote_artifact 端点不在 routes/kb.py —— 授权 helper 都在这个模块，别搬走"
    body = m.group(0)

    for required, why in [
        ("_make_scope_context_checked", "缺 scope context → 后续判权无依据"),
        ("_require_workspace_context", "space scope 缺 workspace 上下文 → 空对空写入串档"),
        ("can_write_to_scope", "缺 scope 写权限判定"),
        ("_require_kb_scope_grant", "缺授权 grant 检查 → 可越权写团队知识库"),
    ]:
        assert required in body, f"promote_artifact 缺 {required}：{why}"

    # Resolve scope_id server-side; accepting client input would hand over the key to unauthorized access.
    assert "body.scope_id" not in body, "scope_id 不得取自请求体（可拿别的 team/ws id 越权）"
    assert "scope_id = user.id" in body and "scope_id = ctx.team_id" in body


def test_remove_endpoint_goes_through_shared_delete_core() -> None:
    """移出必须走 `_delete_file_core`——space/team 文件不是谁传的谁就能移出。"""
    import re
    from pathlib import Path as _P

    src = _P(__file__).resolve().parents[1] / "src" / "bowhead_gateway" / "routes" / "kb.py"
    text = src.read_text(encoding="utf-8")
    m = re.search(r"async def remove_artifact_from_kb\(.*?\n(?=@router|\Z)", text, re.S)
    assert m, "remove_artifact_from_kb 应在 routes/kb.py"
    body = m.group(0)
    assert "_delete_file_core" in body, "必须复用带权限判定的删除核心，不能直接 soft_delete"


# ── Upload background path ─────────────────────────────────


class _FakeArtifactStore:
    def __init__(self, art) -> None:
        self.art = art

    async def get(self, artifact_id):
        return self.art if artifact_id == self.art.id else None


class _FakeRegistry:
    def __init__(self, worker) -> None:
        self.worker = worker
        self.teams: list[str] = []

    async def get(self, team_id, cfg, version):
        self.teams.append(team_id)
        return SimpleNamespace(worker=self.worker) if self.worker is not None else None


def _upload_app(*, team_id: str | None, worker, kb: _FakeKbStore):
    from unittest.mock import AsyncMock

    art = _art(id="art_up", filename="报价单.pdf", mime=_PDF, retention_hint="long_term")
    user_store = SimpleNamespace(
        get_user=AsyncMock(return_value=SimpleNamespace(team_id=team_id)),
        get_team_llm_config_with_version=AsyncMock(return_value=({}, 1)),
    )
    state = SimpleNamespace(
        artifact_store=_FakeArtifactStore(art),
        kb_files_store=kb,
        user_store=user_store,
        team_kb_runtime_registry=_FakeRegistry(worker),
        entitlements=None,
        deployment_mode="saas",
    )
    return SimpleNamespace(state=state), art


@pytest.fixture
def _no_classify_model(monkeypatch):
    from bowhead_gateway.routes import _deps

    from bowhead_core import artifact_classifier

    async def _resolve(*_a, **_kw):
        return None, None

    async def _classify(**_kw):
        return None

    monkeypatch.setattr(_deps, "resolve_team_router", _resolve)
    monkeypatch.setattr(artifact_classifier, "classify_artifact", _classify)


async def test_upload_auto_indexes_into_the_uploader_team_and_parses(_no_classify_model) -> None:
    from bowhead_gateway.routes.files import _classify_then_maybe_index

    kb = _FakeKbStore()
    app, art = _upload_app(team_id="team_a", worker=_FakeWorker(kb), kb=kb)
    await _classify_then_maybe_index(app, art)
    [(fid, row)] = kb.rows.items()
    assert (row["artifact_id"], row["scope"], row["scope_id"], row["team_id"]) == ("art_up", "personal", "u1", "team_a")
    assert kb.parsed == [fid]


@pytest.mark.parametrize("team_id", [None, "team_a"])
async def test_upload_without_kb_model_or_team_creates_no_kb_row(_no_classify_model, caplog, team_id) -> None:
    """No uploader team, or a team without a KB model: nothing to index into, and no warnings."""
    import logging

    from bowhead_gateway.routes.files import _classify_then_maybe_index

    kb = _FakeKbStore()
    app, art = _upload_app(team_id=team_id, worker=None, kb=kb)
    with caplog.at_level(logging.INFO, logger="bowhead_gateway.routes.files"):
        await _classify_then_maybe_index(app, art)
    assert kb.rows == {}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    reason = "kb_not_configured" if team_id else "uploader_team_unknown"
    assert any(f"reason={reason}" in r.getMessage() for r in caplog.records)


async def test_upload_skips_auto_index_when_kb_is_not_licensed(_no_classify_model) -> None:
    from bowhead_gateway.routes.files import _classify_then_maybe_index

    kb = _FakeKbStore()
    app, art = _upload_app(team_id="team_a", worker=_FakeWorker(kb), kb=kb)
    app.state.deployment_mode = "local"
    app.state.entitlements = SimpleNamespace(is_feature_licensed=lambda *_a: _false())
    await _classify_then_maybe_index(app, art)
    assert kb.rows == {}
    assert app.state.team_kb_runtime_registry.teams == []


async def _false() -> bool:
    return False
