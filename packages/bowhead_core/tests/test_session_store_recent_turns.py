from __future__ import annotations

from bowhead_core.session_store import SessionStore


async def test_list_recent_turn_ids_scopes_and_orders_canonical_turns(tmp_path) -> None:
    store = SessionStore(tmp_path / "recent-turns.db")
    await store.initialize()

    first = await store.ensure_session(
        session_id="s_first",
        workspace_id="ws_allowed",
        user_id="u_one",
    )
    second = await store.ensure_session(
        session_id="s_second",
        workspace_id="ws_allowed",
        user_id="u_two",
    )
    outside = await store.ensure_session(
        session_id="s_outside",
        workspace_id="ws_other",
        user_id="u_one",
    )
    await store.record_turn(turn_id="t_first", session_id=first.id, user_text="first")
    await store.record_turn(turn_id="t_second", session_id=second.id, user_text="second")
    await store.record_turn(turn_id="t_outside", session_id=outside.id, user_text="outside")

    assert await store.list_recent_turn_ids(
        workspace_ids=["ws_allowed"],
        user_id=None,
        limit=10,
    ) == ["t_second", "t_first"]
    assert await store.list_recent_turn_ids(
        workspace_ids=["ws_allowed"],
        user_id="u_one",
        limit=10,
    ) == ["t_first"]
    assert (
        await store.list_recent_turn_ids(
            workspace_ids=[],
            user_id=None,
            limit=10,
        )
        == []
    )


async def test_list_turns_limit_returns_the_newest_n_in_seq_order(tmp_path) -> None:
    """上下文工程批 3 review 修：turn 级历史只拉最近 N 轮，但仍按 seq 升序返回。"""
    store = SessionStore(tmp_path / "list-turns-limit.db")
    await store.initialize()
    session = await store.ensure_session(session_id="s_lim", workspace_id="ws_lim", user_id="u_lim")
    for i in range(5):
        await store.record_turn(turn_id=f"t_lim_{i}", session_id=session.id, user_text=f"q{i}")
        await store.mark_completed(turn_id=f"t_lim_{i}", intent=None, final_answer=f"a{i}", tokens_input=0, tokens_output=0, cost_usd=0.0)
        await store.finalize_turn(session_id=session.id, current_turn_id=f"t_lim_{i}", next_turn_id=None)
    newest_two = await store.list_turns(session_id=session.id, limit=2)
    assert [t.user_text for t in newest_two] == ["q3", "q4"]
    everything = await store.list_turns(session_id=session.id)
    assert [t.user_text for t in everything] == ["q0", "q1", "q2", "q3", "q4"]
    assert [t.user_text for t in await store.list_turns(session_id=session.id, limit=0)] == ["q0", "q1", "q2", "q3", "q4"]


# ── r4: `list_turn_spawns` reads Lead spawns from turn_events (anchor = lead.subagent_summary, task from agent_spawned) ──


