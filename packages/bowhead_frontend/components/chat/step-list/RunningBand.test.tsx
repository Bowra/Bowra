/**
 * RunningBand — component guards (throttling / timing / three-row structure / announcements).
 * Boundaries with existing disclosure live in `../TurnContainer.running-band.test.tsx`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";

import { RunningBand, formatElapsed, MIN_DWELL_MS, DWELL_BACKLOG_MAX } from "./RunningBand";
import type { StepView } from "./types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vals?: Record<string, unknown>) =>
    vals ? `${key}:${Object.values(vals).join(",")}` : key,
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function step(id: string, over: Record<string, unknown> = {}): StepView {
  return {
    stepId: id,
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询数据",
    state: "done",
    ...over,
  } as unknown as StepView;
}

/** Band row count = history rows + current row. Always three rows is the basis for constant height. */
function rowCount(container: HTMLElement): number {
  return container.querySelectorAll(".bh-band__row").length
    + container.querySelectorAll(".bh-band__now").length;
}

describe("三行恒定 —— 高度恒定的实现基础", () => {
  it.each([
    ["零步骤", [] as StepView[]],
    ["一步", [step("a", { state: "active" })]],
    ["五步", ["a", "b", "c", "d", "e"].map((id, i) =>
      step(id, { state: i === 4 ? "active" : "done" }))],
  ])("%s → 3 行", (_label, steps) => {
    const { container } = render(<RunningBand steps={steps} running startedAt={null} />);
    expect(rowCount(container)).toBe(3);
  });

  it("纯聊天轮（答案流式 + 零步骤）收成单行，但带本身还在", () => {
    const { container } = render(
      <RunningBand steps={[]} running startedAt={null} answerStreaming />,
    );
    expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();
    expect(container.getAttribute("data-density")).toBe(null); // Root node is inside container.
    expect(
      container.querySelector('[data-testid="running-band"]')?.getAttribute("data-density"),
    ).toBe("single");
    expect(rowCount(container)).toBe(1);
  });
});

describe("Recorded step issues", () => {
  it.each([
    ["failed", "band_row_failed"],
    ["partial", "band_row_partial"],
    ["cancelled", "band_row_cancelled"],
  ])("shows the original %s outcome without inventing a retry", (state, key) => {
    const { container } = render(
      <RunningBand steps={[
        step("issue", { state, error: { kind: "transient" } }),
        step("next", { state: "active" }),
      ]} running startedAt={null} />,
    );
    const row = container.querySelector('[data-row-kind="step"]')!;
    expect(row.textContent).toContain(key);
    expect(row.querySelector(".lucide-triangle-alert")).toBeTruthy();
    expect(container.textContent).not.toMatch(/retried_n|band_row_retried/);
    if (state !== "cancelled") {
      expect(container.querySelector('[data-testid="running-band-issues"]')?.textContent).toBe("step_issues_n:1");
      expect(container.querySelector('[role="status"]')?.textContent).toContain("step_issues_n:1");
    } else {
      expect(container.querySelector('[data-testid="running-band-issues"]')).toBeNull();
    }
  });
});

