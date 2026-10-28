"""CustomSubagentStore unit tests (#90)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "cs.db")


@pytest.mark.asyncio
async def test_create_happy(db_path: str) -> None:
    from bowhead_core.custom_subagent_store import CustomSubagentStore

    store = CustomSubagentStore(db_path)
    await store.initialize()
    a = await store.create(
        user_id="u1",
        name="pricing_audit",
        display_name="定价稽核",
        description="审核新品定价是否符合规则",
        system_prompt="You are a pricing audit agent...",
        allowed_tools=["bi_ask_question", "bi_fetch_rows"],
        auto_read_skills=["analyst-framework"],
        max_iterations=15,
    )
    assert a.id.startswith("cs_")
    assert a.status == "active"
    assert a.allowed_tools == ["bi_ask_question", "bi_fetch_rows"]


@pytest.mark.asyncio
async def test_reserved_name_rejected(db_path: str) -> None:
    from bowhead_core.custom_subagent_store import CustomSubagentStore

    store = CustomSubagentStore(db_path)
    await store.initialize()
    for name in ("compute", "debate"):
        with pytest.raises(ValueError, match="reserved"):
            await store.create(
                user_id="u1",
                name=name,
                display_name="X",
                description="x",
                system_prompt="x",
            )


def test_filter_destructive_underscore_prefixes() -> None:
    # subagent-unification: actual tool names use underscores (canvas_delete_section, etc.).
    # BI sample tools bi_update_/bi_delete_/bi_create_ were all removed on 2026-07-21, but these three
    # prefixes are **deliberately retained** (see custom_subagent_store.DESTRUCTIVE_TOOL_PREFIXES),
    # so continue using them to verify prefix-filtering behavior.
    # The old dotted prefixes matched nothing; underscore prefixes must strip them.
    from bowhead_core.custom_subagent_store import filter_destructive

    assert filter_destructive(["bi_update_measures", "bi_delete_measures", "bi_query"]) == ["bi_query"]
    assert filter_destructive(["bi_create_measures", "bi_update_dimensions", "canvas_delete_section", "skill_read", "bi_query"]) == [
        "skill_read",
        "bi_query",
    ]
    # safe tools are untouched
    assert filter_destructive(["bi_query", "skill_invoke"]) == ["bi_query", "skill_invoke"]


@pytest.mark.asyncio
async def test_destructive_tools_filtered(db_path: str) -> None:
    from bowhead_core.custom_subagent_store import CustomSubagentStore

    store = CustomSubagentStore(db_path)
    await store.initialize()
    a = await store.create(
        user_id="u1",
        name="bad_actor",
        display_name="X",
        description="x",
        system_prompt="x",
        allowed_tools=[
            "bi_query",
            "bi_update_measures",  # Must be filtered
            "canvas_delete_section",  # Must be filtered
            "skill_invoke",
        ],
    )
    assert "bi_query" in a.allowed_tools
    assert "skill_invoke" in a.allowed_tools
    assert "bi_update_measures" not in a.allowed_tools
    assert "canvas_delete_section" not in a.allowed_tools


@pytest.mark.asyncio
async def test_name_uniqueness_per_user(db_path: str) -> None:
    from bowhead_core.custom_subagent_store import CustomSubagentStore

    store = CustomSubagentStore(db_path)
    await store.initialize()
    await store.create(
        user_id="u1",
        name="agent_a",
        display_name="X",
        description="x",
        system_prompt="x",
    )
    with pytest.raises(ValueError, match="already exists"):
        await store.create(
            user_id="u1",
            name="agent_a",
            display_name="Y",
            description="y",
            system_prompt="y",
        )
    # Another user may use the same name.
    await store.create(
        user_id="u2",
        name="agent_a",
        display_name="Y",
        description="y",
        system_prompt="y",
    )


@pytest.mark.asyncio
async def test_patch_and_delete(db_path: str) -> None:
    from bowhead_core.custom_subagent_store import CustomSubagentStore

    store = CustomSubagentStore(db_path)
    await store.initialize()
    a = await store.create(
        user_id="u1",
        name="test_a",
        display_name="A",
        description="desc",
        system_prompt="sys",
    )
    updated = await store.patch(a.id, display_name="A2", max_iterations=20)
    assert updated is not None
    assert updated.display_name == "A2"
    assert updated.max_iterations == 20

    assert await store.delete(a.id) is True
    assert await store.get(a.id) is None


@pytest.mark.asyncio
async def test_bad_name_rejected(db_path: str) -> None:
    from bowhead_core.custom_subagent_store import CustomSubagentStore

    store = CustomSubagentStore(db_path)
    await store.initialize()
    for bad in ["A", "1leading_digit", "has space", "too$pecial"]:
        with pytest.raises(ValueError, match="name must be"):
            await store.create(
                user_id="u1",
                name=bad,
                display_name="X",
                description="x",
                system_prompt="x",
            )


@pytest.mark.asyncio
async def test_max_iterations_cap(db_path: str) -> None:
    from bowhead_core.custom_subagent_store import CustomSubagentStore

    store = CustomSubagentStore(db_path)
    await store.initialize()
    with pytest.raises(ValueError, match="max_iterations"):
        await store.create(
            user_id="u1",
            name="overiter",
            display_name="X",
            description="x",
            system_prompt="x",
            max_iterations=100,
        )


@pytest.mark.asyncio
async def test_system_prompt_token_gate(db_path: str) -> None:
    """闸按 token 判，不按字符。

    正对照（钉住"调高之后写得下"）：8400 字符的中文提示词（≈7800 token，与我们最大的内置
    agent 同量级）必须存得进去 —— 它在旧的 4000 字符线下是存不进去的。
    反对照：术语密集的中文（实测 1.03 tok/char）在**字符线以内**就已经超 token 线，必须被拒。
    这一对正是"字符不是合适的单位"本身。
    """
    from bowhead_core.custom_subagent_store import MAX_PROMPT_LEN, CustomSubagentStore

    store = CustomSubagentStore(db_path)
    await store.initialize()

    await store.create(
        user_id="u1",
        name="long-cn",
        display_name="X",
        description="x",
        system_prompt="分析销售数据并解释不确定性。" * 600,
    )

    dense = "毛利率·净利率·周转天数、LTV/CAC、渗透率（分渠道）\n" * 533
    assert len(dense) <= MAX_PROMPT_LEN, "反对照必须落在字符线以内，否则测的是字符闸不是 token 闸"
    with pytest.raises(ValueError, match="tokens"):
        await store.create(
            user_id="u1",
            name="dense-cn",
            display_name="X",
            description="x",
            system_prompt=dense,
        )