async def test_list_turn_spawns_joins_summary_with_task_and_skips_the_lead_itself(tmp_path) -> None:
    import json as _json

    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import TurnLogStore

    db = str(tmp_path / "s.db")
    store = SessionStore(db_path=db)
    await store.initialize()
    log = TurnLogStore(db_path=db)
    await log.initialize()

    async def _ev(turn_id: str, event_type: str, payload: dict) -> None:
        await log.record(turn_id=turn_id, event_type=event_type, payload=payload)

    # Lead's own agent_spawned (no summary ⇒ not a spawn)
    await _ev("t1", "agent_spawned", {"subagent_id": "sub_lead", "profile": "lead", "task": "用户原话"})
    await _ev("t1", "agent_spawned", {"subagent_id": "sub_c1", "profile": "compute", "task": "把口径改成 1-3 月"})
    await _ev("t1", "partial_answer", {"text": "…"})
    await _ev(
        "t1",
        "lead.subagent_summary",
        {
            "spawn_id": "sub_c1",
            "subagent_id": "sub_c1",
            "call_id": "call_00_x",
            "profile": "compute",
            "rationale": "要重算",
            "status": "succeeded_with_warnings",
            "failure_reason": "canvas_flush_failed",
            "child_tool_count": 7,
            "child_canvas_sections": [{"id": "sec_1", "kind": "hbar", "title": "各省退款率"}],
            "outcome_message": "退款率天津最高",
            "duration_ms": 12345,
        },
    )
    # Since r4, summary includes full task / delivery / canvas_mode, taking priority over agent_spawned's 200-character truncation.
    await _ev("t4", "agent_spawned", {"subagent_id": "sub_c4", "profile": "compute", "task": "截断的前两百字"})
    await _ev(
        "t4",
        "lead.subagent_summary",
        {
            "spawn_id": "sub_c4",
            "call_id": "call_4",
            "profile": "compute",
            "status": "succeeded",
            "task": "完整任务全文",
            "delivery": "report",
            "canvas_mode": "revise",
        },
    )
    # No dispatch in turn 2; turn 3's summary lacks call_id (bad data) ⇒ skip.
    await _ev("t2", "agent_spawned", {"subagent_id": "sub_lead2", "profile": "lead", "task": "谢谢"})
    await _ev("t3", "lead.subagent_summary", {"spawn_id": "sub_z", "profile": "compute", "status": "succeeded"})

    out = await store.list_turn_spawns(["t1", "t2", "t3", "t4", "t_missing"])
    assert set(out) == {"t1", "t4"}
    (r4,) = out["t4"]
    assert r4.task == "完整任务全文" and r4.delivery == "report" and r4.canvas_mode == "revise"
    assert out["t1"][0].delivery == "" and out["t1"][0].canvas_mode == ""
    (rec,) = out["t1"]
    assert rec.call_id == "call_00_x" and rec.profile == "compute" and rec.task == "把口径改成 1-3 月"
    assert rec.rationale == "要重算" and rec.status == "succeeded_with_warnings" and rec.failure_reason == "canvas_flush_failed"
    assert rec.child_tool_count == 7 and rec.canvas_sections == (("hbar", "各省退款率"),) and rec.outcome == "退款率天津最高"
    assert await store.list_turn_spawns([]) == {}
    # Rendered JSON can populate tool_call.arguments (fixed key order).
    from bowhead_core.context_assembly import render_spawn_arguments

    assert _json.dumps(render_spawn_arguments(rec), ensure_ascii=False) == _json.dumps(
        {"profile": "compute", "rationale": "要重算", "task": "把口径改成 1-3 月"}, ensure_ascii=False
    )


