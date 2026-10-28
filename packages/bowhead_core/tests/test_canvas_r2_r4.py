"""WP5 — R2 报告状态机 + R4 FTS5 全文搜索 (store 层)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.canvas_store import (
    CANVAS_STATUSES,
    _effective_status,
    _section_search_text,
)

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "t.db")
    await s.initialize()
    return s


# ── R4: FTS5 full-text search ──────────────────────────────


async def test_search_matches_section_body_not_title(store: CanvasStore) -> None:
    """正文里的词(标题没有)也能搜到 —— 这是 R4 的核心(此前只搜 title+id)."""
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u", title="毛利率报告")
    await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "华东区销售下滑由客流减少导致，毛利率从61.2%降到60.1%。"},
    )
    # ≥3 characters (trigram MATCH)
    assert [x["title"] for x in await store.list_for_user(user_id="u", q="销售下滑")] == ["毛利率报告"]
    # Two CJK characters (body LIKE fallback; trigram cannot index them)
    assert [x["title"] for x in await store.list_for_user(user_id="u", q="客流")] == ["毛利率报告"]
    # Contains a numeric substring.
    assert [x["title"] for x in await store.list_for_user(user_id="u", q="61.2")] == ["毛利率报告"]


async def test_search_no_false_positive(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u", title="库存周转")
    await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "库存上升。"})
    assert await store.list_for_user(user_id="u", q="不存在的词") == []


async def test_search_reindexes_on_patch(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u", title="t")
    s = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "库存上升。"})
    assert await store.list_for_user(user_id="u", q="呆滞料") == []
    await store.patch_section(section_id=s.id, patch={"markdown": "库存上升，需关注呆滞料。"})
    assert [x["title"] for x in await store.list_for_user(user_id="u", q="呆滞料")] == ["t"]


async def test_search_reindexes_on_split(store: CanvasStore) -> None:
    """split 后下半段(切到新 section)仍可搜 + 上半段重索引(否则切出的文字搜不到)."""
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u", title="t")
    s = await store.append_section(
        canvas_id=c.id,
        kind="narrative",
        payload={"markdown": "上半段独有词甲鱼。下半段独有词水獭。"},
    )
    offset = s.payload["markdown"].index("下半段")
    await store.split_section(section_id=s.id, offset=offset)
    # Both sections' unique words remain searchable.
    assert [x["title"] for x in await store.list_for_user(user_id="u", q="甲鱼")] == ["t"]
    assert [x["title"] for x in await store.list_for_user(user_id="u", q="水獭")] == ["t"]
    # Key regression: patching the upper section after split must not lose lower-section words through a stale index.
    await store.patch_section(section_id=s.id, patch={"markdown": "上半段独有词甲鱼（已改）。"})
    assert [x["title"] for x in await store.list_for_user(user_id="u", q="水獭")] == ["t"]


async def test_search_drops_on_delete(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u", title="t")
    s = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "独特关键词蝾螈"})
    assert len(await store.list_for_user(user_id="u", q="蝾螈")) == 1
    await store.delete_section(section_id=s.id)
    assert await store.list_for_user(user_id="u", q="蝾螈") == []


async def test_short_query_does_not_depend_on_fts_table(tmp_path: Path) -> None:
    """feedback-0713 #11: <3 字兜底必须走普通表 canvas_sections, 不碰 canvas_fts.

    prod 镜像的 SQLite 3.40.1 对 FTS5 trigram 虚表上的 <3 字 LIKE 会把约束接管
    进索引且不回退线性扫描 —— 静默返回空集; dev/CI 的 3.51 行为正确, 所以
    「在虚表上跑 LIKE」的缺陷会被 CI 掩蔽(works-in-CI-breaks-in-prod)。
    这里清空 fts 索引后断言短词仍命中, 证明短词路径完全不依赖 canvas_fts:
    任何回退到 canvas_fts LIKE 的改动都会让本测试在所有 SQLite 版本上失败。
    """
    path = tmp_path / "t.db"
    s = CanvasStore(path)
    await s.initialize()
    c = await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u", title="t")
    await s.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "各门店客流环比下降。"})
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(str(path)) as db:
        await db.execute("DELETE FROM canvas_fts")
        await db.commit()
    # Two CJK characters (common production examples: stores/profit/traffic) → canvas_sections.payload scan finds them.
    assert [x["id"] for x in await s.list_for_user(user_id="u", q="门店")] == [c.id]
    # Control: ≥3 characters still use trigram MATCH (cleared index → no match), fixing the <3/≥3 routing boundary.
    assert await s.list_for_user(user_id="u", q="客流环比") == []


async def test_short_ascii_query_skips_payload_scan(tmp_path: Path) -> None:
    """feedback-0713 F5-#11 收窄: 纯 ASCII 1-2 字符不扫 raw JSON payload.

    payload 是 JSON 序列化文本, "id"/"kg" 这类短 ASCII 会命中键名/结构字符——
    每个 canvas 都有 "id" 键 → 全库误报。收窄后纯 ASCII 短词只匹配 title/id;
    CJK 短词路径(上一测试)不受影响。
    """
    path = tmp_path / "t.db"
    s = CanvasStore(path)
    await s.initialize()
    c1 = await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u", title="报告甲")
    await s.append_section(canvas_id=c1.id, kind="narrative", payload={"markdown": "正文没有那两个字母。"})
    c2 = await s.ensure_canvas(session_id="s2", workspace_id="w", user_id="u", title="GDP 分析")
    await s.append_section(canvas_id=c2.id, kind="narrative", payload={"markdown": "国内生产总值走势。"})
    # id is a key in every section payload JSON → before narrowing, all Canvases matched (false positives);
    # afterward only title/id are matched; neither c1 nor c2 title contains id → empty result.
    assert await s.list_for_user(user_id="u", q="id") == []
    # Short ASCII terms can still match title (title LIKE path retained).
    assert [x["id"] for x in await s.list_for_user(user_id="u", q="GD")] == [c2.id]


async def test_search_backfills_existing_sections(tmp_path: Path) -> None:
    """老 DB(FTS 表建立前已有 section)首次 boot 应回填进索引."""
    path = tmp_path / "t.db"
    s1 = CanvasStore(path)
    await s1.initialize()
    c = await s1.ensure_canvas(session_id="s1", workspace_id="w", user_id="u", title="t")
    await s1.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "回填测试词椰子"})
    # Simulate a previously absent FTS table: manually clear fts, then reinitialize to trigger backfill.
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(str(path)) as db:
        await db.execute("DROP TABLE canvas_fts")
        await db.commit()
    s2 = CanvasStore(path)
    await s2.initialize()  # Recreate and backfill
    assert [x["title"] for x in await s2.list_for_user(user_id="u", q="椰子")] == ["t"]


def test_section_search_text_skips_structural_keys() -> None:
    body = _section_search_text(
        {
            "id": "sec_x",
            "kind": "narrative",
            "artifact_id": "art_1",
            "markdown": "可搜索正文",
            "title": "标题词",
        }
    )
    assert "可搜索正文" in body
    assert "标题词" in body
    assert "sec_x" not in body  # Skip id
    assert "art_1" not in body  # Skip artifact_id


# ── R2: report state machine ──────────────────────────────


async def test_set_status_valid(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    assert await store.set_status(c.id, "verified") is True
    got = await store.get_canvas(c.id)
    assert got is not None and got.status == "verified"


async def test_set_status_rejects_invalid(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    with pytest.raises(ValueError, match="invalid canvas status"):
        await store.set_status(c.id, "bogus")


async def test_set_status_missing_canvas_returns_false(store: CanvasStore) -> None:
    assert await store.set_status("cv_nope", "archived") is False


async def test_list_returns_status_fields(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    await store.set_status(c.id, "verified")
    item = (await store.list_for_user(user_id="u"))[0]
    assert item["status"] == "verified"
    assert item["effective_status"] == "verified"
    assert item["quarantined_count"] == 0


async def test_quarantine_derives_effective_stale(store: CanvasStore) -> None:
    """有 quarantined section → effective_status=stale(收编 quarantine), 但存储态不变."""
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    sec = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "x"})
    await store.set_quarantine(section_id=sec.id, quarantined=True, reason="t")
    item = (await store.list_for_user(user_id="u"))[0]
    assert item["status"] == "active"  # Persisted state unchanged
    assert item["effective_status"] == "stale"  # Display state derives stale
    assert item["quarantined_count"] == 1


async def test_archived_excluded_from_retrieval_face(store: CanvasStore) -> None:
    """include_archived=False(AI/KB 检索面) 排除 archived; 默认(/reports)仍含."""
    a = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u", title="A")
    await store.ensure_canvas(session_id="s2", workspace_id="w", user_id="u", title="B")
    await store.set_status(a.id, "archived")
    full = {x["title"] for x in await store.list_for_user(user_id="u")}
    face = {x["title"] for x in await store.list_for_user(user_id="u", include_archived=False)}
    assert full == {"A", "B"}
    assert face == {"B"}


def test_effective_status_precedence() -> None:
    assert _effective_status("active", 0) == "active"
    assert _effective_status("verified", 0) == "verified"
    assert _effective_status("active", 3) == "stale"  # Derived from quarantine
    assert _effective_status("verified", 1) == "stale"  # Derived status overrides verified
    assert _effective_status("archived", 5) == "archived"  # Archived takes precedence
    assert set(CANVAS_STATUSES) == {"active", "verified", "stale", "archived"}
