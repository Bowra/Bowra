/**
 * 0.39.8 #5 — TurnContainer narrative-vs-answer dedup on hydrate.
 *
 * Refresh can restore duplicated narrative and final-answer text:
 * the chat bubble showed the same 4-paragraph prose 3 times:
 *   1) narrative beat 0 (KB intro, "Tier1" no space)
 *   2) narrative beat 1 (rest of prose, "Tier1" no space)
 *   3) ChatBubble (final_answer text, "Tier 1" with space + [cite:N])
 *
 * narrative.delta got split into 2 beats by a step_started midway. Then
 * final_answer carried a near-duplicate of narrative beat 1. F5 hydrate
 * replayed all 3 → user saw 3-way duplicate.
 *
 * Fix in TurnContainer.tsx — when an answer beat exists AND not streaming,
 * suppress narrative beats whose first ~80 chars overlap with the answer
 * text (normalized: drop ** bold markers + citation markers + whitespace).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyFinalAnswer,
  applyNarrativeAppend,
  applyNarrativeSuperseded,
} from "@/lib/turn-store";
import type { TurnRunnerState } from "@/lib/types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

import { TurnContainer } from "./TurnContainer";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
});

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
  return {
    user: null,
    intent: null,
    complexity: null,
    chips: [],
    answer: "",
    artifact: null,
    source: null,
    summary: null,
    traceSteps: [],
    turnId: null,
    sessionId: null,
    sessionTitle: null,
    running: false,
    started: false,
    messages: [],
    userAttachments: [],
    thinking: "",
    thinkingLength: 0,
    lastThinkingHintAttachedAt: "",
    clarifications: [],
    createToolOpen: null,
    subagentCreatorOpen: null,
    solidifyOpen: null,
    plan: null,
    ...over,
  };
}

describe("TurnContainer · narrative-vs-answer dedup (0.39.8 #5)", () => {
  beforeEach(() => _resetAllTurnsForTesting());

  it("keeps rejected protocol hidden while the original question resumes", () => {
    applyCardCreated({ turnId: "t-resume-superseded" });
    applyNarrativeAppend({ turnId: "t-resume-superseded", text: "Rejected clarification protocol", segId: "before-question" });
    applyNarrativeSuperseded("t-resume-superseded");
    const { container, rerender } = render(
      <TurnContainer runner={runner({ turnId: "t-resume-superseded", user: "Original task", running: false })} />,
    );
    expect(container.textContent).not.toContain("Rejected clarification protocol");
    rerender(<TurnContainer runner={runner({ turnId: "t-resume-superseded", user: "Original task", running: true })} />);
    expect(container.textContent).not.toContain("Rejected clarification protocol");
    expect(container.textContent).toContain("Original task");
  });

  it("hides a narrative that repeats the answer around internal markers", () => {
    // 116, 2026-10-06: the Lead streamed its whole answer as narrative with internal markers between
    // paragraphs; final_answer had the markers removed. The markers are hidden at render time, so the
    // user saw the same prose twice while the raw-text overlap probe missed it.
    const narrative =
      "已记住：以后给你汇报 GMV时，都会同时给出环比和同比。\n\n<response_kind>chat</response_kind>\n\n" +
      "<headline>已记住：GMV汇报同时给环比与同比</headline>\n\n已存为长期记忆，可在设置里查看或修改。\n\n" +
      "补一句提醒：刚才那份华东区分析只给了环比，没有同比。";
    const answer =
      "已记住：以后给你汇报 GMV 时，都会同时给出环比和同比。\n\n\n\n\n\n已存为长期记忆，可在设置里查看或修改。\n\n" +
      "补一句提醒：刚才那份华东区分析只给了环比，没有同比。";
    applyCardCreated({ turnId: "t-markers" });
    applyNarrativeAppend({ turnId: "t-markers", text: narrative, segId: "seg_all" });
    applyFinalAnswer({ turnId: "t-markers", text: answer, payload: { turnId: "t-markers" } });
    const { container } = render(
      <TurnContainer runner={runner({ turnId: "t-markers", user: "记住", running: false, answer })} />,
    );
    const text = container.textContent ?? "";
    expect(text.split("补一句提醒").length - 1).toBe(1);
    expect(text).not.toContain("GMV汇报同时给环比与同比");
  });

  it("keeps a distinct narrative visible when markers are present", () => {
    applyCardCreated({ turnId: "t-markers-distinct" });
    applyNarrativeAppend({
      turnId: "t-markers-distinct",
      text: "<response_kind>chat</response_kind>我先确认「华东区」在本工作区的对应口径，再取数。",
      segId: "seg_intro",
    });
    applyFinalAnswer({
      turnId: "t-markers-distinct",
      text: "2026年9月华东区 GMV 为 5.32 亿元，环比增长 18.06%。",
      payload: { turnId: "t-markers-distinct" },
    });
    const { container } = render(
      <TurnContainer
        runner={runner({ turnId: "t-markers-distinct", user: "华东区GMV", running: false, answer: "2026年9月华东区 GMV 为 5.32 亿元，环比增长 18.06%。" })}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("我先确认「华东区」");
    expect(text).toContain("5.32 亿元");
  });

  it.each(["kbf", "kbo"])("keeps distinct %s receipt identities visible", (prefix) => {
    const turnId = `t-receipt-${prefix}`;
    const original = `Receipt ${prefix}_111122223333 is available.`;
    const answer = `Receipt ${prefix}_aaaabbbbcccc is available.`;
    applyCardCreated({ turnId });
    applyNarrativeAppend({ turnId, text: original, segId: "original-receipt" });
    applyFinalAnswer({ turnId, text: answer, payload: { turnId } });
    const { container } = render(<TurnContainer runner={runner({ turnId, user: "Check the receipt", answer })} />);
    expect(container.textContent).toContain(original);
    expect(container.textContent).toContain(answer);
  });

  it("post-stream: hides narrative beat that overlaps with answer text", () => {
    applyCardCreated({ turnId: "t-5" });
    // narrative beat 0 — KB intro (unique to narrative, not in answer)
    applyNarrativeAppend({
      turnId: "t-5",
      text: "KB里没有关于 tier1 的定义文档。不过从工作区的维度来看，有一个 医院分级-综合分级 字段，让我看看。",
      segId: "seg_kb",
    });
    // narrative beat 1 — rest of prose, contains "从数据来看..." (will be duplicated by answer)
    applyNarrativeAppend({
      turnId: "t-5",
      text: "从数据来看，「医院分级-综合分级」和「医院等级」这两个维度的值都是 **Tier1 / Tier2 / Tier3** 这样的分级标签。",
      segId: "seg_body",
    });
    // answer beat — same prose as narrative beat 1 but reformatted
    applyFinalAnswer({
      turnId: "t-5",
      text: "从数据来看，「医院分级-综合分级」和「医院等级」这两个维度的值都是 Tier 1 / Tier 2 / Tier 3 这样的分级标签。",
      payload: { turnId: "t-5" },
    });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-5",
          user: "tier1 是什么",
          running: false,  // POST-stream
          answer: "从数据来看，「医院分级-综合分级」和「医院等级」这两个维度的值都是 Tier 1 / Tier 2 / Tier 3 这样的分级标签。",
        })}
      />,
    );
    const text = container.textContent ?? "";
    // KB intro narrative beat 0 SHOULD still render (unique content)
    expect(text).toContain("KB里没有关于 tier1");
    // narrative beat 1 ("从数据来看…") is duplicate of answer → should appear
    // only ONCE (via ChatBubble). Count occurrences.
    const phrase = "从数据来看";
    const matches = text.match(/从数据来看/g) ?? [];
    expect(matches.length).toBe(1);  // NOT 2 (was the bug)
  });

  it("live stream: does NOT suppress narrative (user wants to see typing)", () => {
    applyCardCreated({ turnId: "t-5b" });
    applyNarrativeAppend({
      turnId: "t-5b",
      text: "从数据来看 Tier1 是公司内部综合分级",
      segId: "seg_live",
    });
    applyFinalAnswer({
      turnId: "t-5b",
      text: "从数据来看 Tier1 是公司内部综合分级",
      payload: { turnId: "t-5b" },
    });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-5b",
          user: "x",
          running: true,  // STILL streaming
          answer: "从数据来看 Tier1 是公司内部综合分级",
        })}
      />,
    );
    // During live stream both narrative + answer render — typing flow preserved
    const text = container.textContent ?? "";
    const matches = text.match(/从数据来看/g) ?? [];
    expect(matches.length).toBeGreaterThanOrEqual(2);
  });

  it("post-stream + no answer beat: narrative renders normally", () => {
    applyCardCreated({ turnId: "t-5c" });
    applyNarrativeAppend({
      turnId: "t-5c",
      text: "短答案 just the narrative path",
      segId: "seg_only",
    });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-5c",
          user: "q",
          running: false,
          answer: "",
        })}
      />,
    );
    expect(container.textContent).toContain("just the narrative path");
  });

  it("citation marker reformat ('Tier1' → 'Tier 1' + [cite:N]) still detects dup", () => {
    applyCardCreated({ turnId: "t-5d" });
    // Both narrative + answer must clear the 50-char threshold.
    applyNarrativeAppend({
      turnId: "t-5d",
      text: "从数据来看，「医院分级-综合分级」和「医院等级」这两个维度的值都是 **Tier1 / Tier2 / Tier3** 分级标签 [data:art_xxx]",
      segId: "seg_pre_reform",
    });
    applyFinalAnswer({
      turnId: "t-5d",
      text: "从数据来看，「医院分级-综合分级」和「医院等级」这两个维度的值都是 Tier 1 / Tier 2 / Tier 3 分级标签 [cite:1]",
      payload: { turnId: "t-5d" },
    });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-5d",
          user: "q",
          running: false,
          answer: "从数据来看，「医院分级-综合分级」和「医院等级」这两个维度的值都是 Tier 1 / Tier 2 / Tier 3 分级标签 [cite:1]",
        })}
      />,
    );
    const text = container.textContent ?? "";
    const matches = text.match(/从数据来看/g) ?? [];
    expect(matches.length).toBe(1);
  });

  it("0.40.1 #1: short Chinese narrative IDENTICAL to answer is suppressed", () => {
    // Pre-0.40.1: 50-char floor blocked dedup for 34-char Chinese prose like
    // "报告已生成，2025年销售达成率与拜访次数的关联分析可在画布中查看。" —
    // Prefix matching applies regardless of the earlier narrative split.
    // of length, so this 34-char Chinese narrative + identical answer
    // renders only once (via ChatBubble).
    applyCardCreated({ turnId: "t-401-1" });
    applyNarrativeAppend({
      turnId: "t-401-1",
      text: "报告已生成，2025年销售达成率与拜访次数的关联分析可在画布中查看。",
      segId: "seg_short_zh",
    });
    applyFinalAnswer({
      turnId: "t-401-1",
      text: "报告已生成，2025年销售达成率与拜访次数的关联分析可在画布中查看。",
      payload: { turnId: "t-401-1" },
    });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-401-1",
          user: "q",
          running: false,
          answer: "报告已生成，2025年销售达成率与拜访次数的关联分析可在画布中查看。",
        })}
      />,
    );
    const text = container.textContent ?? "";
    const matches = text.match(/报告已生成/g) ?? [];
    expect(matches.length).toBe(1);
  });

  it("chat-solidify #2: narrative ⊇ answer (Lead-self 'intro + answer') is suppressed", () => {
    // Real dogfood (0.72.8, no-data/chit-chat turn): the Lead streams its whole
    // prose as narrative — an intro ("好的，我来看看…") FOLLOWED BY the answer —
    // while final_answer carries ONLY the answer tail. The answer's prefix lives
    // INSIDE the narrative, so the old forward-only check (narrative ⊆ answer)
    // missed it and the user saw the answer twice. The new reverse check
    // (narrative ⊇ answer via t.includes(aProbe)) catches it.
    _resetAllTurnsForTesting();
    applyCardCreated({ turnId: "t-cs2" });
    applyNarrativeAppend({
      turnId: "t-cs2",
      text: "好的，我来看看知识库里有什么。知识库里目前有以下资料：《商超零售分析》《门店运营手册》两份文档，你可以直接查阅。",
      segId: "seg_intro_answer",
    });
    applyFinalAnswer({
      turnId: "t-cs2",
      text: "知识库里目前有以下资料：《商超零售分析》《门店运营手册》两份文档，你可以直接查阅。",
      payload: { turnId: "t-cs2" },
    });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-cs2",
          user: "知识库里有哪些资料？帮我查一下",
          running: false,
          answer: "知识库里目前有以下资料：《商超零售分析》《门店运营手册》两份文档，你可以直接查阅。",
        })}
      />,
    );
    const text = container.textContent ?? "";
    // The shared answer phrase appears once (ChatBubble); narrative suppressed.
    const matches = text.match(/以下资料/g) ?? [];
    expect(matches.length).toBe(1);
  });

  it("0.40.1 #2: dedup fires even when answer beat absent (pre-card_id turns)", () => {
    // Pre-0.40.1 backend emitted final_answer without card_id → FE dispatcher
    // early-returned → no answer beat created. The dedup gating on
    // beatAnswerExists meant narrative dup was NEVER suppressed for those
    // turns. Post-0.40.1 we drop that gate and dedup against runner.answer
    // directly (the fallback ChatBubbleNew at TurnContainer:740 also renders
    // it whenever beat is missing). Verifies the legacy-data path.
    applyCardCreated({ turnId: "t-401-2" });
    applyNarrativeAppend({
      turnId: "t-401-2",
      text: "报告已生成，分析可在画布中查看。",
      segId: "seg_legacy",
    });
    // Deliberately do NOT call applyFinalAnswer (simulates no card_id case).
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-401-2",
          user: "q",
          running: false,
          answer: "报告已生成，分析可在画布中查看。",
        })}
      />,
    );
    const text = container.textContent ?? "";
    const matches = text.match(/报告已生成/g) ?? [];
    // 1 match expected — the fallback ChatBubbleNew at line 740 renders
    // runner.answer once; the narrative beat is now suppressed.
    expect(matches.length).toBe(1);
  });

  it("short narrative with unique prose NOT suppressed (no prefix match)", () => {
    applyCardCreated({ turnId: "t-5e" });
    applyNarrativeAppend({
      turnId: "t-5e",
      text: "短句独立内容",
      segId: "seg_short",
    });
    applyFinalAnswer({
      turnId: "t-5e",
      text: "其他完全不一样的答案文本: " + "x".repeat(100),
      payload: { turnId: "t-5e" },
    });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-5e",
          user: "q",
          running: false,
          answer: "其他完全不一样的答案文本: " + "x".repeat(100),
        })}
      />,
    );
    // Narrative content doesn't appear as a prefix in answer → keep it.
    expect(container.textContent).toContain("短句独立内容");
  });
});

describe("TurnContainer suppresses explicitly superseded narratives", () => {
  beforeEach(() => _resetAllTurnsForTesting());

  // Synthetic narrative and final-answer values conflict without sharing a long text prefix.
  // Only the explicit superseded marker should suppress the earlier claim.
  const NARRATIVE_WRONG = "The preliminary calculation gives **slope b = 12.345 units/month** and **R² = 0.950**.";
  const ANSWER_GROUNDED = "The verified regression across the sample period gives slope b = 23.456 units/month and R² = 0.800.";

  function renderTurn(over: Partial<TurnRunnerState>, superseded = false) {
    applyCardCreated({ turnId: "t-sup" });
    applyNarrativeAppend({ turnId: "t-sup", text: NARRATIVE_WRONG, segId: "seg_narr" });
    applyFinalAnswer({
      turnId: "t-sup",
      text: ANSWER_GROUNDED,
      payload: { turnId: "t-sup" },
    });
    // Condition comes from turn-store (shared live/archived/F5), not runner state.
    if (superseded) applyNarrativeSuperseded("t-sup");
    return render(
      <TurnContainer
        runner={runner({
          turnId: "t-sup",
          user: "帮我拟合回归",
          running: false,
          answer: ANSWER_GROUNDED,
          ...over,
        })}
      />,
    );
  }

  it("narrativeSuperseded=true → 压掉造假的 narrative，只留有据答案", () => {
    const { container } = renderTurn({}, true);
    const text = container.textContent ?? "";
    expect(text).toContain("23.456");
    // Guard this regression: wrong numbers must not appear alongside correct ones.
    expect(text).not.toContain("12.345");
    expect(text).not.toContain("0.950");
  });

  it("未标记时保持原样（相似度判据够不着，两段都在）——证明压制确实来自新标记", () => {
    const { container } = renderTurn({});
    const text = container.textContent ?? "";
    expect(text).toContain("23.456");
    expect(text).toContain("12.345");
  });

  it("keeps known incorrect values hidden while execution is still running", () => {
    const { container } = renderTurn({ running: true }, true);
    expect(container.textContent ?? "").not.toContain("12.345");
    expect(container.textContent ?? "").toContain("23.456");
  });
});
