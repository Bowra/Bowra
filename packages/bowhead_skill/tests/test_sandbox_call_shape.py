"""Render entry-specific sandbox input shapes with an entry-independent fallback."""

from __future__ import annotations

import pytest
from bowhead_skill.sandbox_call_shape import (
    FLAT_SANDBOX_TOOL,
    SKILL_INVOKE_TOOL,
    sandbox_call_skeleton,
    sandbox_entry_for,
    sandbox_inputs_hint,
    sandbox_position_note,
    sandbox_read_snippet,
)


class TestEntryResolution:
    def test_flat_entry_wins_when_both_present(self) -> None:
        """compute 同时持有两者（`python_exec` + `skill_invoke`）——必须报扁平入口，
        因为模型真正会调的是它（116 实测 flat 418 次 vs skill_invoke 126 次）。"""
        assert sandbox_entry_for(["skill_invoke", "python_exec", "bi_query"]) == FLAT_SANDBOX_TOOL

    def test_skill_invoke_only(self) -> None:
        assert sandbox_entry_for(["skill_invoke", "bi_query"]) == SKILL_INVOKE_TOOL

    @pytest.mark.parametrize("names", [None, (), [], ["bi_query", "add_chart"]])
    def test_unknown_returns_none_never_guesses(self, names) -> None:
        """★ 判不出必须返 None。宁可给一句两条路都对的保底，也不要猜一个绝对层级 ——
        猜错的那一半调用方会把声明写到不被读的位置。"""
        assert sandbox_entry_for(names) is None


class TestSkeletonFollowsTheEntry:
    def test_flat_puts_inputs_beside_code(self) -> None:
        out = sandbox_call_skeleton(FLAT_SANDBOX_TOOL)
        assert out.startswith("python_exec(code=")
        assert '"artifact_id"' in out
        # The flat entry point **must not** include the nested skill_invoke wrapper.
        assert "script=" not in out and "input={" not in out

    def test_skill_invoke_nests_inputs_inside_input(self) -> None:
        out = sandbox_call_skeleton(SKILL_INVOKE_TOOL)
        assert out.startswith("skill_invoke(name=")
        assert 'script="runner.py"' in out
        assert 'input={"code"' in out

    def test_fallback_is_relative_position_not_absolute_level(self) -> None:
        """★ 本文件最重要的一条：保底话术只能讲**相对位置**。

        讲「顶层」对 skill_invoke 的调用方是错的；讲「input 里」对 python_exec 的
        调用方是错的。只有「与 code 同级」两条路都成立。
        """
        out = sandbox_call_skeleton(None)
        note = sandbox_position_note(None)
        # Return plain code only; nested backticks break the caller's code span (observed).
        assert out.startswith("inputs={")
        assert "skill_invoke" not in out and "python_exec" not in out
        # Give the position note separately, and it must describe relative position.
        assert "与 `code` 同级" in note
        assert "顶层" not in note and "input 里" not in note

    def test_known_entry_needs_no_position_note(self) -> None:
        """骨架本身已经把层级写清楚了，再补一句是噪声。"""
        assert sandbox_position_note(FLAT_SANDBOX_TOOL) == ""
        assert sandbox_position_note(SKILL_INVOKE_TOOL) == ""

    def test_key_and_artifact_id_are_threaded_through(self) -> None:
        out = sandbox_call_skeleton(FLAT_SANDBOX_TOOL, key="d16", artifact_id="art_deadbeef1234")
        assert '"d16"' in out and "art_deadbeef1234" in out


class TestHintCarriesBothHalves:
    """★ 骨架和取法必须成对出现 —— 只给一半模型照样跑不通。

    真机形态：模型记住了「代码里写 INPUTS['rows']['path']」这一半（最显眼），
    丢了「声明放哪」那一半，于是 INPUTS 为空、KeyError。
    """

    @pytest.mark.parametrize("entry", [FLAT_SANDBOX_TOOL, SKILL_INVOKE_TOOL, None])
    def test_full_hint_has_declaration_and_read(self, entry) -> None:
        h = sandbox_inputs_hint(entry, artifact_id="art_abc123456789")
        assert "art_abc123456789" in h  # Declaration side.
        assert sandbox_read_snippet() in h  # Retrieval side.

    @pytest.mark.parametrize("entry", [FLAT_SANDBOX_TOOL, SKILL_INVOKE_TOOL, None])
    def test_short_hint_still_has_both_halves(self, entry) -> None:
        """预算裁剪档也不许只剩一半 —— 那正是旧行为（整条 pop 掉只留一句没形状的话）。"""
        h = sandbox_inputs_hint(entry, artifact_id="art_abc123456789", short=True)
        assert "art_abc123456789" in h
        assert sandbox_read_snippet() in h

    def test_short_is_actually_shorter(self) -> None:
        full = sandbox_inputs_hint(None, artifact_id="art_x")
        short = sandbox_inputs_hint(None, artifact_id="art_x", short=True)
        assert len(short) < len(full) / 2

    def test_row_count_optional(self) -> None:
        assert "读全量 42 行" in sandbox_inputs_hint(None, artifact_id="art_x", row_count=42)
        assert "读全量" not in sandbox_inputs_hint(None, artifact_id="art_x")


def test_no_consumer_hardcodes_the_nesting_level() -> None:
    """反向守卫：全仓不许再出现「skill_invoke 的 input 里」这种写死一个入口的提示。

    ⚠ 这条是「改完别被悄悄改回去」的闸 —— 本次的价值全在于**提示跟着入口走**，
    有人图省事直接写死一句，行为测试抓不到（那句话对一半调用方仍然"看起来对"）。
    """
    import ast
    import pathlib

    # Check only string literals **that may reach the model**. Three versions evolved, each driven by its own failing result:
    #   v1 whole-file substring → failed on repository comments (historical explanations for humans)
    #   v2 tokenize STRING only → failed on docstrings (also STRING tokens)
    #   v3 ast excluding docstrings ← current version
    # Lesson: guard criteria must match what actually reaches the model, not mere text appearing somewhere in a file.
    PHRASE = "skill_invoke 的 input 里"
    root = pathlib.Path(__file__).resolve().parents[3]
    offenders: list[str] = []
    for pkg in ("bowhead_core", "bowhead_skill"):
        src = root / "packages" / pkg / "src"
        if not src.exists():
            continue
        for f in src.rglob("*.py"):
            if f.name == "sandbox_call_shape.py":  # The only place allowed to discuss nesting levels.
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = getattr(node, "body", None)
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings and PHRASE in node.value:
                    offenders.append(str(f.relative_to(root)))
                    break
    assert offenders == [], f"这些地方又把嵌套层级写死在发给模型的串里：{offenders}"
