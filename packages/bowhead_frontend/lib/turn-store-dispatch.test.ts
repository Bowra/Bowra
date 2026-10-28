import { afterEach, describe, expect, it } from "vitest";

import {
  _APPLIED_SEQ_MAX_TURNS,
  _resetNarrativeCursorsForTesting,
  dispatchToTurnStore,
  isNarrativeSegActive,
} from "./turn-store-dispatch";
import {
  _resetAllTurnsForTesting,
  getTurnSnapshot,
  hasBlockingApprovalState,
  selectAnswerPayload,
  selectNarrativeBuffer,
} from "./turn-store";

const T = "turn-disp";

afterEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

describe("turn-store-dispatch — basic routing", () => {
  it("routes a live corrected question by its Turn ID without a step card ID", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_list_card_created", { card_id: "another-turn" });
    const claimed = dispatchToTurnStore("clarification_required", {
      turn_id: T,
      clarification_id: "original-question",
      fallback_text: "Which title?",
      narrative_superseded: true,
      seq: 3,
    });
    expect(claimed).toBe(false);
    expect(getTurnSnapshot(T)?.narrativeSuperseded).toBe(true);
    expect(getTurnSnapshot("another-turn")?.narrativeSuperseded).toBe(false);
    dispatchToTurnStore("turn.resumed", { turn_id: T, request_id: "original-question", seq: 4 });
    expect(getTurnSnapshot(T)?.narrativeSuperseded).toBe(true);
  });

  it("step_list_card_created → applyCardCreated", () => {
    const claimed = dispatchToTurnStore("step_list_card_created", { card_id: T });
    expect(claimed).toBe(true);
    expect(getTurnSnapshot(T)?.cardCreated).toBe(true);
  });

  it("step_list_card_finalized → applyCardFinalized", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_list_card_finalized", {
      card_id: T,
      state: "completed",
      step_count: 5,
    });
    const s = getTurnSnapshot(T);
    expect(s?.cardFinalized).toBe(true);
    expect(s?.cardFinalState).toBe("completed");
  });

  it("turn.resumed reopens the same turn card for continued execution", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T, seq: 1 });
    dispatchToTurnStore("step_list_card_finalized", {
      card_id: T,
      state: "failed",
      step_count: 1,
      seq: 2,
    });

    const claimed = dispatchToTurnStore("turn.resumed", {
      turn_id: T,
      request_id: "clar_1",
      seq: 3,
    });

    // turn.resumed still falls through to the legacy reducer for HITL
    // tombstones/answered-summary state, while also reopening turn-store.
    expect(claimed).toBe(false);
    expect(getTurnSnapshot(T)?.cardFinalized).toBe(false);
    expect(getTurnSnapshot(T)?.cardFinalState).toBeNull();
    expect(getTurnSnapshot(T)?.totalDurationMs).toBeNull();
  });

  it("step_started → applyStepStarted with all fields", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "查询数据",
      started_at: "2026-05-22T14:32:00Z",
      parent_step_id: null,
      concurrency_group_id: "g1",
      input_preview: "SELECT ...",
      tool_name: "bi_query",
      call_id: "c1",
      code_input: {
        language: "python",
        code: "print('persisted')",
        truncated: false,
      },
    });
    const step = getTurnSnapshot(T)?.steps[0];
    expect(step?.stepId).toBe("s1");
    expect(step?.toolName).toBe("bi_query");
    expect(step?.concurrencyGroupId).toBe("g1");
    expect(step?.label).toBe("SELECT ...");
    expect(step?.callId).toBe("c1");
    expect(step?.codeInput).toEqual({
      language: "python",
      code: "print('persisted')",
      truncated: false,
    });
  });

  it("chat-solidify #4: clarification_ask step_started creates NO step beat", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    const claimed = dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "clar1",
      step_type: "tool_run",
      header_zh: "请用户澄清",
      started_at: "2026-07-05T00:00:00Z",
      tool_name: "clarification_ask",
      // raw label+summary JSON that used to render as a bare-JSON step row
      input_preview: '{"label":"选择区域","summary":"用柱状图对比各区域销售额"}',
    });
    // Handled (consumed, no legacy fall-through) but NO trace step created —
    // clarification is surfaced by the clarification card (clarification_required).
    expect(claimed).toBe(true);
    expect(getTurnSnapshot(T)?.steps.length ?? 0).toBe(0);
  });

  it("step_progress appends content_delta", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "lead_thinking_writing",
      header_zh: "写代码",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "lead_thinking_writing_python",
    });
    dispatchToTurnStore("step_progress", {
      card_id: T,
      step_id: "s1",
      content_delta: "import ",
    });
    dispatchToTurnStore("step_progress", {
      card_id: T,
      step_id: "s1",
      content_delta: "pandas\n",
    });
    expect(getTurnSnapshot(T)?.steps[0]?.partialContent).toBe("import pandas\n");
  });

  it("step_done normalizes wire state (success → done, failure → failed)", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "查询",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "bi_query",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      state: "success",
      duration_ms: 700,
      output_preview: "5 / 5 成功",
    });
    expect(getTurnSnapshot(T)?.steps[0]?.state).toBe("done");
    expect(getTurnSnapshot(T)?.steps[0]?.durationMs).toBe(700);
  });

  it("step_done with failure state + error → normalizes to FailureKind permanent/transient", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "parse",
      header_zh: "解析文件",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "parse_file",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "parse",
      state: "failure",
      duration_ms: 100,
      error: {
        type: "corrupted",
        message_zh: "文件损坏",
        is_transient: false,
      },
    });
    const step = getTurnSnapshot(T)?.steps[0];
    expect(step?.state).toBe("failed");
    expect(step?.error?.kind).toBe("permanent");
    expect(step?.error?.message).toBe("文件损坏");
  });

  it("step_done keeps finite Power BI code and typed diagnostics", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "pbi1",
      step_type: "tool_run",
      header_zh: "查询 Power BI",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "pbi_execute_dax",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "pbi1",
      step_type: "tool_run",
      state: "failure",
      duration_ms: 100,
      error: {
        type: "pbi",
        message_zh: "legacy provider prose",
        is_transient: true,
        code: "pbi_query_unreachable",
        diagnostics: {
          schema_version: 1,
          disclosure: "authenticated",
          diagnostic_code: "powerbi_query_failure",
          safe_detail: "phase=execute; failure_class=network",
          retryable: true,
        },
      },
    });

    const error = getTurnSnapshot(T)?.steps[0]?.error;
    expect(error?.code).toBe("pbi_query_unreachable");
    expect(error?.diagnostics).toMatchObject({
      code: "pbi_query_unreachable",
      diagnosticCode: "powerbi_query_failure",
      safeDetail: "phase=execute; failure_class=network",
      retryable: true,
    });
  });

  it("step_done rejects a Power BI diagnostic with the wrong disclosure", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "pbi_admin",
      step_type: "tool_run",
      header_zh: "查询 Power BI",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "pbi_execute_dax",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "pbi_admin",
      step_type: "tool_run",
      state: "failure",
      error: {
        type: "pbi",
        message_zh: "stable fallback",
        is_transient: true,
        code: "pbi_query_failed",
        diagnostics: {
          schema_version: 1,
          disclosure: "admin",
          diagnostic_code: "powerbi_admin_failure",
          safe_detail: "phase=execute; failure_class=upstream",
        },
      },
    });

    const error = getTurnSnapshot(T)?.steps[0]?.error;
    expect(error?.code).toBe("pbi_query_failed");
    expect(error?.diagnostics).toBeUndefined();
  });

  it("step_done derives detail from result_inline table {columns, rows: [{}]}", () => {
    // 0.39.4 bug B/C — BE bi_query step_done payload carries
    // result_inline={columns,rows} which used to be ignored, leaving
    // step.detail null → step NOT expandable. Fix in turn-store-dispatch.ts
    // builds StepDetail{kind:"table"} from this shape.
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "查询",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "bi_query",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      state: "success",
      duration_ms: 700,
      output_preview: "12 行 · 2 列",
      result_inline: {
        columns: ["月份", "GMV"],
        rows: [
          { 月份: "2025-01", GMV: 19821 },
          { 月份: "2025-02", GMV: 17832 },
        ],
      },
    });
    const step = getTurnSnapshot(T)?.steps[0];
    expect(step?.detail).not.toBeNull();
    expect(step?.detail?.kind).toBe("table");
    if (step?.detail?.kind === "table") {
      expect(step.detail.cols).toEqual(["月份", "GMV"]);
      expect(step.detail.rows).toHaveLength(2);
      expect(step.detail.rows[0]).toEqual(["2025-01", 19821]);
    }
  });

  it("step_done derives detail from result_inline {sql} for SQL-shaped tools", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "查询",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "bi_query",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      state: "success",
      duration_ms: 500,
      result_inline: { sql: "SELECT 月份, SUM(GMV) FROM data GROUP BY 月份" },
    });
    const step = getTurnSnapshot(T)?.steps[0];
    expect(step?.detail?.kind).toBe("sql");
  });

  it("step_done python_sandbox derives stdout detail from {stdout, stderr, exit_code}", () => {
    // 0.39.7 #4 — sandbox fail step body used to be blank: existing
    // _deriveDetailFromInline only handled table/sql/code shapes. Real
    // sandbox result shape is {ok, exit_code, stdout, stderr, ...} —
    // user expanding "运行代码 临时失败" got an empty body and couldn't
    // see the traceback that explained the failure.
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "运行代码",
      started_at: "2026-05-27T14:32:00Z",
      tool_name: "python_sandbox",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      state: "failed",
      duration_ms: 320,
      output_preview: "运行出错",
      tool_name: "python_sandbox",
      result_inline: {
        ok: false,
        exit_code: 1,
        stdout: "loading dataset...\n",
        stderr: "Traceback (most recent call last):\n  ZeroDivisionError",
      },
    });
    const step = getTurnSnapshot(T)?.steps[0];
    expect(step?.detail?.kind).toBe("stdout");
    if (step?.detail?.kind === "stdout") {
      expect(step.detail.text).toContain("loading dataset...");
      expect(step.detail.text).toContain("# stderr");
      expect(step.detail.text).toContain("ZeroDivisionError");
      expect(step.detail.text).toContain("exit_code=1");
    }
  });

  it("step_done python_sandbox handles result_json wrapper shape", () => {
    // pycell.py wraps result as `{result_json: {stdout, stderr, ...}}` in some
    // dispatch paths (skill_invoke python_sandbox). Renderer should peek inside.
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "运行代码",
      started_at: "2026-05-27T14:32:00Z",
      tool_name: "python_sandbox",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      state: "success",
      duration_ms: 120,
      tool_name: "python_sandbox",
      result_inline: {
        result_json: {
          ok: true,
          exit_code: 0,
          stdout: "23.41\n",
          stderr: "",
        },
      },
    });
    const step = getTurnSnapshot(T)?.steps[0];
    expect(step?.detail?.kind).toBe("stdout");
    if (step?.detail?.kind === "stdout") {
      expect(step.detail.text.trim()).toBe("23.41");
    }
  });

  it("step_done python_sandbox stdout-only (no stderr, exit=0) renders bare stdout", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "运行代码",
      started_at: "2026-05-27T14:32:00Z",
      tool_name: "python_sandbox",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      state: "success",
      duration_ms: 95,
      tool_name: "python_sandbox",
      result_inline: { ok: true, exit_code: 0, stdout: "hello world" },
    });
    const step = getTurnSnapshot(T)?.steps[0];
    expect(step?.detail?.kind).toBe("stdout");
    if (step?.detail?.kind === "stdout") {
      expect(step.detail.text).toBe("hello world");
      expect(step.detail.text).not.toContain("stderr");
      expect(step.detail.text).not.toContain("exit_code");
    }
  });

  it("step_done keeps bounded sandbox output refs when result_inline is capped", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "sandbox-output",
      step_type: "tool_run",
      header_zh: "运行代码",
      started_at: "2026-08-06T00:00:00Z",
      tool_name: "python_sandbox",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "sandbox-output",
      step_type: "tool_run",
      state: "success",
      duration_ms: 551,
      result_inline: null,
      result_meta: {
        output_artifacts: [
          {
            artifact_id: "art_stdout",
            filename: "sandbox-stdout.txt",
            mime: "text/plain",
            size: 12,
            data_role: "stdout",
          },
          {
            artifact_id: "art_calc",
            filename: "calc.md",
            mime: "text/markdown",
            size: 17,
          },
          { artifact_id: "art_stdout", filename: "duplicate.txt" },
          { filename: "missing-id.txt" },
          { artifact_id: "x".repeat(201), filename: "oversized-id.txt" },
        ],
      },
    });

    expect(getTurnSnapshot(T)?.steps[0]?.outputArtifacts).toEqual([
      {
        artifactId: "art_stdout",
        filename: "sandbox-stdout.txt",
        mime: "text/plain",
        size: 12,
        dataRole: "stdout",
      },
      {
        artifactId: "art_calc",
        filename: "calc.md",
        mime: "text/markdown",
        size: 17,
      },
    ]);
  });

  it("step_done caps untrusted output refs before storing them", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "many-sandbox-outputs",
      step_type: "tool_run",
      header_zh: "运行代码",
      started_at: "2026-08-06T00:00:00Z",
      tool_name: "python_sandbox",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "many-sandbox-outputs",
      step_type: "tool_run",
      state: "success",
      result_meta: {
        output_artifacts: Array.from({ length: 40 }, (_, index) => ({
          artifact_id: `art_${index}`,
          filename: `${index}.txt`,
        })),
      },
    });

    const refs = getTurnSnapshot(T)?.steps[0]?.outputArtifacts;
    expect(refs).toHaveLength(32);
    expect(refs?.at(-1)?.artifactId).toBe("art_31");
  });

  it("step_done recovers output refs from old result_inline events", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "old-sandbox-output",
      step_type: "tool_run",
      header_zh: "运行代码",
      started_at: "2026-08-05T00:00:00Z",
      tool_name: "python_sandbox",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "old-sandbox-output",
      step_type: "tool_run",
      state: "success",
      duration_ms: 10,
      result_inline: {
        output_artifacts: [
          { artifact_id: "art_legacy", name: "legacy.md" },
        ],
      },
    });

    expect(getTurnSnapshot(T)?.steps[0]?.outputArtifacts).toEqual([
      { artifactId: "art_legacy", filename: "legacy.md" },
    ]);
  });

  it("step_done with no result_inline → detail stays null (back-compat)", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "查询",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "subagent_spawn",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      state: "success",
      duration_ms: 100,
      output_preview: "派 compute",
    });
    expect(getTurnSnapshot(T)?.steps[0]?.detail).toBeNull();
  });

  it("step_done with sub_steps maps WireSubStep → SubStepResult", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "查询",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "bi_query",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      state: "partial",
      duration_ms: 700,
      sub_steps: [
        { label: "SELECT a", state: "success", output_preview: "10 rows" },
        { label: "SELECT b", state: "failure", error_kind: "sql_error" },
      ],
    });
    const subs = getTurnSnapshot(T)?.steps[0]?.subSteps;
    expect(subs?.length).toBe(2);
    expect(subs?.[0]?.status).toBe("done");
    expect(subs?.[1]?.status).toBe("failed");
  });

  it("emoji_ack with __turnId routes to applyEmojiAck", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("emoji_ack", {
      target_msg_id: "user-msg-1",
      emoji: "🤝",
      __turnId: T,
    });
    const acks = getTurnSnapshot(T)?.emojiAcks;
    expect(acks?.length).toBe(1);
    expect(acks?.[0]?.emoji).toBe("🤝");
  });

  it("unknown event names return false (caller falls through)", () => {
    expect(dispatchToTurnStore("partial_answer", { token: "x" })).toBe(false);
    expect(dispatchToTurnStore("tool_call", { call_id: "c1" })).toBe(false);
    expect(dispatchToTurnStore("done", { turn_id: "x" })).toBe(false);
  });

  it("missing card_id drops the event silently (returns false)", () => {
    expect(dispatchToTurnStore("step_started", { step_id: "s1" })).toBe(false);
    expect(dispatchToTurnStore("step_progress", { step_id: "s1" })).toBe(false);
  });

  it("turn.follow_up no-ops but claims the event", () => {
    expect(
      dispatchToTurnStore("turn.follow_up", {
        follow_up_turn_id: "t2",
        follow_up_stream_url: "/x",
        reason: "steer_drain",
        buffered_count: 1,
      }),
    ).toBe(true);
    // chat-solidify 2026-07-03: removed the dead emoji_remove name (never emitted by the backend); no longer claimed.
    expect(
      dispatchToTurnStore("emoji_remove", { target_msg_id: "m1", emoji: "🤝", __turnId: T }),
    ).toBe(false);
  });

  // Phase 1.6 G3 — Lead narrative streaming (reads via Phase 1.7-A selector)
  it("narrative.delta → applyNarrativeAppend accumulates by card_id", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    expect(
      dispatchToTurnStore("narrative.delta", { card_id: T, delta: "好的，" }),
    ).toBe(true);
    expect(
      dispatchToTurnStore("narrative.delta", { card_id: T, delta: "我先查 Q3 数据" }),
    ).toBe(true);
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("好的，我先查 Q3 数据");
  });

  it("narrative.delta drops events without card_id", () => {
    expect(
      dispatchToTurnStore("narrative.delta", { delta: "orphan" }),
    ).toBe(false);
  });

  it("narrative.delta claims but skips empty delta (no buffer mutation)", () => {
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "abc" });
    expect(
      dispatchToTurnStore("narrative.delta", { card_id: T, delta: "" }),
    ).toBe(true);
    // empty delta did not extend the buffer
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("abc");
  });

  it("narrative.delta skips duplicate persisted seq for the same turn", () => {
    dispatchToTurnStore("narrative.delta", { card_id: T, seq: 7, delta: "上面" });
    dispatchToTurnStore("narrative.delta", { card_id: T, seq: 8, delta: "两个问题" });
    dispatchToTurnStore("narrative.delta", { card_id: T, seq: 7, delta: "上面" });
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("上面两个问题");

    dispatchToTurnStore("narrative.delta", { card_id: "turn-other", seq: 7, delta: "独立" });
    expect(selectNarrativeBuffer(getTurnSnapshot("turn-other"))).toBe("独立");
  });

  it("applied-seq dedupe map is bounded — oldest turns evicted (no unbounded leak)", () => {
    // Turn T0 applies seq 7.
    dispatchToTurnStore("narrative.delta", { card_id: "T0", seq: 7, delta: "X" });
    expect(selectNarrativeBuffer(getTurnSnapshot("T0"))).toBe("X");

    // Start enough *new* turns to push T0 past the eviction cap.
    for (let i = 0; i < _APPLIED_SEQ_MAX_TURNS + 1; i++) {
      dispatchToTurnStore("narrative.delta", { card_id: `evict-${i}`, seq: 1, delta: "." });
    }

    // T0 was evicted → its seq 7 is no longer remembered → re-applied (not
    // deduped). With an unbounded map it would have been skipped → "X".
    dispatchToTurnStore("narrative.delta", { card_id: "T0", seq: 7, delta: "Y" });
    expect(selectNarrativeBuffer(getTurnSnapshot("T0"))).toBe("XY");
  });

  it("O6: 驱逐优先挑已 finalized 的 turn — 活着的老 turn 的去重记忆保留", () => {
    // T-alive is still running (not finalized); T-done is finalized. Both precede later arrivals.
    dispatchToTurnStore("narrative.delta", { card_id: "T-alive", seq: 7, delta: "A" });
    dispatchToTurnStore("step_list_card_created", { card_id: "T-done" });
    dispatchToTurnStore("narrative.delta", { card_id: "T-done", seq: 7, delta: "D" });
    dispatchToTurnStore("step_list_card_finalized", { card_id: "T-done", state: "completed" });

    // Push just enough for one eviction (2 old + cap-1 new → the cap-1-th new turn triggers one eviction;
    // finalized-first must select T-done, not the older T-alive).
    for (let i = 0; i < _APPLIED_SEQ_MAX_TURNS - 1; i++) {
      dispatchToTurnStore("narrative.delta", { card_id: `o6-${i}`, seq: 1, delta: "." });
    }

    // Preserve T-alive's (unfinalized) seq memory → replay seq 7 is deduplicated, not doubled.
    dispatchToTurnStore("narrative.delta", { card_id: "T-alive", seq: 7, delta: "A2" });
    expect(selectNarrativeBuffer(getTurnSnapshot("T-alive"))).toBe("A");
    // Evict finalized T-done first → lose its seq memory → replay reapplies (acceptable:
    // finalized turns receive no more live events; only explicit replay reaches this path).
    dispatchToTurnStore("narrative.delta", { card_id: "T-done", seq: 7, delta: "D2" });
    expect(selectNarrativeBuffer(getTurnSnapshot("T-done"))).toBe("DD2");
  });

  // Phase 1.7-A — final_answer routes through dispatch (does not short-circuit)
  it("final_answer → applyFinalAnswer pushes one `answer` beat; returns false (legacy reducer still owns runner.answer)", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    expect(
      dispatchToTurnStore("final_answer", {
        turn_id: T,
        text: "最终结论",
      }),
    ).toBe(false);
    const payload = selectAnswerPayload(getTurnSnapshot(T));
    expect(payload?.turnId).toBe(T);
    const beats = getTurnSnapshot(T)?.beats ?? [];
    expect(beats.filter((b) => b.kind === "answer").length).toBe(1);
  });

  // Phase 1.7-A — clarification_required (turn-terminating) pushes picker beat
  it("clarification_required with clarification_id → picker beat; returns false (legacy reducer still sets runner.clarificationPending)", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    const claimed = dispatchToTurnStore("clarification_required", {
      turn_id: T,
      clarification_id: "clar-99",
      candidates: ["按月", "按周"],
      questions: [{
        question: "主要看哪个周期?",
        options: ["按月", "按周"],
        type: "single_select",
      }],
      fallback_text: null,
    });
    expect(claimed).toBe(false);
    const beats = getTurnSnapshot(T)?.beats ?? [];
    const picker = beats.find((b) => b.kind === "picker");
    expect(picker?.kind).toBe("picker");
    if (picker?.kind === "picker") {
      expect(picker.clarification.clarificationId).toBe("clar-99");
      expect(picker.clarification.candidates).toEqual(["按月", "按周"]);
      expect(picker.clarification.questions).toEqual([{
        question: "主要看哪个周期?",
        options: ["按月", "按周"],
        type: "single_select",
      }]);
    }
  });

  it("clarification_required WITHOUT clarification_id → no picker beat (V2.7 HITL legacy path)", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    const claimed = dispatchToTurnStore("clarification_required", {
      turn_id: T,
      // no clarification_id → V2.7 HITL flow
      request_id: "v27-1",
      kind: "options",
    });
    expect(claimed).toBe(false);
    const beats = getTurnSnapshot(T)?.beats ?? [];
    expect(beats.filter((b) => b.kind === "picker").length).toBe(0);
  });
});