async def test_list_turn_spawns_falls_back_to_peer_dispatched_for_incomplete_turns_without_summary(tmp_path) -> None:
    """r7（116 第五轮：8/8 取消轮 peer 被杀在 summary 之前）：给了 incomplete_status 的轮没有 summary 时，
    从 peer_dispatched（call_id / profile / rationale）+ agent_spawned（task）合成记录，子工具数按 tool_result 条数；
    有 summary 的未完成轮照旧；不传 incomplete_status 时返回逐字节不变。"""
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import TurnLogStore

    db = str(tmp_path / "s.db")
    store = SessionStore(db_path=db)
    await store.initialize()
    log = TurnLogStore(db_path=db)
    await log.initialize()

    async def _ev(turn_id: str, event_type: str, payload: dict) -> None:
        await log.record(turn_id=turn_id, event_type=event_type, payload=payload)

    # tc: cancelled during peer data retrieval; peer_dispatched / agent_spawned / two tool_result events exist, but no summary.
    await _ev("tc", "agent_spawned", {"subagent_id": "sub_lead", "profile": "lead", "task": "原话"})
    await _ev(
        "tc",
        "peer_dispatched",
        {"call_id": "call_00_c", "profile": "compute", "spawn_id": "sub_c", "subagent_id": "sub_c", "rationale": "取数+报告走 compute"},
    )
    await _ev("tc", "agent_spawned", {"subagent_id": "sub_c", "profile": "compute", "task": "查 2026 上半年各品牌营业额并写报告"})
    await _ev("tc", "tool_result", {"subagent_id": "sub_c", "tool_name": "bi_query", "ok": True})
    await _ev("tc", "tool_result", {"subagent_id": "sub_c", "tool_name": "python_sandbox", "ok": True})
    # tf: A returns (summary), B dispatches later and is killed (no summary) ⇒ use A's summary, synthesize B; seq orders A before B; bad tool_result JSON does not crash.
    await _ev("tf", "peer_dispatched", {"call_id": "call_00_a", "profile": "compute", "spawn_id": "sub_a", "rationale": "a"})
    await _ev("tf", "agent_spawned", {"subagent_id": "sub_a", "profile": "compute", "task": "任务 a"})
    await _ev(
        "tf",
        "lead.subagent_summary",
        {"spawn_id": "sub_a", "call_id": "call_00_a", "profile": "compute", "status": "succeeded", "child_tool_count": 3},
    )
    await _ev("tf", "peer_dispatched", {"call_id": "call_00_b", "profile": "canvas-writer", "spawn_id": "sub_b", "rationale": "b"})
    await _ev("tf", "agent_spawned", {"subagent_id": "sub_b", "profile": "canvas-writer", "task": "任务 b"})
    await _ev("tf", "tool_result", {"subagent_id": "sub_b", "tool_name": "read_report", "ok": True})
    await log.record(turn_id="tf", event_type="tool_result", payload={"subagent_id": "sub_b"})
    import aiosqlite as _aiosqlite

    async with _aiosqlite.connect(db) as raw:
        await raw.execute(
            "UPDATE turn_events SET payload_json='{not json' WHERE turn_id='tf' AND event_type='tool_result' AND seq=(SELECT MAX(seq) FROM turn_events WHERE turn_id='tf')"
        )
        await raw.commit()
    # td: cancelled during Lead compose; peer returned and summary exists ⇒ use normal path without synthesis.
    await _ev("td", "peer_dispatched", {"call_id": "call_00_d", "profile": "compute", "spawn_id": "sub_d", "rationale": "x"})
    await _ev("td", "agent_spawned", {"subagent_id": "sub_d", "profile": "compute", "task": "任务 d"})
    await _ev(
        "td",
        "lead.subagent_summary",
        {"spawn_id": "sub_d", "call_id": "call_00_d", "profile": "compute", "status": "succeeded", "child_tool_count": 6, "task": "任务 d 全文"},
    )
    # te: cancelled before dispatch; nothing exists.
    await _ev("te", "agent_spawned", {"subagent_id": "sub_lead3", "profile": "lead", "task": "原话"})

    plain = await store.list_turn_spawns(["tc", "td", "te", "tf"])
    assert set(plain) == {"td", "tf"} and len(plain["tf"]) == 1
    out = await store.list_turn_spawns(
        ["tc", "td", "te", "tf"], incomplete_status={"tc": "cancelled", "td": "cancelled", "te": "cancelled", "tf": "aborted"}
    )
    assert set(out) == {"tc", "td", "tf"}
    (rc,) = out["tc"]
    assert rc.call_id == "call_00_c" and rc.profile == "compute" and rc.rationale == "取数+报告走 compute"
    assert rc.task == "查 2026 上半年各品牌营业额并写报告" and rc.status == "cancelled" and rc.child_tool_count == 2
    assert rc.canvas_sections == () and rc.outcome == "" and rc.failure_reason is None and rc.synthesized is True
    ra, rb = out["tf"]
    assert (ra.call_id, ra.status, ra.synthesized) == ("call_00_a", "succeeded", False)
    assert (rb.call_id, rb.status, rb.synthesized, rb.child_tool_count, rb.task) == ("call_00_b", "aborted", True, 1, "任务 b")
    assert out["td"] == plain["td"] and out["td"][0].status == "succeeded" and out["td"][0].task == "任务 d 全文"
    # Turns absent from incomplete_status are not synthesized even without summary (completed turns' peer_dispatched must not invent records).
    assert "tc" not in await store.list_turn_spawns(["tc"], incomplete_status={})