describe("最短驻留 —— 节流不是防抖", () => {
  it("★ 步骤每 200ms 到一次时仍在放行（防抖写法会一直不放行）", () => {
    // On 116, two 0ms steps in one turn flashed past in one frame without throttling; naive debouncing
    // (resetting the timer whenever steps change) **never** releases during continuous arrivals,
    // holding until backlog exceeds the threshold, then jumping, exactly the flash throttling should prevent.
    // Mutation: make `wait` always MIN_DWELL_MS (= debounce) → this case must fail.
    vi.useFakeTimers();
    const all = ["a", "b", "c"].map((id) => step(id));
    const { container, rerender } = render(
      <RunningBand steps={[]} running startedAt={null} />,
    );

    // Three steps arrive 200ms apart; total 600ms > MIN_DWELL_MS.
    for (let i = 0; i < all.length; i += 1) {
      rerender(<RunningBand steps={all.slice(0, i + 1)} running startedAt={null} />);
      vi.advanceTimersByTime(200);
      rerender(<RunningBand steps={all.slice(0, i + 1)} running startedAt={null} />);
    }
    vi.advanceTimersByTime(MIN_DWELL_MS);
    rerender(<RunningBand steps={all} running startedAt={null} />);

    // Released at least through step two (disclosure's step N is no longer 0).
    expect(container.querySelector(".bh-band__meta")?.textContent).toMatch(/band_step_n:[23]/);
  });

  it("积压超过阈值直接跳到最新（排队排到天亮比闪更糟）", () => {
    vi.useFakeTimers();
    const many = Array.from({ length: DWELL_BACKLOG_MAX + 3 }, (_, i) => step(`s${i}`));
    const { container, rerender } = render(
      <RunningBand steps={[]} running startedAt={null} />,
    );
    rerender(<RunningBand steps={many} running startedAt={null} />);
    vi.advanceTimersByTime(1);
    rerender(<RunningBand steps={many} running startedAt={null} />);
    expect(container.querySelector(".bh-band__meta")?.textContent)
      .toContain(`band_step_n:${many.length}`);
  });
});

describe("计时走 i18n —— 不在组件里拼中文", () => {
  const echo = ((key: string, vals?: Record<string, unknown>) =>
    vals ? `${key}:${Object.values(vals).join(",")}` : key) as never;

  it("★ 三个区间各走各的 key（突变：改回硬编码「秒/分」→ 必红）", () => {
    expect(formatElapsed(18_000, echo)).toBe("band_elapsed_sec:18");
    expect(formatElapsed(120_000, echo)).toBe("band_elapsed_min:2");
    expect(formatElapsed(205_000, echo)).toBe("band_elapsed_min_sec:3,25");
  });

  it("负数与 0 不炸", () => {
    expect(formatElapsed(-5, echo)).toBe("band_elapsed_sec:0");
  });
});

describe("播报", () => {
  it("整条带**不是** live region，只有一个隐藏的 role=status", () => {
    // Putting role=status on the band itself would interrupt screen-reader users every second as its timer updates.
    const { container } = render(
      <RunningBand steps={[step("a", { state: "active" })]} running startedAt={Date.now()} />,
    );
    const band = container.querySelector('[data-testid="running-band"]')!;
    expect(band.getAttribute("role")).toBeNull();
    expect(band.querySelectorAll('[role="status"]')).toHaveLength(1);
    // Announcements exclude the timer changing every second.
    expect(band.querySelector('[role="status"]')?.textContent).not.toMatch(/band_elapsed/);
  });

  it("★★★ 派了 peer 但名字还没到 → 不点名，且**绝不**渲成 Lead 自己的名字", () => {
    // 📏116 v1.1.5 dogfood captured `正在等 Bowhead 返回` (Lead waiting for itself).
    // Pin the **render branch**: the model gives waitingOnPeerUnknown, so the component must use anonymous copy.
    // Mutation: remove the `waitingOnPeerUnknown` branch → fall back to band_thinking; this case fails.
    const { container } = render(
      <RunningBand
        steps={[step("spawn", {
          state: "active", toolName: "subagent_spawn", headerZh: "派子 agent",
          subagentId: "lead_1", peerProseKey: "p1",
        })]}
        running
        startedAt={null}
      />,
    );
    const act = container.querySelector(".bh-band__act")?.textContent ?? "";
    expect(act).toBe("band_waiting_peer_anon");
    // Before the name arrives, **never** use the named variant (it would receive Lead's name).
    expect(act).not.toMatch(/^band_waiting_peer:/);
  });

  it("★ 正对照：名字到了就走点名那条", () => {
    const { container } = render(
      <RunningBand
        steps={[step("spawn", {
          state: "active", toolName: "subagent_spawn", headerZh: "派子 agent",
          subagentId: "lead_1", peerProseKey: "p1",
        })]}
        peerNames={{ p1: "compute" }}
        peerDisplayNames={{ p1: "主分析" }}
        running
        startedAt={null}
      />,
    );
    expect(container.querySelector(".bh-band__act")?.textContent).toBe("band_waiting_peer:主分析");
  });

  it("静默期（paused）当前行换成「仍在等模型响应」", () => {
    const { container } = render(
      <RunningBand steps={[step("a", { state: "active" })]} running startedAt={null} paused />,
    );
    expect(container.querySelector(".bh-band__act")?.textContent).toBe("band_paused");
    expect(
      container.querySelector('[data-testid="running-band"]')?.getAttribute("data-paused"),
    ).toBe("1");
  });
});