describe("peer_dispatched → peerProseKey (dogfood s_3e854 ④)", () => {
  it("attaches the PEER id (spawn_id) to the spawn step, NOT the Lead id", () => {
    // the subagent_spawn step_started carries the LEAD's id (the 3-strikes root cause)
    dispatchToTurnStore("step_started", {
      card_id: T, step_id: "sp1", step_type: "subagent",
      tool_name: "subagent_spawn", header_zh: "派 peer", subagent_id: "LEAD_id",
    });
    // peer_dispatched carries the PEER id + card_id (backend fix subagent.py)
    const claimed = dispatchToTurnStore("peer_dispatched", {
      card_id: T, spawn_id: "PEER_id", subagent_id: "PEER_id", peer_name: "compute",
    });
    const spawn = getTurnSnapshot(T)?.steps.find((s) => s.stepId === "sp1");
    // prose key = peer id (== partial_answer.parent_agent_id), so prose matches this row
    expect(spawn?.peerProseKey).toBe("PEER_id");
    // the step's OWN id is the Lead id — keying prose on it never matched (the bug)
    expect(spawn?.subagentId).toBe("LEAD_id");
    // not claimed → handle()'s legacy switch still updates the PeersDispatched tray
    expect(claimed).toBe(false);
  });

  it("falls back to subagent_id when spawn_id absent", () => {
    dispatchToTurnStore("step_started", {
      card_id: T, step_id: "sp1", step_type: "subagent",
      tool_name: "subagent_spawn", header_zh: "x",
    });
    dispatchToTurnStore("peer_dispatched", {
      card_id: T, subagent_id: "PEER_id", peer_name: "compute",
    });
    expect(
      getTurnSnapshot(T)?.steps.find((s) => s.stepId === "sp1")?.peerProseKey,
    ).toBe("PEER_id");
  });

  it("parallel peer dispatch uses parent_step_id instead of arrival order", () => {
    dispatchToTurnStore("step_started", {
      card_id: T, step_id: "sp1", step_type: "subagent",
      tool_name: "subagent_spawn", header_zh: "x",
    });
    dispatchToTurnStore("step_started", {
      card_id: T, step_id: "sp2", step_type: "subagent",
      tool_name: "subagent_spawn", header_zh: "x",
    });

    // Both spawn rows already exist before either peer-dispatched event. The
    // old last-unkeyed heuristic would cross-wire PEER_1 onto sp2.
    dispatchToTurnStore("peer_dispatched", {
      card_id: T,
      spawn_id: "PEER_1",
      parent_step_id: "sp1",
      peer_name: "compute",
    });
    dispatchToTurnStore("peer_dispatched", {
      card_id: T,
      spawn_id: "PEER_2",
      parent_step_id: "sp2",
      peer_name: "financial-analyst",
    });

    const steps = getTurnSnapshot(T)?.steps ?? [];
    expect(steps.find((step) => step.stepId === "sp1")?.peerProseKey).toBe("PEER_1");
    expect(steps.find((step) => step.stepId === "sp2")?.peerProseKey).toBe("PEER_2");
  });

  it("no card_id → no-op, no throw", () => {
    expect(() =>
      dispatchToTurnStore("peer_dispatched", { spawn_id: "PEER_id", peer_name: "compute" }),
    ).not.toThrow();
  });

  // P4a: peer_dispatched (non-plan Lead-tool path) still attaches the prose key,
  // but peer IDENTITY is NOT taken from here — agent_spawned is the single source
  // (it's the only spawn event the plan path emits). Verified live on 116.
  it("peer_dispatched attaches the prose key but does NOT set peerNames", () => {
    dispatchToTurnStore("step_started", {
      card_id: T, step_id: "sp1", step_type: "subagent",
      tool_name: "subagent_spawn", header_zh: "派 peer", subagent_id: "LEAD_id",
    });
    dispatchToTurnStore("peer_dispatched", {
      card_id: T, spawn_id: "PEER_id", peer_name: "financial-analyst",
    });
    expect(getTurnSnapshot(T)?.steps.find((s) => s.stepId === "sp1")?.peerProseKey).toBe("PEER_id");
    expect(getTurnSnapshot(T)?.peerNames).toEqual({}); // identity comes from agent_spawned
  });

  // P4a: the AUTHORITATIVE peer-identity event (verified live on 116). EVERY peer's
  // runner-start emits agent_spawned (subagent_id + profile == agent_name), plan OR
  // non-plan; the plan path emits ONLY this. The runner injects card_id; peer inner
  // steps carry subagentId == this subagent_id, so the block resolves its name.
  it("agent_spawned persists peer identity into peerNames", () => {
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "sub_e82b54962e",
      profile: "business-analyst",
      display_name: "  华东业务专家  ",
      agent_id: "osa_east",
      agent_revision_id: "arv_east_3",
      agent_release_id: "arl_east_3",
      agent_execution_id: "aex_east",
      display_name_snapshot: "华东业务专家",
    });
    expect(getTurnSnapshot(T)?.peerNames).toEqual({ sub_e82b54962e: "business-analyst" });
    expect(getTurnSnapshot(T)?.peerDisplayNames).toEqual({
      sub_e82b54962e: "华东业务专家",
    });
    expect(getTurnSnapshot(T)?.peerAgentAttribution).toEqual({
      sub_e82b54962e: {
        agentId: "osa_east",
        agentRevisionId: "arv_east_3",
        agentReleaseId: "arl_east_3",
        agentExecutionId: "aex_east",
        displayNameSnapshot: "华东业务专家",
      },
    });
  });

  it("additive Agent attribution merges first non-empty across event shapes", () => {
    dispatchToTurnStore("peer_dispatched", {
      card_id: T,
      spawn_id: "s1",
      peer_name: "sales-agent",
      agent_id: "osa_sales",
      agent_revision_id: "arv_sales_1",
      agent_execution_id: "aex_sales",
    });
    dispatchToTurnStore("lead.subagent_summary", {
      card_id: T,
      spawn_id: "s1",
      status: "succeeded",
      agent_id: "osa_conflict",
      agent_release_id: "arl_sales_1",
      display_name_snapshot: "销售专家",
    });
    expect(getTurnSnapshot(T)?.peerAgentAttribution.s1).toEqual({
      agentId: "osa_sales",
      agentRevisionId: "arv_sales_1",
      agentReleaseId: "arl_sales_1",
      agentExecutionId: "aex_sales",
      displayNameSnapshot: "销售专家",
    });
  });

  it("agent_spawned persists the peer task into peerTasks (plan-path block header)", () => {
    dispatchToTurnStore("agent_spawned", {
      card_id: T, subagent_id: "s1", profile: "compute", task: "拆解营收增长驱动",
    });
    expect(getTurnSnapshot(T)?.peerTasks).toEqual({ s1: "拆解营收增长驱动" });
  });

  it("agent_spawned re-emits (spawn/finish/max-iter) stay idempotent", () => {
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s1",
      profile: "custom-agent",
      display_name: "销售专家",
    });
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s1",
      profile: "custom-agent",
      status: "completed",
    });
    // A malformed duplicate start cannot rewrite the persisted first snapshot.
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s1",
      profile: "custom-agent",
      display_name: "改名后的名字",
    });
    expect(getTurnSnapshot(T)?.peerNames).toEqual({ s1: "custom-agent" });
    expect(getTurnSnapshot(T)?.peerDisplayNames).toEqual({ s1: "销售专家" });
    expect(getTurnSnapshot(T)?.peerTerminalById).toEqual({
      s1: { status: "succeeded" },
    });
  });

  it("plan-path agent_spawned terminal shapes stop zero-step peers from running forever", () => {
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s_plan",
      profile: "financial-analyst",
      task: "直接分析财务健康度",
    });
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s_plan",
      profile: "financial-analyst",
      status: "completed",
      duration_ms: 920,
      tool_count: 0,
    });

    expect(getTurnSnapshot(T)?.peerTerminalById).toEqual({
      s_plan: { status: "succeeded", durationMs: 920 },
    });
  });

  it("failed graph and exhausted legacy terminal shapes remain peer-local failures", () => {
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s_graph",
      profile: "compute",
      status: "failed",
      error: "api_key=must-not-render",
    });
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s_legacy",
      profile: "compute",
      status: "max_iterations_exhausted",
      duration_ms: 1200,
    });

    expect(getTurnSnapshot(T)?.peerTerminalById).toEqual({
      s_graph: { status: "failed" },
      s_legacy: { status: "failed", durationMs: 1200 },
    });
  });

  it("blank display_name is omitted while the routing slug remains available", () => {
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s1",
      profile: "custom-agent",
      display_name: " \t ",
    });
    expect(getTurnSnapshot(T)?.peerNames).toEqual({ s1: "custom-agent" });
    expect(getTurnSnapshot(T)?.peerDisplayNames).toEqual({});
  });

  it("rename/delete affects new turns without rewriting an old turn snapshot", () => {
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s1",
      profile: "sales-agent",
      display_name: "旧名称",
    });
    dispatchToTurnStore("agent_spawned", {
      card_id: "t_after_rename",
      subagent_id: "s2",
      profile: "sales-agent",
      display_name: "新名称",
    });
    dispatchToTurnStore("agent_spawned", {
      card_id: "t_after_delete",
      subagent_id: "s3",
      profile: "sales-agent",
    });

    expect(getTurnSnapshot(T)?.peerDisplayNames).toEqual({ s1: "旧名称" });
    expect(getTurnSnapshot("t_after_rename")?.peerDisplayNames).toEqual({
      s2: "新名称",
    });
    expect(getTurnSnapshot("t_after_delete")?.peerDisplayNames).toEqual({});
    expect(getTurnSnapshot("t_after_delete")?.peerNames).toEqual({
      s3: "sales-agent",
    });
  });

  it("agent_spawned for the Lead profile is skipped (Lead stays the narrator)", () => {
    dispatchToTurnStore("agent_spawned", { card_id: T, subagent_id: "lead_id", profile: "lead" });
    dispatchToTurnStore("agent_spawned", { card_id: T, subagent_id: "c1", profile: "compute" });
    expect(getTurnSnapshot(T)?.peerNames).toEqual({ c1: "compute" }); // no lead_id
  });

  it("lead.subagent_summary persists the peer terminal for live and replay rendering", () => {
    dispatchToTurnStore("lead.subagent_summary", {
      card_id: T,
      spawn_id: "s1",
      status: "succeeded_with_warnings",
      duration_ms: 1840,
      failure_reason: "one source unavailable",
      outcome_reason: "one source unavailable after three attempts",
    });

    expect(getTurnSnapshot(T)?.peerTerminalById).toEqual({
      s1: {
        status: "succeeded_with_warnings",
        durationMs: 1840,
      },
    });
  });

  it("a peer stopped at an agent run limit is partial, not failed, live and on replay", () => {
    dispatchToTurnStore("agent_spawned", {
      card_id: T,
      subagent_id: "s1",
      profile: "compute",
      status: "partial",
      stop_reason: "time_limit",
      duration_ms: 3000,
    });
    dispatchToTurnStore("lead.subagent_summary", {
      card_id: T,
      spawn_id: "s1",
      status: "partial",
      stop_reason: "time_limit",
      duration_ms: 3100,
      failure_reason: null,
    });

    expect(getTurnSnapshot(T)?.peerTerminalById.s1).toMatchObject({
      status: "partial",
      stopReason: "time_limit",
      durationMs: 3100,
    });
    expect(getTurnSnapshot(T)?.peerTerminalById.s1?.failureReason).toBeUndefined();
  });

  it("keeps the saved run a Continue click resumes, and why a resume started fresh", () => {
    dispatchToTurnStore("lead.subagent_summary", {
      card_id: T,
      spawn_id: "s1",
      status: "partial",
      stop_reason: "step_limit",
      resume_checkpoint_id: "rsc_0123456789abcdef0123",
      resume_refused: "tools_changed",
    });
    expect(getTurnSnapshot(T)?.peerTerminalById.s1).toMatchObject({
      status: "partial",
      resumeCheckpointId: "rsc_0123456789abcdef0123",
      resumeRefused: "tools_changed",
    });
  });

  it("peer_dispatch_failed keeps only finite localized facts", () => {
    dispatchToTurnStore("narrative.delta", {
      card_id: T,
      delta: "Lead is still synthesizing",
    });
    const narrative = getTurnSnapshot(T)?.beats.find(
      (beat) => beat.kind === "narrative",
    );
    const claimed = dispatchToTurnStore("peer_dispatch_failed", {
      card_id: T,
      spawn_id: "s1",
      peer_name: "compute",
      reason_code: "peer_execution_timeout",
      error_preview: "Authorization: Bearer raw-secret",
      diagnostics: {
        schema_version: 1,
        disclosure: "authenticated",
        diagnostic_code: "chat_peer_failed",
        occurrence_id: "0123456789abcdef0123456789abcdef",
        turn_id: T,
        safe_detail: "phase=peer; failure_class=timeout",
        retryable: true,
      },
    });

    expect(claimed).toBe(false);
    expect(getTurnSnapshot(T)?.peerTerminalById.s1).toEqual({
      status: "failed",
      failureReason: "peer_execution_timeout",
      failureDiagnostics: {
        schemaVersion: 1,
        disclosure: "authenticated",
        diagnosticCode: "chat_peer_failed",
        code: "peer_execution_timeout",
        occurrenceId: "0123456789abcdef0123456789abcdef",
        turnId: T,
        safeDetail: "phase=peer; failure_class=timeout",
        retryable: true,
      },
    });
    expect(JSON.stringify(getTurnSnapshot(T))).not.toContain("raw-secret");
    expect(narrative?.kind).toBe("narrative");
    if (narrative?.kind === "narrative") {
      expect(isNarrativeSegActive(T, narrative.segId)).toBe(true);
    }
  });
});

