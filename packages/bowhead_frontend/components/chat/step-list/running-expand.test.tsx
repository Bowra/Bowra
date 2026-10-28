/**
 * chat-ux-2026-08 batch B extraction — do not expand self-healing failures while running.
 *
 * In batch B, I tried gating on **failure type** (do not expand transient), but two existing tests blocked it and it was reverted.
 * The lesson: the wrong condition was chosen; the issue is not transient expansion, but expansion **while running**.
 * On 116, a transient failure midway through a turn opened FailureAckCard,
 * pushed everything below down, then the agent retried successfully and the explanation became stale:
 * **information no longer true** had seized the viewport.
 *
 * Pin three rules:
 *   ① While running: do not expand self-healing failures; **terminal failures still expand** (whether running, not error type).
 *   ② After completion: expand self-healing failures, both for f1-retry-skip's honest guidance
 *      and live/replay consistency (archive has no provider, always false, always expands).
 *   ③ Never seize it again after the user manually collapses it.
 *
 * ⚠ Mutation record (avoid repeating the investigation): changing `forceOpen` uniformly to `!turnRunning`
 * **does not** fail these tests. The tests are valid: **terminal failures have two independent expansion paths**,
 * `StepFromView`'s `forceOpen` and `Step`'s fallback `useState(openProp ?? (... || status === "fail"))`.
 * Either opens it, making this an **equivalent mutation** for fail steps (identical visible behavior).
 * Removing both fails tests 2 and 3 — confirmed experimentally.
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "@/messages/zh-CN.json";

import { AgentTrace } from "./AgentTrace";
import { TurnRunningProvider } from "./turn-running";
import type { StepView } from "./types";

afterEach(() => cleanup());

/** Self-healing failure: transient, with no explicit backend retryable:false. */
function selfHealingStep(): StepView {
  return {
    stepId: "s_heal",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询数据",
    state: "failed",
    label: "各省营业额",
    subagentId: "LEAD",
    error: { kind: "transient", uiKind: "transient", message: "上游超时" },
  } as unknown as StepView;
}

/** Terminal failure: backend explicitly says retry is impossible. */
function terminalStep(): StepView {
  return {
    stepId: "s_dead",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询数据",
    state: "failed",
    label: "退款率",
    subagentId: "LEAD",
    error: {
      kind: "permanent",
      uiKind: "permanent",
      message: "口径不存在",
      diagnostics: { retryable: false },
    },
  } as unknown as StepView;
}

function mount(steps: StepView[], running: boolean) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <TurnRunningProvider running={running}>
        <AgentTrace steps={steps} peerNames={{}} peerProse={{}} />
      </TurnRunningProvider>
    </NextIntlClientProvider>,
  );
}

/** Whether this step row is expanded (aria-expanded is Step's own disclosure state). */
function rowExpanded(container: HTMLElement, labelRe: RegExp): boolean {
  const btn = Array.from(container.querySelectorAll("button")).find((b) =>
    labelRe.test(b.getAttribute("aria-label") ?? b.textContent ?? ""),
  );
  expect(btn, `找不到步骤行 ${labelRe}`).toBeTruthy();
  return btn!.getAttribute("aria-expanded") === "true";
}

describe("运行期间 — 自愈失败不抢视口", () => {
  it("★ 跑的时候：会自愈的失败**不**展开（重试成功后那段解释就过期了）", () => {
    const { container } = mount([selfHealingStep()], true);
    expect(rowExpanded(container, /各省营业额/)).toBe(false);
  });

  it("★ 跑的时候：终态失败**照常**展开 —— 判据是「在不在跑」不是「什么错」", () => {
    const { container } = mount([terminalStep()], true);
    expect(rowExpanded(container, /退款率/)).toBe(true);
  });

  it("两种失败同屏时各按各的来（不是一刀切全收）", () => {
    const { container } = mount([selfHealingStep(), terminalStep()], true);
    expect(rowExpanded(container, /各省营业额/)).toBe(false);
    expect(rowExpanded(container, /退款率/)).toBe(true);
  });
});

describe("turn 跑完 — 诚实提示必须回来", () => {
  it("expands the original failure after execution without predicting another attempt", () => {
    const { container } = mount([selfHealingStep()], false);
    expect(rowExpanded(container, /各省营业额/)).toBe(true);
    // Since 7162e70ee uncoded message prose is replaced by the bounded generic explanation.
    expect(screen.getByText(zh.step_list.step_failed_generic)).toBeTruthy();
    expect(screen.queryByText("上游超时")).toBeNull();
    expect(screen.queryByText(/任务仍在进行|AI 会自行调整后重试/)).toBeNull();
  });

  it("★ 运行中→跑完 的**迁移**也要展开（openProp 只当初值读会永远展开不了）", () => {
    const steps = [selfHealingStep()];
    const { container, rerender } = render(
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <TurnRunningProvider running>
          <AgentTrace steps={steps} peerNames={{}} peerProse={{}} />
        </TurnRunningProvider>
      </NextIntlClientProvider>,
    );
    expect(rowExpanded(container, /各省营业额/)).toBe(false);

    rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <TurnRunningProvider running={false}>
          <AgentTrace steps={steps} peerNames={{}} peerProse={{}} />
        </TurnRunningProvider>
      </NextIntlClientProvider>,
    );
    expect(rowExpanded(container, /各省营业额/)).toBe(true);
  });

  it("★ 用户在运行期间自己开合过 → 跑完不抢（别跟用户争披露）", () => {
    const steps = [selfHealingStep()];
    const tree = (running: boolean) => (
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <TurnRunningProvider running={running}>
          <AgentTrace steps={steps} peerNames={{}} peerProse={{}} />
        </TurnRunningProvider>
      </NextIntlClientProvider>
    );
    const { container, rerender } = render(tree(true));
    const btn = Array.from(container.querySelectorAll("button")).find((b) =>
      /各省营业额/.test(b.getAttribute("aria-label") ?? b.textContent ?? ""),
    )!;
    fireEvent.click(btn); // User opens it manually.
    fireEvent.click(btn); // Then manually collapses it again.
    expect(rowExpanded(container, /各省营业额/)).toBe(false);

    rerender(tree(false));
    expect(rowExpanded(container, /各省营业额/)).toBe(false);
  });
});

describe("无 provider 的缺省方向", () => {
  it("★ 没有 provider（归档/重放/harness）= 不在跑 = 照常展开，行为与今天一致", () => {
    const { container } = render(
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <AgentTrace steps={[selfHealingStep()]} peerNames={{}} peerProse={{}} />
      </NextIntlClientProvider>,
    );
    expect(rowExpanded(container, /各省营业额/)).toBe(true);
  });
});
