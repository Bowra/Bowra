"""首轮**成功**后必须起 AI 会话标题（2026-08-11 116 真机逮到的退化）.

此前全仓只有两个起标题的调用点，都在 orchestrator 的终态 helper 里——
`_finish_ambiguous`（澄清轮）与 `_finish_error`（失败轮）。**正常成功的轮次一个都没有**，
⚠ 更正（独立 review）：那两个 helper 零生产调用方，所以真相不是「失败轮会起」，
而是**从来没有任何路径起过 AI 标题**。用户侧表现为侧边栏标题
一直是问句原文（session_store 在 seq==1 写的占位）。

★ 为什么既有单测一条都没红：缺的是成功路径上一次 fire-and-forget 调用，它不改变
任何返回值、不改变事件序列、不改变持久化内容。只有真发一轮对话再去看侧边栏才看得见。
本文件因此断言的是「派发这个动作发生了、且用对了 router」，而不是任何返回值。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _Store:
    def __init__(self, n_turns: int = 1) -> None:
        self._seq = n_turns  # Current Turn sequence: 1=first Turn.
        self.titles: list[tuple[str, str]] = []

    async def get_turn(self, turn_id: str):
        return SimpleNamespace(seq=self._seq)

    async def list_turns(self, *, session_id: str):  # Must not be called again (large O(n) query).
        raise AssertionError("不许再用 list_turns 判首轮——它是 SELECT * + 全量 hydrate")

    async def set_title(self, *, session_id: str, title: str) -> None:
        self.titles.append((session_id, title))


def _req(**kw):
    base = {
        "turn_id": "t_1",
        "session_id": "s_1",
        "text": "上个月营业额多少？",
        "workspace_id": "w1",
        "user_id": "u1",
        "team_id": "team_a",
        "language_context": None,
        "team_router": None,
        "interactive_session": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _runner(store, router):
    from bowhead_core.turn_runner import TurnRunner

    orch = SimpleNamespace(
        session_store=store,
        router=None,  # Assembly placeholder: must not be used.
        config=SimpleNamespace(llm_agent_type="echo"),
    )
    return TurnRunner(orch), orch


async def _settle():
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_completed_first_turn_spawns_title(monkeypatch):
    store = _Store(n_turns=1)
    team_router = object()
    seen: dict = {}

    async def _fake_gen(**kw):
        seen.update(kw)
        return "上月营业额查询"

    monkeypatch.setattr("bowhead_core.session_title.generate_and_store_title", _fake_gen)
    runner, _orch = _runner(store, team_router)
    result = SimpleNamespace(status=SimpleNamespace(value="completed"), final_answer="9.2 亿")

    await runner._spawn_session_title(_req(team_router=team_router), result)
    await _settle()

    assert seen, "首轮成功后没有起标题——这正是退化本身"
    # ★ Must use the team router: since batch 2-a the assembly placeholder is RefusalAdapter, so using it starts nothing useful.
    assert seen["router"] is team_router, f"用错 router: {seen['router']!r}"
    assert seen["session_id"] == "s_1"
    assert seen["user_text"] == "上个月营业额多少？"


@pytest.mark.asyncio
async def test_non_first_turn_does_not_retitle(monkeypatch):
    """多轮对话不重命名——起标题要烧 LLM，且标题跳变让用户困惑。"""
    store = _Store(n_turns=3)
    called: list = []
    monkeypatch.setattr(
        "bowhead_core.session_title.generate_and_store_title",
        lambda **kw: called.append(kw),
    )
    runner, _ = _runner(store, object())
    result = SimpleNamespace(status=SimpleNamespace(value="completed"), final_answer="x")

    await runner._spawn_session_title(_req(team_router=object()), result)
    await _settle()

    assert not called, "非首轮不该重命名"


@pytest.mark.asyncio
async def test_failed_turn_not_handled_here(monkeypatch):
    """非成功终态由 _finish_error / _finish_ambiguous 各自处理，此处必须放手。"""
    store = _Store(n_turns=1)
    called: list = []
    monkeypatch.setattr(
        "bowhead_core.session_title.generate_and_store_title",
        lambda **kw: called.append(kw),
    )
    runner, _ = _runner(store, object())
    result = SimpleNamespace(status=SimpleNamespace(value="failed"), final_answer="")

    await runner._spawn_session_title(_req(team_router=object()), result)
    await _settle()

    assert not called


@pytest.mark.asyncio
async def test_no_team_router_means_no_title(monkeypatch):
    """拿不到团队 router 就不起——宁可不起，也不去碰装配占位（批 2-a C1 渠道）。"""
    store = _Store(n_turns=1)
    called: list = []
    monkeypatch.setattr(
        "bowhead_core.session_title.generate_and_store_title",
        lambda **kw: called.append(kw),
    )
    runner, orch = _runner(store, None)
    orch.router = None
    result = SimpleNamespace(status=SimpleNamespace(value="completed"), final_answer="x")

    await runner._spawn_session_title(_req(team_router=None), result)
    await _settle()

    assert not called, "无团队 router 时不得回退到装配占位"


def test_success_path_actually_calls_the_spawner() -> None:
    """★ 接线闸：`_run_inner` 的成功持久化块里必须真的调 `_spawn_session_title`。

    上面四条守的是 helper 的**行为**，它们**守不住接线**——实测：把调用点摘掉，
    四条全绿。而本次 bug 恰恰就是「helper 存在、没被接上」（另两个调用点在
    orchestrator 的失败/澄清终态里，成功路径一个都没有）。

    这是本 sprint 反复栽的同一个坑「闸装在被测行为旁边而不是路径上」。理想做法是
    端到端驱动 `_run_inner`，但那需要造出整个 Lead 运行时；退而求其次做源码级接线闸
    ——它抓不到「调了但参数错」，但能抓住本次真实发生的那一类：**调用点消失**。
    参数正确性由上面四条覆盖，两者合起来才完整。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "bowhead_core" / "turn_runner.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_inner":
            target = node
            break
    # Positive control: fail when the function is missing (rename/refactor), never pass silently.
    assert target is not None, "_run_inner 不见了——接线闸失去落点，请重新定位后再改本测试"

    called = {n.func.attr for n in ast.walk(target) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_spawn_session_title" in called, (
        "_run_inner 里没有调用 _spawn_session_title —— 首轮成功后不会起 AI 标题，侧边栏会一直显示用户问句原文（session_store 在 seq==1 写的占位）"
    )


