"""Advisory file classification and explicit promotion capability boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.artifact_classifier import _parse_retention
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_gateway.kb_promotion import (
    is_kb_promotable,
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


@pytest.mark.parametrize("hint", ["long_term", "one_off", None, "garbage"])
def test_classification_never_authorizes_membership(hint):
    assert should_auto_index(_art(retention_hint=hint)) == (False, "explicit_knowledge_action_required")


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