describe("approval lifecycle → fold guard facts", () => {
  it("approval.required stays pending until approval.executed resolves it", () => {
    dispatchToTurnStore("approval.required", {
      card_id: T,
      approval_id: "ap_1",
      tool_name: "bi_update_measures",
    });
    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({ ap_1: "pending" });

    dispatchToTurnStore("approval.executed", {
      card_id: T,
      approval_id: "ap_1",
      ok: true,
    });
    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({ ap_1: "executed" });
  });

  it("approval execution failure remains an abnormal action outcome", () => {
    dispatchToTurnStore("approval.required", {
      card_id: T,
      approval_id: "ap_2",
    });
    dispatchToTurnStore("approval.executed", {
      card_id: T,
      approval_id: "ap_2",
      ok: false,
    });
    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({
      ap_2: "execution_failed",
    });
  });

  it("late approval.required cannot downgrade an executed terminal status", () => {
    dispatchToTurnStore("approval.executed", {
      card_id: T,
      approval_id: "ap_3",
      ok: true,
      seq: 20,
    });
    dispatchToTurnStore("approval.required", {
      card_id: T,
      approval_id: "ap_3",
      seq: 21,
    });

    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({ ap_3: "executed" });
  });

  it("approval.rejected → terminal rejected status that keeps the turn expanded (R-2)", () => {
    dispatchToTurnStore("approval.required", {
      card_id: T,
      approval_id: "ap_4",
      seq: 30,
    });
    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({ ap_4: "pending" });

    dispatchToTurnStore("approval.rejected", {
      card_id: T,
      approval_id: "ap_4",
      seq: 31,
    });
    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({ ap_4: "rejected" });
    // Rejected = abnormal completion → still blocks the normal-completion fold.
    expect(hasBlockingApprovalState(getTurnSnapshot(T))).toBe(true);
  });

  it("late approval.required cannot downgrade a rejected terminal status", () => {
    dispatchToTurnStore("approval.rejected", {
      card_id: T,
      approval_id: "ap_5",
      seq: 40,
    });
    // A stale/replayed required arriving after the terminal must not resurrect
    // pending and permanently hold the fold gate on the wrong fact.
    dispatchToTurnStore("approval.required", {
      card_id: T,
      approval_id: "ap_5",
      seq: 41,
    });
    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({ ap_5: "rejected" });
  });

  it("approval.rejected applier is idempotent for synthetic replays (seq<1 bypasses seq-dedupe)", () => {
    // seq<1 events are never seq-deduped (§5.1), so the applier's own guard —
    // not shouldSkipAppliedSeq — is what must keep this idempotent.
    dispatchToTurnStore("approval.rejected", { card_id: T, approval_id: "ap_6", seq: -1 });
    dispatchToTurnStore("approval.rejected", { card_id: T, approval_id: "ap_6", seq: -1 });
    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({ ap_6: "rejected" });
  });

  it("a stale approval.executed cannot un-reject a rejected terminal (monotonic)", () => {
    dispatchToTurnStore("approval.rejected", { card_id: T, approval_id: "ap_7", seq: 60 });
    // A late/replayed execution event for the same approval must NOT flip
    // rejected→executed (which would wrongly let the turn auto-collapse).
    dispatchToTurnStore("approval.executed", { card_id: T, approval_id: "ap_7", ok: true, seq: 61 });
    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({ ap_7: "rejected" });
    expect(hasBlockingApprovalState(getTurnSnapshot(T))).toBe(true);
  });

  it("a stale approval.rejected cannot clobber an executed terminal (monotonic)", () => {
    dispatchToTurnStore("approval.executed", { card_id: T, approval_id: "ap_8", ok: true, seq: 70 });
    dispatchToTurnStore("approval.rejected", { card_id: T, approval_id: "ap_8", seq: 71 });
    expect(getTurnSnapshot(T)?.approvalStatusById).toEqual({ ap_8: "executed" });
  });

  it("fold-equivalence: pending and rejected both block the fold; executed does not", () => {
    // This equivalence is why the reject path's live-vs-replay delivery gap is
    // invisible: a terminal turn's live tab never receives the out-of-band
    // approval.rejected (closed bus), so it keeps the persisted "pending" until
    // F5 flips it to "rejected" — and since both block, the disclosure stays
    // expanded identically. If "rejected" ever became non-blocking, that gap
    // would turn into a visible live≠F5 regression; this locks against it.
    dispatchToTurnStore("approval.required", { card_id: T, approval_id: "ap_9", seq: 80 });
    expect(hasBlockingApprovalState(getTurnSnapshot(T))).toBe(true); // pending

    dispatchToTurnStore("approval.rejected", { card_id: T, approval_id: "ap_9", seq: 81 });
    expect(hasBlockingApprovalState(getTurnSnapshot(T))).toBe(true); // rejected

    dispatchToTurnStore("approval.executed", { card_id: T, approval_id: "ap_10", ok: true, seq: 82 });
    // ap_9 rejected still blocks; ap_10 executed alone would not.
    expect(hasBlockingApprovalState(getTurnSnapshot(T))).toBe(true);
  });
});

