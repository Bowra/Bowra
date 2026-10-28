"""Tests for sprint δ-#7 — delta_lifecycle.py adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.confidence_gate import reset_threshold_cache
from bowhead_core.delta_lifecycle import (
    DefaultDeltaLifecycle,
    NoopDeltaLifecycle,
    build_delta_lifecycle,
)
from bowhead_core.provenance_prompts import reset_cache_for_test
from bowhead_core.registered_fact_store import RegisteredFactStore
from bowhead_core.session_store import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeResp:
    content: str


class _FakeRouter:
    def __init__(self, response: dict[str, Any] | str) -> None:
        self.calls: list[str] = []
        self.text = json.dumps(response, ensure_ascii=False) if isinstance(response, dict) else response

    async def invoke(self, role: str, req: Any) -> _FakeResp:
        self.calls.append(req.messages[0].content)
        return _FakeResp(content=self.text)


@pytest.fixture
async def fact_store(tmp_path: Path) -> RegisteredFactStore:
    db = tmp_path / "facts.db"
    await SessionStore(db).initialize()
    return RegisteredFactStore(db)


@pytest.fixture(autouse=True)
def _resets() -> None:
    reset_cache_for_test()
    reset_threshold_cache()


# ─── builder ───────────────────────────────────────────────


def test_builder_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PROVENANCE_HITL_ENABLED", "false")
    lc = build_delta_lifecycle(fact_store=None)
    assert isinstance(lc, NoopDeltaLifecycle)
    assert lc.is_enabled() is False


def test_builder_noop_when_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PROVENANCE_HITL_ENABLED", "true")
    lc = build_delta_lifecycle(fact_store=None)
    assert isinstance(lc, NoopDeltaLifecycle)


def test_builder_default_when_enabled(fact_store: RegisteredFactStore) -> None:
    lc = build_delta_lifecycle(fact_store=fact_store, enabled=True)
    assert isinstance(lc, DefaultDeltaLifecycle)
    assert lc.is_enabled() is True


# ─── Noop ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_on_pre_llm_returns_empty() -> None:
    lc = NoopDeltaLifecycle()
    out = await lc.on_pre_llm(workspace_id="w", user_text="x")
    assert out == ""


@pytest.mark.asyncio
async def test_noop_on_post_turn_returns_none() -> None:
    lc = NoopDeltaLifecycle()
    task = await lc.on_post_turn(directive="x", workspace_id="w")
    assert task is None


@pytest.mark.asyncio
async def test_noop_on_post_subagent_return_none_action() -> None:
    lc = NoopDeltaLifecycle()
    out = await lc.on_post_subagent_return({"confidence_judgment": {"confidence": 9}})
    assert out.action is None
    assert out.judgment is None


@pytest.mark.asyncio
async def test_noop_on_post_verify_passes() -> None:
    lc = NoopDeltaLifecycle()
    v = await lc.on_post_verify("Q3 GMV 12 亿")
    assert v.passed is True


# ─── Default — on_pre_llm ──────────────────────────────────


@pytest.mark.asyncio
async def test_on_pre_llm_returns_block(fact_store: RegisteredFactStore) -> None:
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="ebitda_definition",
        fact_type="formula_definition",
        value_json='{"f":"营收-COGS"}',
        source="user_directive",
    )
    lc = DefaultDeltaLifecycle(fact_store=fact_store)
    out = await lc.on_pre_llm(workspace_id="ws_1", user_text="EBITDA?")
    assert "Active Facts" in out
    assert "ebitda_definition" in out


# ─── Default — on_post_subagent_return ─────────────────────


@pytest.mark.asyncio
async def test_on_post_subagent_return_high_conf_silent(
    fact_store: RegisteredFactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOWHEAD_PROVENANCE_HITL_ENABLED", raising=False)
    monkeypatch.delenv("BOWHEAD_HITL_CONFIRM_ENABLED", raising=False)
    lc = DefaultDeltaLifecycle(fact_store=fact_store)
    out = await lc.on_post_subagent_return(
        {
            "confidence_judgment": {
                "claim": "EBITDA 是营收-COGS",
                "proposed_source": "[llm-std:]",
                "confidence": 10,
            },
        }
    )
    assert out.action == "use_silent"
    assert out.judgment is not None
    assert out.judgment.confidence == 10


@pytest.mark.asyncio
async def test_on_post_subagent_return_low_conf_hitl(
    fact_store: RegisteredFactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOWHEAD_PROVENANCE_HITL_ENABLED", raising=False)
    lc = DefaultDeltaLifecycle(fact_store=fact_store)
    out = await lc.on_post_subagent_return(
        {
            "confidence_judgment": {
                "claim": "x",
                "proposed_source": "[llm-std:]",
                "confidence": 4,
            },
        }
    )
    assert out.action == "trigger_hitl"


@pytest.mark.asyncio
async def test_on_post_subagent_return_missing_judgment(
    fact_store: RegisteredFactStore,
) -> None:
    lc = DefaultDeltaLifecycle(fact_store=fact_store)
    out = await lc.on_post_subagent_return({"some_other_key": "x"})
    assert out.action is None


@pytest.mark.asyncio
async def test_on_post_subagent_return_subflag_off(
    fact_store: RegisteredFactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_HITL_CONFIRM_ENABLED", "false")
    lc = DefaultDeltaLifecycle(fact_store=fact_store)
    out = await lc.on_post_subagent_return(
        {
            "confidence_judgment": {"confidence": 4, "claim": "x", "proposed_source": "x"},
        }
    )
    assert out.action is None


# ─── Default — on_post_turn (auto_learner fire-and-forget) ─


@pytest.mark.asyncio
async def test_on_post_turn_skip_when_classify_returns_none(
    fact_store: RegisteredFactStore,
) -> None:
    router = _FakeRouter({"type": "skip", "confidence": 0})
    lc = DefaultDeltaLifecycle(fact_store=fact_store, router=router)
    task = await lc.on_post_turn(directive="你好", workspace_id="ws_1")
    assert task is not None
    await task
    facts = await fact_store.list_active(workspace_id="ws_1")
    assert facts == []


@pytest.mark.asyncio
async def test_on_post_turn_no_router_returns_none(
    fact_store: RegisteredFactStore,
) -> None:
    lc = DefaultDeltaLifecycle(fact_store=fact_store, router=None)
    task = await lc.on_post_turn(directive="x", workspace_id="ws_1")
    assert task is None


@pytest.mark.asyncio
async def test_on_post_turn_empty_directive_returns_none(
    fact_store: RegisteredFactStore,
) -> None:
    router = _FakeRouter({"type": "preference", "fact_key": "x"})
    lc = DefaultDeltaLifecycle(fact_store=fact_store, router=router)
    task = await lc.on_post_turn(directive="   ", workspace_id="ws_1")
    assert task is None


# ─── Default — on_post_verify ──────────────────────────────


@pytest.mark.asyncio
async def test_on_post_verify_delegates(fact_store: RegisteredFactStore) -> None:
    text = "Q3 GMV 12.7 亿."  # untagged
    lc = DefaultDeltaLifecycle(fact_store=fact_store)
    v = await lc.on_post_verify(text, workspace_id="ws_1")
    assert v.untagged_claim_count >= 1
    assert v.passed is False


def test_the_post_turn_hook_reads_a_field_that_actually_exists():
    """★★ 2026-09-08 逮到的线上死链：turn_runner 读 `request.user_message`，
    而 `TurnRequest` **没有这个字段**（是 `text`）⇒ `getattr` 恒返 ""
    ⇒ `on_post_turn` 从来没被调用过 ⇒ 116 生产 `registered_facts` **0 行**。

    同款 bug 在 `capture_nl_intent` 那条修过一次，这条漏网。
    这类 bug 的特征是**静默**：没有异常、没有日志，只是一个功能整体不存在。

    ★ 突变验证：把 turn_runner 里那行改回 `user_message` ⇒ 本用例红。
    """
    import re
    from pathlib import Path

    from bowhead_core.types import TurnRequest

    src = Path(__import__("bowhead_core.turn_runner", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    reads = set(re.findall(r'getattr\(request,\s*"([a-z_]+)"', src))
    fields = set(TurnRequest.__dataclass_fields__)
    # Check only text fields used in turn finalization; other getattr calls have their own fallback semantics.
    assert "user_message" not in reads, "TurnRequest 没有 user_message —— 读它就是恒空"
    assert "text" in fields


# ─── r3: structural invariants of two write entries ───────────────────────


@pytest.mark.asyncio
async def test_the_turn_path_can_never_reach_the_team_visible_fact_store(tmp_path, monkeypatch):
    """★★★ 这条用例是「个人域与团队域＝两个写入口」在代码里的**唯一凭证**。

    r3 之前，turn 收尾这条路会把分类结果写进 `RegisteredFactStore`，而
    `scope=project` 落成 `user_id IS NULL` ＝ **工作区共享**
    （`registered_fact_store._scope_filter`：两者皆空即 workspace-shared）。
    也就是模型判定、零确认、直接写成团队可见的东西。

    「关掉 flag」不够 —— 关着的门总有人推开，而推开它的人不会知道门后是团队面。
    所以 r3 把那条路**整条删除**，并用这条用例钉死：**无论分类器返回什么，
    这条路都碰不到 fact store**。

    ★ 用「一碰就炸的 store」而不是「事后查表为空」：后者在写入被
    某个 flag 悄悄挡住时也会绿，分不清「删干净了」和「今天恰好没写」。
    ★ 突变验证：把 `upsert` 调用加回 `_classify_and_write` ⇒ 本用例红。
    """
    from bowhead_core.auto_learner import MODIFICATION_REQUEST, LearnedFact
    from bowhead_core.delta_lifecycle import DefaultDeltaLifecycle

    # Actually exercise the path to prove it cannot touch team scope; with the gate off,
    # no touch only means the entire path did not run, making the test self-validating.
    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "1")

    class _ExplodingFactStore:
        """团队可见面。这条路上任何一次触碰都是 bug。"""

        def __getattr__(self, name: str):
            raise AssertionError(f"turn 收尾这条路碰了团队可见的 fact store（.{name}）——两个写入口被打通了")

    seen: list[Any] = []

    async def _fake_classify(*_a, **_kw):
        return LearnedFact(fact_type=MODIFICATION_REQUEST, fact_key="ranking_chart_type", value_json="", confidence=9)

    async def _hook(fact) -> None:
        # ⚠ Must be async: the hook is awaited; synchronous lambda would raise on await,
        # swallowed as a warning by try/except, making the positive control appear to pass.
        seen.append(fact)

    monkeypatch.setattr("bowhead_core.delta_lifecycle.classify", _fake_classify)
    lc = DefaultDeltaLifecycle(
        fact_store=_ExplodingFactStore(),
        router=object(),
        on_modification_request=_hook,
    )
    await lc._classify_and_write(  # noqa: SLF001
        directive="记住排名图用横向条形图",
        workspace_id="ws",
        session_id="s",
        turn_id="t",
        project_id="p",
        user_id="u",
        hitl_ctx=None,
    )
    # Positive control: path works (otherwise no store access may simply mean the path never ran).
    assert [f.fact_key for f in seen] == ["ranking_chart_type"]


@pytest.mark.asyncio
async def test_the_capture_flag_is_checked_before_the_llm_call_not_after(tmp_path, monkeypatch):
    """★★ 闸关时**连分类器都不许调**。

    ⚠ 这是同一个坑的第二次。老四档的闸原本守在 `classify()` 之前；r3 把老四档
    连同那个 flag 一起删掉之后，`classify()` 就没人挡了 —— 而个人记忆这条路的
    写入闸在更下游，于是每一轮都会白烧一次 LLM 调用、写入却仍然是关的。
    「删一个闸之前先看它到底在守什么」。

    ★ 突变验证：把那道判断挪到 `classify()` **之后** ⇒ 本用例红（分类器被调了一次）。
    只断言「没写库」是不够的——那个断言在闸放错位置时照样绿。
    """
    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "0")
    calls = {"n": 0}

    async def _counting_classify(*_a, **_kw):
        calls["n"] += 1
        return

    monkeypatch.setattr("bowhead_core.delta_lifecycle.classify", _counting_classify)

    db = tmp_path / "f2.db"
    await SessionStore(db).initialize()
    lc = DefaultDeltaLifecycle(fact_store=RegisteredFactStore(db), router=object())
    await lc._classify_and_write(  # noqa: SLF001
        directive="记住排名图用横向条形图",
        workspace_id="ws",
        session_id="s",
        turn_id="t",
        project_id="p",
        user_id="u",
        hitl_ctx=None,
    )
    assert calls["n"] == 0, "闸关时不该调分类器——那是一次真金白银的 LLM 调用"

    # Positive control: enabled must call it, or no call might merely mean a broken path.
    monkeypatch.setenv("FEATURE_MEMORY_AUTO_CAPTURE", "1")
    await lc._classify_and_write(  # noqa: SLF001
        directive="记住排名图用横向条形图",
        workspace_id="ws",
        session_id="s",
        turn_id="t",
        project_id="p",
        user_id="u",
        hitl_ctx=None,
    )
    assert calls["n"] == 1