async def test_list_turn_spawns_keeps_the_full_task_and_published_failures_of_a_helper_that_never_returned(tmp_path) -> None:
    """A helper that never returned leaves its full task and its failed Tool calls (an interrupted task).

    The task comes from `peer_dispatched`; the failures come from what events already publish.
    """
    from bowhead_core.context_assembly import TASK_TRUNCATION_NOTE, render_spawn_arguments, render_spawn_result
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import TurnLogStore

    db = str(tmp_path / "s.db")
    store = SessionStore(db_path=db)
    await store.initialize()
    log = TurnLogStore(db_path=db)
    await log.initialize()

    async def _ev(turn_id: str, event_type: str, payload: dict) -> None:
        await log.record(turn_id=turn_id, event_type=event_type, payload=payload)

    task = "Inspect the workspace and write an HTML analysis. " * 15
    download = "download_data failed (errcode=0 type=unknown): 下载数据失败, 请检查 query 日志."
    label = "BIQueryFailedError: download_data failed (errcode=0 type=unk"
    assert len(task) > 200
    await _ev("tn", "peer_dispatched", {"call_id": "call_n", "profile": "compute", "spawn_id": "sub_n", "rationale": "r", "task": task})
    await _ev("tn", "agent_spawned", {"subagent_id": "sub_n", "profile": "compute", "task": task[:200]})
    await _ev("tn", "tool_result", {"subagent_id": "sub_n", "tool_name": "bi_schema", "call_id": "c0", "ok": True})
    # A Tool that returned its own error publishes each query's message in the result.
    await _ev(
        "tn",
        "tool_result",
        {
            "subagent_id": "sub_n",
            "tool_name": "bi_query",
            "call_id": "c1",
            "ok": False,
            "failure_code": "tool_execution_failed",
            "result_inline": {
                "ok": False,
                "error_message": "All queries failed.",
                "results": [{"ok": False, "error_message": download}, {"ok": False, "error_message": download}],
            },
        },
    )
    await _ev("tn", "step_done", {"call_id": "c1", "tool_name": "bi_query", "ok": False, "output_preview": "失败: 执行出错"})
    # A Tool that raised publishes a code; its step row shows the first 60 characters of the error.
    for call in ("c2", "c3"):
        await _ev(
            "tn",
            "tool_result",
            {"subagent_id": "sub_n", "tool_name": "bi_query", "call_id": call, "ok": False, "error": "tool_execution_failed", "result_inline": None},
        )
        await _ev("tn", "step_done", {"call_id": call, "tool_name": "bi_query", "ok": False, "output_preview": f"失败: {label}"})
    # Neither a returned message nor a label: the failure code.
    await _ev(
        "tn", "tool_result", {"subagent_id": "sub_n", "tool_name": "python_sandbox", "call_id": "c4", "ok": False, "failure_code": "sandbox_timeout"}
    )
    # Another agent's failure is not this helper's.
    await _ev(
        "tn", "tool_result", {"subagent_id": "sub_lead", "tool_name": "web_search", "call_id": "c5", "ok": False, "failure_code": "search_failed"}
    )
    # A Turn recorded before `peer_dispatched.task` existed keeps only the 200-character preview.
    await _ev("to", "peer_dispatched", {"call_id": "call_o", "profile": "compute", "spawn_id": "sub_o", "rationale": "r"})
    await _ev("to", "agent_spawned", {"subagent_id": "sub_o", "profile": "compute", "task": task[:200]})
    # A helper that returned reads the full task too; its summary keeps 2000 characters.
    long_task = "x" * 2500
    await _ev("tr", "peer_dispatched", {"call_id": "call_r", "profile": "compute", "spawn_id": "sub_r", "rationale": "r", "task": long_task})
    await _ev(
        "tr",
        "lead.subagent_summary",
        {"spawn_id": "sub_r", "call_id": "call_r", "profile": "compute", "status": "succeeded", "child_tool_count": 1, "task": long_task[:2000]},
    )

    out = await store.list_turn_spawns(["tn", "to", "tr"], incomplete_status={"tn": "failed", "to": "cancelled"})
    (new,) = out["tn"]
    assert new.task == task.strip() and new.task_truncated is False and new.child_tool_count == 5
    assert new.tool_failures == (("bi_query", download), ("bi_query", label), ("bi_query", label), ("python_sandbox", "sandbox_timeout"))
    assert render_spawn_arguments(new)["task"] == task.strip()
    assert render_spawn_result(new) == (
        "failed｜子工具 5 次｜没有回执（被中止前是否已写入画布未知，以画布现状为准）｜"
        f"Helper tool failures: bi_query ×1: {download}; bi_query ×2: {label}; python_sandbox ×1: sandbox_timeout"
    )
    (old,) = out["to"]
    # The preview ends on a space here; the cut is still recognized from the recorded length.
    assert task[199] == " " and old.task == task[:200].strip() and len(old.task) == 199 and old.tool_failures == ()
    assert old.task_truncated is True
    (returned,) = out["tr"]
    assert returned.task == long_task and returned.synthesized is False and returned.tool_failures == ()
    assert render_spawn_arguments(old)["task"] == old.task + TASK_TRUNCATION_NOTE
    # A short task that was never cut carries no note.
    await _ev("ts", "peer_dispatched", {"call_id": "call_s", "profile": "compute", "spawn_id": "sub_s", "rationale": "r"})
    await _ev("ts", "agent_spawned", {"subagent_id": "sub_s", "profile": "compute", "task": "Short task."})
    (short,) = (await store.list_turn_spawns(["ts"], incomplete_status={"ts": "aborted"}))["ts"]
    assert short.task_truncated is False and render_spawn_arguments(short)["task"] == "Short task."