describe("披露条与当前行是两个粒度", () => {
  it("★ 运行中两处**不能**渲同一句话", () => {
    // Spike screenshots caught the first disclosure strip and current row both rendering `what`, duplicating one sentence onscreen.
    // Tests checking only that both have content missed it; directly compare the two strings here.
    // Mutation: revert action to barText → this case must fail.
    const { container } = render(
      <RunningBand
        steps={[step("a", { state: "active", headerZh: "按门店汇总客流量并排名" })]}
        running
        startedAt={null}
      />,
    );
    const bar = container.querySelector(".bh-band__txt")?.textContent ?? "";
    const act = container.querySelector(".bh-band__act")?.textContent ?? "";
    expect(bar).toBeTruthy();
    expect(act).toBeTruthy();
    expect(act).not.toBe(bar);
    // Disclosure strip uses coarse phase vocabulary based on tool category (bi_query → query).
    expect(bar).toBe("band_phase_query");
    // Only the current row carries the specific action.
    expect(act).toContain("按门店汇总客流量并排名");
  });

  it("认不出类别的工具 → 披露条回落通用相位词", () => {
    const { container } = render(
      <RunningBand
        steps={[step("a", { state: "active", toolName: "brand_new_tool", headerZh: "在做点什么" })]}
        running
        startedAt={null}
      />,
    );
    expect(container.querySelector(".bh-band__txt")?.textContent).toBe("band_working");
  });
});

describe("禁词与兜底", () => {
  it("★ 抬头被禁词滤空的**已完成**步骤画成完成行，不是骨架", () => {
    // Checking only whether label is empty would make finished work look like it is still loading.
    const { container } = render(
      <RunningBand
        steps={[
          step("a", { headerZh: "运行: python_sandbox", toolName: "brand_new_tool" }),
          step("b", { state: "active" }),
        ]}
        running
        startedAt={null}
      />,
    );
    const rows = Array.from(container.querySelectorAll(".bh-band__row"));
    const ghosts = rows.filter((r) => r.className.includes("--ghost"));
    expect(ghosts).toHaveLength(1); // Only the padding row is a skeleton.
    expect(container.textContent).toContain("band_row_done");
    expect(container.textContent).not.toContain("python_sandbox");
  });
});

/**
 * chat-ux-2026-08 ① — peer terminal states on the band.
 * Data rules (which rows, ordering) live in `running-band-model.test.ts`; pin only **rendering** here:
 * glyphs / copy keys / persistent chip.
 */