@pytest.mark.asyncio
async def test_non_interactive_producers_never_spawn_title(monkeypatch):
    """★ 来源闸（独立 review P1）：非交互式生产者一律不起标题。

    `_run_inner` 服务**所有** turn 生产者。定时任务每 tick 新建 `s_sched_*` 会话、
    workflow dry-run 每次新建 scratch 会话——两者都满足「首轮」，不挡就是**每跑一次
    白烧一次起标题的 LLM 调用**，而且永远持续下去。

    为什么不能靠 `sessions.origin` 分辨：116 实测 101 个定时任务会话的 origin 全是
    `normal`（只有 dry-run 会 set_origin）。所以只能让生产者显式声明。
    """
    store = _Store(n_turns=1)
    called: list = []
    monkeypatch.setattr(
        "bowhead_core.session_title.generate_and_store_title",
        lambda **kw: called.append(kw),
    )
    runner, _ = _runner(store, object())
    result = SimpleNamespace(status=SimpleNamespace(value="completed"), final_answer="x")

    # Default False = scheduled job / dry-run / undeclared new producer.
    await runner._spawn_session_title(_req(interactive_session=False, team_router=object()), result)
    await _settle()
    assert not called, "非交互式生产者起了标题——每 tick 白烧一次 LLM"


@pytest.mark.asyncio
async def test_title_tokens_are_attributed_to_session_title(monkeypatch):
    """归属（独立 review P2）：起标题的 token 必须记 `reason="session_title"`。

    `create_task` 会拷贝调用方的 contextvar，不显式改 reason 就会记成 `reason="turn"`，
    混进「用户请求驱动」的账里——而 usage.py / user_store.py 的注释都把 session_title
    列为独立分类轴，正是为了把后台自发用量分出来。
    """
    from bowhead_router.usage import current_usage_context

    store = _Store(n_turns=1)
    seen: dict = {}

    async def _fake_gen(**kw):
        seen["reason"] = current_usage_context().reason
        return "标题"

    monkeypatch.setattr("bowhead_core.session_title.generate_and_store_title", _fake_gen)
    runner, _ = _runner(store, object())
    result = SimpleNamespace(status=SimpleNamespace(value="completed"), final_answer="x")

    await runner._spawn_session_title(_req(team_router=object()), result)
    await _settle()

    assert seen.get("reason") == "session_title", f"归属错了：{seen.get('reason')!r}"