describe("partial_answer → peerProse (dogfood s_3e854 ④ — P0 F5/replay fix)", () => {
  it("peer partial_answer accumulates into the turn-store keyed by parent_agent_id", () => {
    dispatchToTurnStore("partial_answer", { card_id: T, delta: "好，", parent_agent_id: "PEER_id" });
    dispatchToTurnStore("partial_answer", { card_id: T, delta: "schema 确认", parent_agent_id: "PEER_id" });
    expect(getTurnSnapshot(T)?.peerProse["PEER_id"]).toBe("好，schema 确认");
  });

  it("Lead-scope partial_answer (no parent_agent_id) is NOT claimed → returns false", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    const claimed = dispatchToTurnStore("partial_answer", { card_id: T, delta: "Lead answer" });
    expect(claimed).toBe(false);
    expect(getTurnSnapshot(T)?.peerProse).toEqual({});
  });

  it("FULL replay: spawn + peer_dispatched + partial_answer → spawn row's peerProseKey resolves to its prose", () => {
    // This is the F5/replay scenario the P0 review found broken: replayTurnEvents feeds
    // ALL events through dispatch, so BOTH the key (peer_dispatched) AND the prose
    // (partial_answer) must rebuild in the one turn-store the render reads.
    dispatchToTurnStore("step_started", {
      card_id: T, step_id: "sp1", step_type: "subagent",
      tool_name: "subagent_spawn", header_zh: "派 peer", subagent_id: "LEAD_id",
    });
    dispatchToTurnStore("peer_dispatched", { card_id: T, spawn_id: "PEER_id", peer_name: "compute" });
    dispatchToTurnStore("partial_answer", { card_id: T, delta: "正在分析…", parent_agent_id: "PEER_id" });
    const s = getTurnSnapshot(T);
    const spawn = s?.steps.find((x) => x.stepId === "sp1");
    expect(spawn?.peerProseKey).toBe("PEER_id");
    // the render computes liveProse = turnStore.peerProse[spawn.peerProseKey] — non-empty here
    expect(s?.peerProse[spawn!.peerProseKey!]).toBe("正在分析…");
  });
});