describe("★ peer 终态行与常驻失败计数", () => {
  const peerNames = { p1: "compute" };
  const peerDisplayNames = { p1: "主分析" };

  it("零子步骤的 peer 挂掉 → 红行 + 归属名进得去文案", () => {
    const { container } = render(
      <RunningBand
        steps={[step("a", { state: "active" })]}
        running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
        peerTerminalById={{ p1: { status: "failed", evtSeq: 9 } }}
      />,
    );
    const row = container.querySelector('[data-row-kind="peer"]');
    expect(row).toBeTruthy();
    expect(row!.className).toContain("is-fail");
    // Copy uses i18n (mock echoes key:parameters), **not** Chinese strings constructed in the model.
    expect(row!.textContent).toContain("band_peer_failed:主分析");
  });

  it("★ peer 行**不能**渲成「已重试」", () => {
    // ↻ + "已重试" is reserved for self-healing failures (step row tone=warn) on this band.
    // Peer rows branch by `peerOutcome` (facts), never by `tone` (visual tier).
    // Mutation: renderer branches peer rows by `tone` again → this case must fail.
    const { container } = render(
      <RunningBand
        steps={[]} running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
        peerTerminalById={{ p1: { status: "failed", evtSeq: 9 } }}
      />,
    );
    const row = container.querySelector('[data-row-kind="peer"]')!;
    expect(row.textContent).toContain("band_peer_failed:主分析");
    expect(row.textContent).not.toContain("band_row_retried");
  });

  it("★ 失败计数必须能被读屏听见（chip 在 aria-label 的 button 里，听不到）", () => {
    // Independent review caught the chip inside `.bh-band__bar`, whose button has `aria-label`,
    // **replacing the whole accessible name**: screen readers never hear the chip. It is the persistent half of disclosure,
    // so being inaudible makes that disclosure nonexistent for screen-reader users.
    // Mutation: remove peerFailCount from `.bh-band__sr` announcement text → this case must fail.
    const { container } = render(
      <RunningBand
        steps={["a", "b", "c"].map((id, i) =>
          step(id, { state: i === 2 ? "active" : "done", evtSeq: 10 + i }))}
        running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
        peerTerminalById={{ p1: { status: "failed", evtSeq: 1 } }}
      />,
    );
    const live = container.querySelector('[role="status"]')!;
    expect(live.textContent).toContain("band_peer_fail_n:1");
  });

  it("★ 失败计数 chip 常驻披露条 —— 行滚出去了它还在", () => {
    // History has only 2 rows. Deliberately put the peer terminal state **first**, followed by three steps,
    // forcing it out of history; the chip is its only remaining trace.
    // Mutation: remove the disclosure strip's `peerFailCount` chip → this case must fail
    //       (a row-presence assertion cannot catch it; the row should already have scrolled out).
    const { container } = render(
      <RunningBand
        steps={["a", "b", "c"].map((id, i) =>
          step(id, { state: i === 2 ? "active" : "done", evtSeq: 10 + i }))}
        running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
        peerTerminalById={{ p1: { status: "failed", evtSeq: 1 } }}
      />,
    );
    expect(container.querySelector('[data-row-kind="peer"]')).toBeNull();  // Already scrolled out.
    const chip = container.querySelector('[data-testid="running-band-peer-fail"]');
    expect(chip).toBeTruthy();
    expect(chip!.textContent).toContain("band_peer_fail_n:1");
  });

  it("零失败时不出 chip（正对照：上一条不是恒真）", () => {
    const { container } = render(
      <RunningBand
        steps={[step("a", { state: "active" })]} running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
        peerTerminalById={{ p1: { status: "succeeded", evtSeq: 1 } }}
      />,
    );
    expect(container.querySelector('[data-testid="running-band-peer-fail"]')).toBeNull();
  });

  it("★ 恒高不变：peer 行进来之后仍是 3 行", () => {
    const { container } = render(
      <RunningBand
        steps={[]} running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
        peerTerminalById={{ p1: { status: "failed", evtSeq: 1 } }}
      />,
    );
    expect(rowCount(container)).toBe(3);
  });
});

/**
 * chat-ux-2026-08 ② — prose tail rendering and throttling in the current row.
 * Data rules (whose prose, windowing/filter order) live in `running-band-model.test.ts`.
 */
