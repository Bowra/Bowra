/**
 * 1e running band · wiring guards (`docs/sprints/chat-interaction-ux-2026-08-07/运行态披露面-不变量清点.md` §5).
 *
 * Each guard here covers a defect **caught in review but missed by all-green local tests** across three implementations.
 * Internal behavior (throttling / timing / three-row structure) lives in `step-list/RunningBand.test.tsx`;
 * this file covers only the boundary between the band and existing disclosure surfaces, unresolved in all three iterations.
 *
 * Each test comment records its corresponding mutation. Keep these records: the assertions all look like
 * rendering checks, and only the mutation records explain which regression each blocks.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyNarrativeAppend,
  applyPeerName,
  applyPeerProse,
  applyPeerTerminal,
  applyStepDone,
  applyStepStarted,
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

function startStep(turnId: string, stepId: string, subagentId?: string): void {
  applyStepStarted({
    turnId,
    stepId,
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询数据",
    startedAt: new Date(),
    ...(subagentId ? { subagentId } : {}),
  });
}

describe("1e 带 · 与既有披露面的边界", () => {
  beforeEach(() => _resetAllTurnsForTesting());

  it("★ 带不重建 —— Lead 步骤组结束 + peer 接手，带是**同一个 DOM 节点**", () => {
    // On 116, `整条塌成 Bowhead ✓ 三秒` happened because disclosure followed the agent block,
    // rebuilding the entire block when Lead handed off to a peer. Avoiding remounts is one purpose of the band.
    // ⚠ Assert node identity with `toBe`, not content with `toEqual`; the latter also passes after a rebuild.
    // Mutation: also put the band in BeatStream's no-beat early return (two JSX locations)
    //       → React unmounts and rebuilds on the first beat, so this test must fail.
    const turnId = "t-band-no-remount";
    applyCardCreated({ turnId });

    // Start in the **opening gap** (zero beats), the actual starting point of that turn on 116,
    // and the transition that rebuilt the band when the prior version used two JSX locations (beats 0 → 1).
    const { container, rerender } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    const before = container.querySelector('[data-testid="running-band"]');
    expect(before).toBeTruthy();
    expect(before?.getAttribute("data-phase")).toBe("preparing");

    // The first step arrives: beats change from 0 to 1.
    startStep(turnId, "s1");
    rerender(<TurnContainer runner={runner({ turnId, user: "查询", running: true })} />);
    expect(container.querySelector('[data-testid="running-band"]')).toBe(before);

    // Lead's step finishes; the peer takes over and starts its own step.
    applyStepDone({ turnId, stepId: "s1", state: "done", durationMs: 900 });
    applyPeerName(turnId, "peer_1", "compute", "拆解营收", "数据专家");
    startStep(turnId, "s2", "peer_1");
    rerender(<TurnContainer runner={runner({ turnId, user: "查询", running: true })} />);

    const after = container.querySelector('[data-testid="running-band"]');
    expect(after).toBe(before);
  });

  it("★ peer 中途硬挂且**没有散文**时，终态披露不能消失", () => {
    // Invariant history: the third iteration made `traceEnabled` depend only on peer prose, so
    // turns where a peer failed before producing prose lost outcomeReason / footnotes / provenance footer entirely.
    //
    // ⚠ **The containing surface changed (③)**; capability did not (`docs/单元测试规范.md`: `能力搬家 ≠ 能力丢失`).
    //   With the running ledger entirely disabled, the **band** carries terminal disclosure plus one-click access to the full reason:
    //     ① The band shows this peer's red row (from ①) and a persistent failure count;
    //     ② Click `展开运行过程` → the full Chinese `outcomeReason` returns.
    //   Guard ② **must** be asserted: asserting only ① lets a broken expansion target pass all tests,
    //   although losing access to the reason is the regression this invariant originally prevented.
    // Mutation: remove peerTerminalById from RunningBand → ① fails;
    //       remove peerTerminalById from the inline `TurnTraceRegion` → ② fails.
    const turnId = "t-band-peer-terminal";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");
    applyPeerName(turnId, "peer_1", "compute", "拆解营收", "数据专家");
    applyPeerTerminal({
      turnId,
      peerId: "peer_1",
      status: "failed",
      failureReason: "peer_execution_failed",
      durationMs: 1200,
    });

    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );

    // ① The band exists and prominently shows this peer's terminal state (red row + persistent count).
    const band = container.querySelector('[data-testid="running-band"]')!;
    expect(band).toBeTruthy();
    expect(band.querySelector('[data-row-kind="peer"].is-fail')).toBeTruthy();
    expect(band.querySelector('[data-testid="running-band-peer-fail"]')).toBeTruthy();
    // The running main surface cannot show the English/long reason (a known cost of band takeover, documented explicitly).
    expect(container.textContent).not.toContain("peer_errors.execution_failed");

    // ② One-click access: click the disclosure strip → the full terminal reason returns (next-intl mocks echo keys).
    fireEvent.click(
      container.querySelector('[data-testid="running-band-bar"]') as HTMLElement,
    );
    expect(container.textContent).toContain("peer_errors.execution_failed");
  });

  it("★ 接线：store 里的 peer 终态到得了带上（①，运行态账本关掉后的唯一落点）", () => {
    // This guards **wiring** (the peerTerminalById prop from `TurnContainer` → `RunningBand`),
    // rather than data-selection rules; those live in
    // `step-list/running-band-model.test.ts`, with rendering in `step-list/RunningBand.test.tsx`.
    // Mutation: remove `peerTerminalById={turnStoreState?.peerTerminalById}` from <RunningBand>
    //       → this test must fail (model/component guards still pass because they supply the prop directly).
    const turnId = "t-band-peer-terminal-wired";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");
    applyPeerName(turnId, "peer_1", "compute", "拆解营收", "数据专家");
    applyPeerTerminal({
      turnId,
      peerId: "peer_1",
      status: "failed",
      failureReason: "peer_execution_failed",
      durationMs: 1200,
    });

    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    const band = container.querySelector('[data-testid="running-band"]')!;
    expect(band.querySelector('[data-row-kind="peer"].is-fail')).toBeTruthy();
    expect(band.querySelector('[data-testid="running-band-peer-fail"]')).toBeTruthy();
  });

  it("零 peer 的普通轮不会因此渲出空的 trace 壳", () => {
    // The converse of the previous test: does unchanged `traceEnabled` render an empty shell? No:
    // `hasTraceFacts` stays false with empty stepViews and zero peers, so renderTraceOnce returns early.
    // Without this test, the previous one invites disabling traceEnabled to avoid an empty shell.
    const turnId = "t-band-no-peer";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");

    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();
    expect(container.querySelector(".bh-step-list")).toBeNull();
  });

  it("★ Lead 先出散文的轮次，带照常显示（散文与带共存）", () => {
    // The second iteration's main defect copied `showWorking`'s `!fallbackNarrative` into bandMode,
    // although narrative.delta arrives first in almost every real turn; once Lead spoke, the band never appeared,
    // effectively disabling the feature in production, while none of the new tests supplied a narrative beat.
    // Mutation: add `&& !selectNarrativeBuffer(turnStoreState)` to bandMode → this test must fail.
    const turnId = "t-band-narrative-first";
    applyCardCreated({ turnId });
    applyNarrativeAppend({ turnId, segId: "seg1", text: "我先看下数据结构……" });
    startStep(turnId, "s1");

    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    const band = container.querySelector('[data-testid="running-band"]');
    expect(band).toBeTruthy();
    expect(container.textContent).toContain("我先看下数据结构……");

    // ★ Order is also contractual: the design places prose **above** the band (1e's `.disc` is the column's last element).
    //   Mutation: move <RunningBand> back before <BeatStream> → this test must fail.
    //   Asserting only that both exist is insufficient; that let the prior version put the band above prose unnoticed.
    const prose = Array.from(container.querySelectorAll("p, div")).find(
      (n) => n.textContent === "我先看下数据结构……",
    );
    expect(prose).toBeTruthy();
    expect(
      Boolean(prose!.compareDocumentPosition(band!) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
  });

  /**
   * ③ Disable the entire running ledger; write **paired** guards.
   *
   * Guarding only suppression invites permanently hiding it, even outside the running state,
   * reverting to the bug fixed in inventory §2.5. Guarding only preservation leaves
   * the two live progress surfaces unchanged. Use three tests: suppression / preservation / positive control.
   */
  describe("★ ③ 运行态只留一个活进度面（成对闸）", () => {
    /** A realistic multi-agent running turn: peer task heading + prose + substeps. */
    function multiAgentTurn(turnId: string): void {
      applyCardCreated({ turnId });
      startStep(turnId, "s1");
      applyPeerName(turnId, "peer_1", "compute", "拆解营收构成", "数据专家");
      applyPeerProse(turnId, "peer_1", "我先看一下口径。");
      startStep(turnId, "s2", "peer_1");
    }

    it("★ 压掉那半：多 agent 运行轮的 peer 账本整块不渲", () => {
      // Root cause: `peerTasks` is written as soon as a peer is dispatched, so in **every** multi-agent turn
      // the ledger coexists with the band throughout; on 116 they were 280px apart, each spinning and reporting the same thing.
      // Mutation: restore `traceEnabled` to `(!bandSlotActive || (bandMode && …))` → this test must fail.
      const turnId = "t-band-ledger-off";
      multiAgentTurn(turnId);
      const { container } = render(
        <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
      );
      expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();
      expect(container.querySelectorAll(".bh-step-list")).toHaveLength(0);
      expect(container.querySelector(".provflow")).toBeNull();
      // The ledger is suppressed, but peer **facts** remain: both task and prose are on the band.
      expect(container.textContent).toContain("我先看一下口径。");
    });

    it("★ **无 beat 那一支**也得压住（两条判据必须同源）", () => {
      // ⚠ Added guard: the suppression test above exercises `TurnBeatFlow.traceEnabled`,
      //   so it **cannot reach** the `noBeatHasTraceFacts` predicate; breaking only the latter
      //   (removing `!bandSlotActive`) leaves that test green, as verified directly.
      //
      // The no-beat branch has narrow prerequisites: `applyStepStarted` / narrative / answer push beats,
      // so the turn must have peer facts but **no steps at all**, precisely the shape of
      // plan-path peers / direct LLM results. Adding even one step makes this guard ineffective.
      // Mutation: remove `!bandSlotActive` from `noBeatHasTraceFacts` → this test must fail.
      const turnId = "t-band-ledger-off-nobeat";
      applyCardCreated({ turnId });
      applyPeerName(turnId, "peer_1", "compute", "拆解营收构成", "数据专家");
      applyPeerProse(turnId, "peer_1", "我先看一下口径。");
      const { container } = render(
        <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
      );
      expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();
      expect(container.querySelectorAll(".bh-step-list")).toHaveLength(0);
      // Facts remain: the band carries the prose (②).
      expect(container.textContent).toContain("我先看一下口径。");
    });

    it("★ 保住那半：turn 跑完，账本必须回来（不能一压压死）", () => {
      // Mutation: make `traceEnabled` always false (ignoring bandSlotActive) → this test must fail.
      const turnId = "t-band-ledger-back-done";
      multiAgentTurn(turnId);
      const { container } = render(
        <TurnContainer runner={runner({ turnId, user: "查询", running: false })} />,
      );
      expect(container.querySelector('[data-testid="running-band"]')).toBeNull();
      expect(container.querySelector(".bh-step-list")).toBeTruthy();
    });

    it("★ 正对照：澄清挂起（带不接管）时账本也必须在", () => {
      // A **different** band-absent path from the previous test. Both are needed:
      // `running=false` proves only that the ledger returns after the turn ends, not that it remains
      // while running with the band yielding; pending clarification is precisely that running-state branch.
      const turnId = "t-band-ledger-back-clarify";
      multiAgentTurn(turnId);
      const { container } = render(
        <TurnContainer
          runner={runner({
            turnId,
            user: "查询",
            running: true,
            // Match PENDING in `TurnContainer.clarification.test.tsx`; the predicate reads
            // `runner.clarificationPending` (not `clarifications`) and must derive
            // `questions` (here via candidates synthesized into single-choice questions); otherwise memo returns null,
            // the band takes over as usual, and this positive control **passes without exercising its branch**.
            clarificationPending: {
              clarificationId: "c1",
              candidates: ["按门店", "按品牌"],
              fallbackText: "您想按哪个口径汇总？",
              questions: undefined,
            },
          })}
        />,
      );
      expect(container.querySelector('[data-testid="running-band"]')).toBeNull();
      expect(container.querySelector(".bh-step-list")).toBeTruthy();
    });
  });

  it("★ 多 agent 运行轮不在带下面渲空壳 AgentBlock", () => {
    // Final review caught a case misclassified in inventory §2.5: `hasTraceFacts` is true whenever peerNames
    // is nonempty, and peer dispatch writes it immediately, so every multi-agent running turn renders
    // an empty-body AgentBlock + provenance footer below the band (steps cleared after band takeover).
    // ⚠ Both original peer tests had different shapes (one had zero peers; the other a terminal reason),
    //   so they stayed green. Check three tables for content the band cannot render: prose / tasks / terminal states.
    // Mutation: remove `(!bandMode || bandLeavesPeerContent)` from traceEnabled → this test must fail.
    const turnId = "t-band-peer-hollow";
    applyCardCreated({ turnId });
    applyPeerName(turnId, "peer_1", "compute", "", "数据专家");
    startStep(turnId, "s1", "peer_1");

    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();
    expect(container.querySelector(".bh-step-list")).toBeNull();
    expect(container.querySelector(".provflow")).toBeNull();
  });

  it("只出散文、零 tool step 的 peer 轮不被带吃掉", () => {
    // This branch preserves **prose visibility**.
    // ⚠ Its location changed (②): prose previously required the ledger at the beat's original position; after ③ disables the running ledger,
    //   the **band's current row** is its only surface. Move the assertion accordingly (`docs/单元测试规范.md`:
    //   `能力搬家 ≠ 能力丢失`); capability remains, hosted elsewhere.
    //   Mutation: remove `peerProse={turnStoreState?.peerProse}` from <RunningBand> → this test must fail.
    const turnId = "t-band-peer-prose";
    applyCardCreated({ turnId });
    applyPeerName(turnId, "peer_1", "compute", "检查营收异常", "数据专家");
    applyPeerProse(turnId, "peer_1", "先看退款率的分布……");

    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "检查营收异常", running: true })} />,
    );
    expect(container.textContent).toContain("先看退款率的分布……");
    // The **band** must render it; a generic textContent check also counts the ledger's copy,
    // making this guard ineffective after ③.
    const band = container.querySelector('[data-testid="running-band"]')!;
    expect(band.querySelector('[data-testid="running-band-prose"]')?.textContent)
      .toContain("先看退款率的分布……");
  });

  it("★ 运行期间的硬失败：带上显红，且完整错误说明一键可达", () => {
    // Inventory §2.1 correction: after the band suppresses stepViews, a step-level hard failure's
    // FailureAckCard is absent from the main surface **while running** (turn end unmounts the band and restores forceOpen trace).
    // Thus the band must satisfy two requirements: ① make that step prominent; ② one-click access to the full card.
    // Mutation: make toneOf check only isRecoverableStepFailure (hard failures also get green ✓) → the first assertion must fail.
    const turnId = "t-band-hard-fail";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");
    applyStepDone({
      turnId,
      stepId: "s1",
      state: "failed",
      durationMs: 800,
      // Since 7162e70ee only a finite code explains a step failure (the mocked translator echoes the key).
      error: { kind: "permanent", code: "plan_execution_failed", message: "口径不存在" },
    });
    startStep(turnId, "s2");

    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );

    // ① The band's history row is red, not green ✓.
    expect(container.querySelector(".bh-band__row.is-fail")).toBeTruthy();
    // The main surface cannot yet show the original error (a known cost of band takeover, documented explicitly).
    expect(container.textContent).not.toContain("plan_execution_failed");
    expect(container.textContent).not.toContain("口径不存在");

    // ② Click the band = expand the running process → the full FailureAckCard returns.
    fireEvent.click(
      container.querySelector('[data-testid="running-band-bar"]') as HTMLElement,
    );
    // Yielding = **three rows** give way to full trace; the disclosure strip remains as the collapse control (one control, two states).
    // 2026-08-15: expansion was a one-way door; users could never return to the band afterward (user-reported).
    expect(container.querySelector(".bh-band__now")).toBeNull();
    expect(container.querySelector('[data-testid="running-band-bar"]')).toBeTruthy();
    expect(container.textContent).toContain("plan_execution_failed");
    expect(container.textContent).not.toContain("口径不存在");
  });

  it("★★★ 展开不再是单向门：再点一下披露条就收回带（user 报的那条）", () => {
    // 📏 User observation: expanding then collapsing showed `之前的控件样子` (ledger + provenance),
    //    with no return to the band. Root cause: `traceRequestOrigin` clears only on a new turn,
    //    and `onExpand` had **no reverse action anywhere in the repository**.
    // Mutation: change `onExpand={() => setTraceRequestOrigin(null)}` back to `?? "band"` → this test fails.
    const turnId = "t-band-toggle";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");
    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    const bar = () => container.querySelector('[data-testid="running-band-bar"]') as HTMLElement;

    // Collapsed: three rows present, trace absent.
    expect(container.querySelector(".bh-band__now")).toBeTruthy();
    expect(container.querySelector('[data-placement="band-slot"]')).toBeNull();

    fireEvent.click(bar());   // Expand
    expect(container.querySelector(".bh-band__now")).toBeNull();
    expect(container.querySelector('[data-placement="band-slot"]')).toBeTruthy();

    fireEvent.click(bar());   // ★ Click again = return to the band
    expect(container.querySelector(".bh-band__now")).toBeTruthy();
    expect(container.querySelector('[data-placement="band-slot"]')).toBeNull();
  });

  it("★★★ 开合不重建：披露条是同一个 DOM 节点，键盘焦点不掉", () => {
    // Independent review caught the first version's expanded state using a **second** `<RunningBand>` JSX location,
    // causing React to unmount and rebuild on every toggle: a new button node, focus lost to <body>,
    // so keyboard users could not press again to collapse. This is the same trap as item 2
    // in RunningBand's header (one JSX location only) and this file's :537 warning.
    // Mutation: split `expanded={bandExpandedInPlace}` into two branches, each with <RunningBand> → this test fails.
    const turnId = "t-band-focus-keep";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");
    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    const before = container.querySelector('[data-testid="running-band-bar"]') as HTMLElement;
    before.focus();
    expect(document.activeElement).toBe(before);

    fireEvent.click(before);   // Expand

    const after = container.querySelector('[data-testid="running-band-bar"]') as HTMLElement;
    expect(after).toBe(before);                    // Same node, no rebuild
    expect(document.activeElement).toBe(after);    // Focus remains; press again to collapse
  });

  it("★ 展开态**只**渲披露条，不渲三行（否则同屏两个进度面，正是 ③ 要关的）", () => {
    // Mutation: remove `expanded ? null : (three rows)` in `RunningBand` → this test fails.
    const turnId = "t-band-expanded-rows";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");
    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    fireEvent.click(container.querySelector('[data-testid="running-band-bar"]') as HTMLElement);
    expect(container.querySelector(".bh-band__now")).toBeNull();
    expect(container.querySelectorAll(".bh-band__row")).toHaveLength(0);
    // The disclosure strip remains, now offering collapse.
    const bar = container.querySelector('[data-testid="running-band-bar"]') as HTMLElement;
    expect(bar).toBeTruthy();
    expect(bar.getAttribute("aria-expanded")).toBe("true");
    expect(bar.getAttribute("aria-label")).toBe("collapse_trace");
  });

  it("带让位之后本轮不再抢回来（用户要过细节就不收回去）", () => {
    const turnId = "t-band-sticky-expand";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");

    const { container, rerender } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    fireEvent.click(
      container.querySelector('[data-testid="running-band-bar"]') as HTMLElement,
    );
    // Yielding = **three rows** give way to full trace; the disclosure strip remains as the collapse control (one control, two states).
    // 2026-08-15: expansion was a one-way door; users could never return to the band afterward (user-reported).
    expect(container.querySelector(".bh-band__now")).toBeNull();
    expect(container.querySelector('[data-testid="running-band-bar"]')).toBeTruthy();

    // Two more steps arrive; new progress must not replace the trace with the band.
    startStep(turnId, "s2");
    startStep(turnId, "s3");
    rerender(<TurnContainer runner={runner({ turnId, user: "查询", running: true })} />);
    expect(container.querySelector(".bh-band__now")).toBeNull();
    expect(container.querySelector('[data-testid="running-band-bar"]')).toBeTruthy();
  });

  // Observed on 116 (1.0.97 plan turn): the band rendered at the turn block's **bottom**, trace at the beat's original position,
  // ~280px apart. Clicking `展开运行过程` expanded trace **above** the viewport; the user reported `跳到顶部了`.
  // Fix: expand in place, showing the expanded content where the user tapped.
  // These guards must be **paired**: band origin → band slot; inline origin → original position.
  // Guarding only the first half invites moving inline expansion to the slot too, relocating
  // the trace being read on HITL resume (explicitly prohibited in inventory §2.9; the resume test in `TurnContainer.test.tsx`
  // already failed once for this, correctly, rather than because the test was obsolete).
  it("★ 从带展开 → trace 就地渲在带的槽位，且**只此一份**", () => {
    // ⚠ The fixture must include a **peer with nonempty prose**: in a zero-peer turn, the beat-position path
    //   has nothing to render, so the single-copy check is ineffective; the first version used that fixture,
    //   and stayed green after removing suppression (`docs/单元测试规范.md`: `fixture 选错样本，断言空转`).
    //   Peer facts make both paths **want** to render, giving suppression something to block.
    const turnId = "t-band-expand-inplace";
    applyCardCreated({ turnId });
    applyPeerName(turnId, "peer_1", "compute", "", "数据专家");
    applyPeerProse(turnId, "peer_1", "我先看一下口径。");
    startStep(turnId, "s1", "peer_1");
    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    // **Before** expansion: after ③ the entire running ledger is absent, with zero copies (the guard for ③ itself).
    // ⚠ Changing 1 copy to 0 here captures ③'s core behavior change, rather than weakening the assertion:
    //   after expansion, the guard still requires **exactly 1 copy**, locking down both states.
    expect(container.querySelectorAll(".bh-step-list")).toHaveLength(0);

    fireEvent.click(
      container.querySelector('[data-testid="running-band-bar"]') as HTMLElement,
    );
    const disclosure = container.querySelector('[data-testid="execution-disclosure"]');
    expect(disclosure).toBeTruthy();
    expect(disclosure!.getAttribute("data-placement")).toBe("band-slot");
    // Still **only one copy** after expansion: silence the beat-position path or the peer ledger renders twice.
    expect(container.querySelectorAll(".bh-step-list")).toHaveLength(1);
    expect(disclosure!.querySelector(".bh-step-list")).toBeTruthy();
  });

  it("★ 就地展开后在内容上按一下，**不许**搬回 beat 原位", () => {
    // A real intermediate bug: `markExplicitDisclosureInspection` rewrote the origin every time,
    // so tapping the expanded inline trace changed its origin to inline and moved content from the slot back above,
    // relocating it mid-read, **the same problem** this fix addresses.
    // Recording only the first origin fixes this.
    const turnId = "t-band-expand-sticky-placement";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");
    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    fireEvent.click(
      container.querySelector('[data-testid="running-band-bar"]') as HTMLElement,
    );
    const list = container.querySelector(".bh-step-list") as HTMLElement;
    expect(list).toBeTruthy();

    fireEvent.pointerDown(list);   // User taps the trace (explicit inspection)

    const disclosure = container.querySelector('[data-testid="execution-disclosure"]');
    // Assert presence in the slot, rather than absence elsewhere; the latter is vacuously true if all disclosure disappears.
    expect(disclosure).toBeTruthy();
    expect(disclosure!.getAttribute("data-placement")).toBe("band-slot");
    expect(container.querySelectorAll(".bh-step-list")).toHaveLength(1);
  });

  it("★ 换一轮之后带回来（traceRequested 必须按 turnId 重置）", () => {
    // Without reset, after one expansion every subsequent turn in this conversation would lack the band.
    // Mutation: remove `setTraceRequested(false)` from the turnId effect → this test must fail.
    applyCardCreated({ turnId: "t-first" });
    startStep("t-first", "s1");
    const { container, rerender } = render(
      <TurnContainer runner={runner({ turnId: "t-first", user: "q1", running: true })} />,
    );
    fireEvent.click(
      container.querySelector('[data-testid="running-band-bar"]') as HTMLElement,
    );
    // Yielding = **three rows** give way to full trace; the disclosure strip remains as the collapse control (one control, two states).
    // 2026-08-15: expansion was a one-way door; users could never return to the band afterward (user-reported).
    expect(container.querySelector(".bh-band__now")).toBeNull();
    expect(container.querySelector('[data-testid="running-band-bar"]')).toBeTruthy();

    applyCardCreated({ turnId: "t-second" });
    startStep("t-second", "s2");
    rerender(
      <TurnContainer runner={runner({ turnId: "t-second", user: "q2", running: true })} />,
    );
    expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();
  });

  it("★ Tab 到披露条 / 触屏起手滑动**不能**把带换掉", () => {
    // Final review caught direct setTraceRequested(true) in `markExplicitInspection`, after which
    // the interaction region's onPointerDown / onFocusCapture immediately unmounted the entire band in both cases:
    //   · Keyboard user tabs to the disclosure button (focus) → band disappears, focus drops to <body>;
    //   · Mobile user starts swiping on the band (touch pointerdown) → running surface is replaced.
    // Yielding the band must use only the disclosure strip's onClick entry point.
    // Mutation: remove the `.bh-step-list / step-summary` target check → this test must fail.
    const turnId = "t-band-focus";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");
    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: true })} />,
    );
    const bar = container.querySelector('[data-testid="running-band-bar"]') as HTMLElement;

    fireEvent.focus(bar);
    expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();

    fireEvent.pointerDown(bar);
    expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();

    // Only an actual click yields the band (the sole entry point).
    fireEvent.click(bar);
    expect(container.querySelector(".bh-band__now")).toBeNull();
    expect(container.querySelector('[data-testid="running-band-bar"]')).toBeTruthy();
  });

  it("turn 跑完 → 带退场，交给既有的完成态（StepSummary / trace）", () => {
    const turnId = "t-band-done";
    applyCardCreated({ turnId });
    startStep(turnId, "s1");
    applyStepDone({ turnId, stepId: "s1", state: "done", durationMs: 900 });

    const { container } = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: false })} />,
    );
    expect(container.querySelector('[data-testid="running-band"]')).toBeNull();
    expect(container.querySelector(".bh-step-list")).toBeTruthy();
  });
});