describe("done.narrative_superseded persists through the shared turn store", () => {
  // Refresh rebuilds the shared store through turn-store-replay and dispatchToTurnStore.
  // A marker kept only in runner state would disappear and expose superseded text again.
  //
  // These tests target dispatchToTurnStore, the **shared route for live SSE and F5 replay**; passing means both states share a source.
  // This is the actual guard for CLAUDE.md's live / persisted / replayed consistency invariant.

  it("done 带 narrative_superseded → 写进 turn-store", () => {
    dispatchToTurnStore("done", { turn_id: "t-x", status: "completed", narrative_superseded: true });
    expect(getTurnSnapshot("t-x")?.narrativeSuperseded).toBe(true);
  });

  it("done 不带该键 → 保持 false（老 turn / 未触发，wire 形状不变）", () => {
    dispatchToTurnStore("done", { turn_id: "t-y", status: "completed" });
    expect(getTurnSnapshot("t-y")?.narrativeSuperseded ?? false).toBe(false);
  });

  it("重放同一条 done 幂等（F5 可能重复喂同一事件）", () => {
    const ev = { turn_id: "t-z", status: "completed", narrative_superseded: true };
    dispatchToTurnStore("done", ev);
    dispatchToTurnStore("done", ev);
    expect(getTurnSnapshot("t-z")?.narrativeSuperseded).toBe(true);
  });

  it("非 done 的终态事件不误置（只有 done 携带该判定）", () => {
    dispatchToTurnStore("turn_failed", { turn_id: "t-w", narrative_superseded: true });
    expect(getTurnSnapshot("t-w")?.narrativeSuperseded ?? false).toBe(false);
  });
});