describe("★ 散文尾巴：当前行第二行", () => {
  const peerNames = { p1: "compute" };
  const peerDisplayNames = { p1: "主分析" };

  it("散文渲在当前行的 foot 里", () => {
    const { container } = render(
      <RunningBand
        steps={[step("a", { state: "active", subagentId: "p1" })]}
        running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
        peerProse={{ p1: "正在按品牌汇总营业额" }}
      />,
    );
    const prose = container.querySelector('[data-testid="running-band-prose"]');
    expect(prose).toBeTruthy();
    expect(prose!.textContent).toContain("正在按品牌汇总营业额");
    expect(prose!.closest(".bh-band__foot")).toBeTruthy();
  });

  it("★ 散文在场时替掉 detail，但归属名仍在", () => {
    // `.bh-band__foot` is one line: including both silently sacrifices the less-observed item under flex pressure.
    // Mutation: make footMeta always pass detail rather than `prose ? [] : detail` → this case must fail.
    const { container } = render(
      <RunningBand
        steps={[step("a", { state: "active", subagentId: "p1", label: "2025年 天津 营业额" })]}
        running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
        peerProse={{ p1: "正在核对口径" }}
      />,
    );
    const foot = container.querySelector(".bh-band__foot")!;
    expect(foot.textContent).toContain("主分析");          // Preserve attribution.
    expect(foot.textContent).toContain("正在核对口径");     // Prose remains.
    expect(foot.textContent).not.toContain("2025年 天津 营业额"); // Detail yields.
  });

  it("正对照：没有散文时 detail 必须回来", () => {
    // Pinning only the preceding case would also pass if detail never rendered.
    const { container } = render(
      <RunningBand
        steps={[step("a", { state: "active", subagentId: "p1", label: "2025年 天津 营业额" })]}
        running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
      />,
    );
    const foot = container.querySelector(".bh-band__foot")!;
    expect(foot.textContent).toContain("2025年 天津 营业额");
    expect(container.querySelector('[data-testid="running-band-prose"]')).toBeNull();
  });

  it("★ 无名 peer 走「无名」文案，不出现编造的名字", () => {
    // Mutation: renderer always uses `band_peer_failed:{name}` again → this case must fail
    //       (would render `band_peer_failed:` with an empty suffix).
    const { container } = render(
      <RunningBand
        steps={[]} running startedAt={null}
        peerTerminalById={{ pX: { status: "failed", evtSeq: 1 } }}
      />,
    );
    const row = container.querySelector('[data-row-kind="peer"]')!;
    expect(row.textContent).toContain("band_peer_failed_anon");
    expect(row.textContent).not.toContain("band_peer_failed:");
  });

  it("★★ 换人那一刻散文必须同帧跟着换（节流不能跨归属）", () => {
    // Independent review caught `footMeta` (including agentName) updating synchronously, while throttled prose lagged
    // 450ms: at handoff, the footnote showed the new agent's name with the previous agent's words,
    // reintroducing in rendering the misattribution blocked by model-level allowFallback:false.
    // Mutation: remove ownerKey reset in `useThrottledText` → this case must fail.
    vi.useFakeTimers();
    const view = (id: string, sub: string) => (
      <RunningBand
        steps={[step("a", { state: "active", subagentId: sub })]}
        running startedAt={null}
        peerNames={{ p1: "compute", p2: "compute" }}
        peerDisplayNames={{ p1: "销售分析", p2: "库存分析" }}
        peerProse={{ p1: "我在看销售", p2: "我在看库存" }}
        key={id}
      />
    );
    const { container, rerender } = render(view("v", "p1"));
    expect(
      container.querySelector('[data-testid="running-band-prose"]')!.textContent,
    ).toContain("我在看销售");

    // Ownership changes p1 → p2. **Advance no timers**: the next frame must already show p2's words.
    rerender(view("v", "p2"));
    const foot = container.querySelector(".bh-band__foot")!;
    expect(foot.textContent).toContain("库存分析");     // Name changed.
    expect(foot.textContent).toContain("我在看库存");   // Words must change too.
    expect(foot.textContent).not.toContain("我在看销售");
  });

  it("★★ 交棒到 Lead 自己的步时，上一位的散文必须立刻消失", () => {
    // Other half of the same rule: model returns an empty string for Lead steps (no fallback to someone else's prose);
    // without resetting throttling, it takes 450ms to apply, temporarily showing Bowhead with some peer's words.
    vi.useFakeTimers();
    const view = (sub?: string) => (
      <RunningBand
        steps={[step("a", { state: "active", ...(sub ? { subagentId: sub } : {}) })]}
        running startedAt={null}
        peerNames={{ p1: "compute" }} peerDisplayNames={{ p1: "销售分析" }}
        peerProse={{ p1: "我在看销售" }}
      />
    );
    const { container, rerender } = render(view("p1"));
    expect(container.querySelector('[data-testid="running-band-prose"]')).toBeTruthy();
    rerender(view());   // Lead's own step.
    expect(container.querySelector('[data-testid="running-band-prose"]')).toBeNull();
  });

  it("★ 散文是**节流**不是防抖 —— token 连续到达时必须放行", () => {
    // Naive debounce (reset timer on every new value) never releases when tokens arrive every few tens of milliseconds,
    // leaving prose at its first frame. Calculate wait from the **last release time**.
    // Mutation: make `useThrottledText` wait always `ms` (debounce reset) → this case must fail.
    vi.useFakeTimers();
    const render1 = (prose: string) => (
      <RunningBand
        steps={[step("a", { state: "active", subagentId: "p1" })]}
        running startedAt={null}
        peerNames={peerNames} peerDisplayNames={peerDisplayNames}
        peerProse={{ p1: prose }}
      />
    );
    const { container, rerender } = render(render1("第一段"));
    // A new token every 100ms, 800ms total, far beyond one MIN_DWELL_MS window.
    for (let i = 0; i < 8; i += 1) {
      rerender(render1(`第一段${"字".repeat(i + 1)}`));
      act(() => { vi.advanceTimersByTime(100); });
    }
    const prose = container.querySelector('[data-testid="running-band-prose"]')!;
    // Has released, rather than remaining at the initial frame.
    expect(prose.textContent).not.toBe("第一段");
    expect(prose.textContent).toContain("第一段字");
  });
});

