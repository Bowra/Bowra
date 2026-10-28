"""DreamingEngine REM + Deep 聚合测试."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_memory import DreamingEngine

if TYPE_CHECKING:
    from pathlib import Path

_LEGACY_OBSERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS skill_observations (
    id                 TEXT PRIMARY KEY,
    turn_id            TEXT NOT NULL,
    intent             TEXT NOT NULL,
    tool_chain_sig     TEXT NOT NULL,
    section_kinds_sig  TEXT NOT NULL,
    full_signature     TEXT NOT NULL,
    sample_json        TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
"""


async def _seed(db_path: Path) -> None:
    """写 skill_observations + turn_events 给 engine 消费.

    skill_observations 的写入方 SkillDistiller 已删除（docs/skills-tools-ssot.md
    §5-G8）——这里内联遗留 DDL 模拟"老库还有历史数据"的场景。
    """
    from bowhead_core import TurnLogStore

    await TurnLogStore(db_path=str(db_path)).initialize()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(_LEGACY_OBSERVATIONS_DDL)
        await db.commit()

    async with aiosqlite.connect(str(db_path)) as db:
        # 4 turns with the same pattern.
        for i in range(4):
            await db.execute(
                "INSERT INTO skill_observations(id, turn_id, intent, tool_chain_sig, "
                "section_kinds_sig, full_signature, sample_json, created_at) "
                "VALUES (?, ?, 'lookup', 'bi_ask_question', 'eyebrow,h1,kpi_row', "
                "'sig_a', '{}', datetime('now'))",
                (f"obs_{i}", f"t{i}"),
            )
        # 2 turns with another pattern.
        for i in range(2):
            await db.execute(
                "INSERT INTO skill_observations(id, turn_id, intent, tool_chain_sig, "
                "section_kinds_sig, full_signature, sample_json, created_at) "
                "VALUES (?, ?, 'causal', 'bi_attribute', 'eyebrow,h1,attribution', "
                "'sig_b', '{}', datetime('now'))",
                (f"obs_c_{i}", f"tc{i}"),
            )

        # turn_events: 3 turns × multiple events.
        for tid in ["t0", "t1", "tc0"]:
            for seq, (et, payload) in enumerate(
                [
                    ("turn_started", {}),
                    ("tool_result", {"tool_name": "bi_ask_question", "ok": True, "duration_ms": 120}),
                    ("tool_result", {"tool_name": "bi_ask_question", "ok": False, "error": "boom", "duration_ms": 50}),
                    ("done", {"status": "completed"}),
                ],
                start=1,
            ):
                await db.execute(
                    "INSERT INTO turn_events(id, turn_id, seq, event_type, payload_json, occurred_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
                    (f"e_{tid}_{seq}", tid, seq, et, json.dumps(payload)),
                )
            await db.execute(
                "INSERT INTO turn_events(id, turn_id, seq, event_type, payload_json, occurred_at) VALUES (?, ?, ?, 'error', ?, datetime('now'))",
                (f"e_{tid}_5", tid, 5, json.dumps({"message": "hook denied: nope"})),
            )
        await db.commit()


async def test_rem_digest_aggregates(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await _seed(db)
    eng = DreamingEngine(db_path=str(db))
    d = await eng.rem_digest(window_days=30)
    assert d.total_turns == 6
    # The most frequent pattern comes first.
    assert d.top_patterns[0]["intent"] == "lookup"
    assert d.top_patterns[0]["count"] == 4
    assert d.top_patterns[1]["intent"] == "causal"


async def test_rem_prompt_block(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await _seed(db)
    eng = DreamingEngine(db_path=str(db))
    d = await eng.rem_digest(window_days=30)
    block = d.as_prompt_block()
    assert "frequently" in block.lower() or "Your users" in block
    assert "bi_ask_question" in block


async def test_deep_digest_aggregates(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await _seed(db)
    eng = DreamingEngine(db_path=str(db))
    d = await eng.deep_digest(window_hours=24)
    assert d.total_turns == 3
    # Each turn has 2 tool_result events (1 success, 1 failure) → 6 total.
    assert d.tool_usage.get("bi_ask_question") == 6
    assert d.tool_failure_rate["bi_ask_question"] == 0.5
    # Average duration = (120+50)*3 / 6 = 85.
    assert d.avg_duration_ms == 85
    # Deduplicate error samples → 1 entry.
    assert d.errors == ["hook denied: nope"]
    # by_event_type
    assert d.by_event_type.get("done") == 3


async def test_digest_empty_db(tmp_path: Path) -> None:
    """空库也应返回空 digest 不 crash.

    注意：新库没有 skill_observations 表（写入方已删除）——rem_digest 走
    no-such-table 容错路径返回空 digest，本测试同时覆盖这条新路径。
    """
    db = tmp_path / "empty.db"
    from bowhead_core import TurnLogStore

    await TurnLogStore(db_path=str(db)).initialize()

    eng = DreamingEngine(db_path=str(db))
    rem = await eng.rem_digest()
    assert rem.total_turns == 0
    assert rem.top_patterns == []
    assert rem.as_prompt_block() == ""

    deep = await eng.deep_digest()
    assert deep.total_turns == 0
    assert deep.tool_usage == {}
    assert deep.avg_duration_ms is None