/**
 * chat-ux-2026-08 — event order (`evt_seq`) enters the store.
 *
 * Its sole purpose is letting the running band place **steps** and **peer terminal states** in one history;
 * previously neither side had any common ordering basis.
 * ⚠ Keep each mutation documented below: these assertions look like field passthrough checks,
 * and only the mutation records explain what they guard against.
 */
describe("evt_seq → 步骤与 peer 终态的共同顺序依据", () => {
  const E = "t-evtseq";

  it("step_started 的 evt_seq 落到 Step 上", () => {
    // Mutation: omit `evtSeq: readEvtSeq(payload)` in `applyStepStarted` → must fail.
    dispatchToTurnStore("step_list_card_created", { card_id: E, seq: 1 });
    dispatchToTurnStore("step_started", {
      card_id: E, step_id: "s1", step_type: "tool_run",
      tool_name: "bi_query", header_zh: "查询数据", evt_seq: 42, seq: 2,
    });
    expect(getTurnSnapshot(E)?.steps[0]?.evtSeq).toBe(42);
  });

  it("没有 evt_seq 时回落到 seq；两个都没有则为 null", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: E });
    dispatchToTurnStore("step_started", {
      card_id: E, step_id: "s-seq", step_type: "tool_run",
      tool_name: "bi_query", header_zh: "查询数据", seq: 7,
    });
    dispatchToTurnStore("step_started", {
      card_id: E, step_id: "s-none", step_type: "tool_run",
      tool_name: "bi_query", header_zh: "查询数据",
    });
    const steps = getTurnSnapshot(E)!.steps;
    expect(steps.find((s) => s.stepId === "s-seq")?.evtSeq).toBe(7);
    expect(steps.find((s) => s.stepId === "s-none")?.evtSeq).toBeNull();
  });

  it("peer 终态的 evt_seq 落到 PeerTerminal 上", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: E });
    dispatchToTurnStore("lead.subagent_summary", {
      card_id: E, turn_id: E, subagent_id: "p1", status: "failed",
      failure_reason: "peer_execution_failed", evt_seq: 31, seq: 3,
    });
    expect(getTurnSnapshot(E)?.peerTerminalById.p1?.evtSeq).toBe(31);
  });

  it("★ 同一个 peer 被写两遍时，evtSeq 取**最早**那次", () => {
    // Production writes twice: `agent_spawned` first records state, then `lead.subagent_summary` fills
    // reason/diagnostics. Using the later sequence moves this terminal entry to the band's history tail:
    // exactly the error this ordering prevents.
    // Mutation: change `applyPeerTerminal` to `evtSeq: input.evtSeq ?? prior?.evtSeq` → must fail.
    dispatchToTurnStore("step_list_card_created", { card_id: E });
    dispatchToTurnStore("agent_spawned", {
      card_id: E, subagent_id: "p2", profile: "compute",
      status: "failed", evt_seq: 5, seq: 5,
    });
    expect(getTurnSnapshot(E)?.peerTerminalById.p2?.evtSeq).toBe(5);
    dispatchToTurnStore("lead.subagent_summary", {
      card_id: E, turn_id: E, subagent_id: "p2", status: "failed",
      failure_reason: "peer_execution_failed", evt_seq: 90, seq: 90,
    });
    const t = getTurnSnapshot(E)!.peerTerminalById.p2!;
    expect(t.evtSeq).toBe(5);              // Sequence stays unchanged.
    expect(t.failureReason).toBe("peer_execution_failed");  // Additional fields still update normally.
  });

  it("★ 首次落库没有序、后一次才带序时，序必须补得进去", () => {
    // Positive control for `prior.evtSeq === next.evtSeq` in `applyPeerTerminal`.
    // Independent review called it a tautology whose removal would not fail any test; that is incorrect:
    // when prior exists **without** evtSeq (bypass-bus events have no sequence) and a later update supplies one,
    // they differ; this condition prevents the nothing-changed early return from dropping the newly arrived sequence.
    // Mutation: delete the `prior.evtSeq === next.evtSeq` line → this case must fail.
    dispatchToTurnStore("step_list_card_created", { card_id: E });
    // First update: no evt_seq / seq (bypass-bus).
    dispatchToTurnStore("peer_dispatch_failed", {
      card_id: E, turn_id: E, spawn_id: "p3", reason_code: "peer_execution_failed",
    });
    expect(getTurnSnapshot(E)?.peerTerminalById.p3?.evtSeq).toBeUndefined();
    // Second update: the same facts, now with a sequence; no other field changes.
    dispatchToTurnStore("peer_dispatch_failed", {
      card_id: E, turn_id: E, spawn_id: "p3", reason_code: "peer_execution_failed",
      evt_seq: 12,
    });
    expect(getTurnSnapshot(E)?.peerTerminalById.p3?.evtSeq).toBe(12);
  });

  it("★ 重复的 step_started 保留最早的序（重连补发不该把它挪到后面）", () => {
    // Mutation: `evtSeq: input.evtSeq ?? null` (ignore prior) → must fail.
    dispatchToTurnStore("step_list_card_created", { card_id: E });
    dispatchToTurnStore("step_started", {
      card_id: E, step_id: "dup", step_type: "tool_run",
      tool_name: "bi_query", header_zh: "查询数据", evt_seq: 4, seq: 4,
    });
    dispatchToTurnStore("step_started", {
      card_id: E, step_id: "dup", step_type: "tool_run",
      tool_name: "bi_query", header_zh: "查询数据", evt_seq: 80, seq: 80,
    });
    expect(getTurnSnapshot(E)?.steps.find((s) => s.stepId === "dup")?.evtSeq).toBe(4);
  });
});