describe("工作台与画布第 1 批：原内联样式改修饰类", () => {
  it.each([
    [false, "bh-band__bar", "bh-band__chev"],
    [true, "bh-band__bar legacy-band-bar--expanded", "bh-band__chev legacy-band-chev--expanded"],
  ])("expanded=%s：展开时去底线、箭头朝上（原 borderBottom: none / rotate(180deg)）", (expanded, bar, chev) => {
    const { container } = render(<RunningBand steps={[step("a", { state: "active" })]} running startedAt={null} expanded={expanded} />);
    const barEl = container.querySelector("[data-testid='running-band-bar']") as HTMLElement;
    expect(barEl.className).toBe(bar);
    expect(barEl.getAttribute("style")).toBeNull();
    const chevEl = container.querySelector(".bh-band__chev") as HTMLElement;
    expect(chevEl.className).toBe(chev);
    expect(chevEl.getAttribute("style")).toBeNull();
  });

  it("骨架行首条宽 20 走类，第二条按行号算的宽度仍是内联", () => {
    const { container } = render(<RunningBand steps={[]} running startedAt={null} />);
    const ghost = container.querySelector(".bh-band__row--ghost") as HTMLElement;
    const [lead, second] = Array.from(ghost.querySelectorAll(".bh-band__gbar")) as HTMLElement[];
    expect(lead.className).toBe("bh-band__gbar legacy-band-gbar--lead");
    expect(lead.getAttribute("style")).toBeNull();
    expect(second.style.width).toMatch(/px$/);
  });
});
