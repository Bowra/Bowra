"""Custom subagent integration — runner resolve + prompt injection (#127)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_runner_resolves_custom_profile(tmp_path: Path) -> None:
    """SubagentRunner._resolve_profile 找不到 built-in 时查 custom_store."""
    from bowhead_core.custom_subagent_store import CustomSubagentStore
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolRegistry

    store = CustomSubagentStore(str(tmp_path / "cs.db"))
    await store.initialize()
    await store.create(
        user_id="u1",
        name="my_agent",
        display_name="My Agent",
        description="test",
        system_prompt="You are a test agent.",
        allowed_tools=["bi_ask_question"],
        auto_read_skills=["analyst-framework"],
        max_iterations=15,
    )
    runner = SubagentRunner(
        router=None,
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        custom_store=store,
    )
    # built-in miss → custom hit
    p = await runner._resolve_profile("my_agent", user_id="u1")
    assert p is not None
    assert p.name == "my_agent"
    assert "analyst-framework" in p.system_prompt  # auto_read prelude injection
    # 2026-08-30: `bi_ask_question` registered as `renamed_from` (handler removed earlier; purely a rename)
    # ⇒ normalize to `bi_query` at profile resolution (C2 consolidation). This also proves the custom branch is wired.
    # See docs/design/工具改名迁移.md.
    assert "bi_query" in p.tool_whitelist
    assert "bi_ask_question" not in p.tool_whitelist
    assert p.max_iterations == 15


@pytest.mark.asyncio
async def test_runner_misses_other_user_custom(tmp_path: Path) -> None:
    """u2 不应看到 u1 的自定义 subagent."""
    from bowhead_core.custom_subagent_store import CustomSubagentStore
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolRegistry

    store = CustomSubagentStore(str(tmp_path / "cs.db"))
    await store.initialize()
    await store.create(
        user_id="u1",
        name="u1_agent",
        display_name="U1",
        description="test",
        system_prompt="...",
    )
    runner = SubagentRunner(
        router=None,
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        custom_store=store,
    )
    p = await runner._resolve_profile("u1_agent", user_id="u2")
    assert p is None  # Other users cannot see it.


@pytest.mark.asyncio
async def test_runner_builtin_still_works(tmp_path: Path) -> None:
    """built-in profile lookup 不受 custom_store 影响."""
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolRegistry

    runner = SubagentRunner(
        router=None,
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        custom_store=None,
    )
    p = await runner._resolve_profile("compute", user_id="u1")
    assert p is not None
    assert p.name == "compute"


def test_python_sandbox_selection_grants_both_entries() -> None:
    """One run-code selection grants flat and nested runtime entries.

    The flat form supports long code while nested calls remain compatible with
    existing custom prompts. The authoring catalog still presents one selection.
    """
    from bowhead_core.subagent import _materialize_authoring_pseudo_tools

    names, prelude, allowlist = _materialize_authoring_pseudo_tools(("bi_query", "python_sandbox"))
    assert "python_exec" in names and "skill_invoke" in names
    assert "python_sandbox" not in names  # Authoring pseudo-tool; absent at runtime.
    assert allowlist == ("python_sandbox",)  # Without generic skill_invoke selected ⇒ only this skill may be invoked.
    assert "python_exec" in prelude and "skill_invoke" in prelude

    # Select none, grant none (negative control).
    untouched, empty_prelude, none_allow = _materialize_authoring_pseudo_tools(("bi_query",))
    assert untouched == ("bi_query",) and empty_prelude == "" and none_allow is None


def test_generic_skill_invoke_selection_is_not_narrowed() -> None:
    """作者显式勾了通用 `skill_invoke` 时不设 allowlist —— 这条既有语义不因新增入口而变。"""
    from bowhead_core.subagent import _materialize_authoring_pseudo_tools

    _, _, allowlist = _materialize_authoring_pseudo_tools(("skill_invoke", "python_sandbox"))
    assert allowlist is None