describe("@Agent continuation reuses one grouped Agent block", () => {
  const C = "t-continuation";
  const sid = "sub_primary";

  function events(secondOk: boolean): Array<[string, Record<string, unknown>]> {
    return [
      [
        "agent_spawned",
        { card_id: C, subagent_id: sid, profile: "coach", event_seq: 1 },
      ],
      [
        "agent_spawned",
        {
          card_id: C,
          subagent_id: sid,
          profile: "coach",
          status: "completed",
          event_seq: 2,
        },
      ],
      [
        "lead.subagent_summary",
        {
          card_id: C,
          subagent_id: sid,
          status: "succeeded",
          seq: 3,
          event_seq: 3,
        },
      ],
      [
        "agent_spawned",
        { card_id: C, subagent_id: sid, profile: "coach", event_seq: 4 },
      ],
      [
        "agent_spawned",
        {
          card_id: C,
          subagent_id: sid,
          profile: "coach",
          status: secondOk ? "completed" : "failed",
          event_seq: 5,
        },
      ],
      [
        "lead.subagent_summary",
        secondOk
          ? {
              card_id: C,
              subagent_id: sid,
              status: "succeeded",
              seq: 6,
              event_seq: 6,
            }
          : {
              card_id: C,
              subagent_id: sid,
              status: "failed",
              failure_reason: "peer_empty_result",
              seq: 6,
              event_seq: 6,
            },
      ],
    ];
  }

  it("runs again while the continuation runs, then settles", () => {
    const all = events(true);
    for (const [type, payload] of all.slice(0, 4))
      dispatchToTurnStore(type, payload);
    expect(getTurnSnapshot(C)?.peerTerminalById[sid]?.rerunning).toBe(true);
    for (const [type, payload] of all.slice(4))
      dispatchToTurnStore(type, payload);
    const terminal = getTurnSnapshot(C)?.peerTerminalById[sid];
    expect(terminal?.rerunning).toBeFalsy();
    expect(terminal?.status).toBe("succeeded");
    expect(terminal?.evtSeq).toBe(2);
  });

  it("a failed continuation keeps the completed Agent and records its failure secondarily", () => {
    for (const [type, payload] of events(false))
      dispatchToTurnStore(type, payload);
    const terminal = getTurnSnapshot(C)?.peerTerminalById[sid];
    expect(terminal?.status).toBe("succeeded");
    expect(terminal?.failureReason).toBeUndefined();
    expect(terminal?.continuationFailureReason).toBe("peer_empty_result");
  });

  it("an ordinary peer whose only run fails after completing is still failed", () => {
    for (const [type, payload] of events(false).slice(0, 2))
      dispatchToTurnStore(type, payload);
    dispatchToTurnStore("lead.subagent_summary", {
      card_id: C,
      subagent_id: sid,
      status: "failed",
      failure_reason: "peer_empty_result",
      seq: 3,
      event_seq: 3,
    });
    expect(getTurnSnapshot(C)?.peerTerminalById[sid]?.status).toBe("failed");
  });

  it("replay and reconnect redelivery give the same terminal as live", () => {
    for (const [type, payload] of events(false))
      dispatchToTurnStore(type, payload);
    const live = getTurnSnapshot(C)?.peerTerminalById[sid];
    // Reconnect redelivers the earlier runner start after the turn settled.
    dispatchToTurnStore("agent_spawned", {
      card_id: C,
      subagent_id: sid,
      profile: "coach",
      event_seq: 4,
    });
    expect(getTurnSnapshot(C)?.peerTerminalById[sid]).toEqual(live);
    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    for (const [type, payload] of events(false))
      dispatchToTurnStore(type, payload);
    expect(getTurnSnapshot(C)?.peerTerminalById[sid]).toEqual(live);
  });
});
