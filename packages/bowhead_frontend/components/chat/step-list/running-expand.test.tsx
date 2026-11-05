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

function trace(steps: StepView[], running: boolean) {
  return (
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <TurnRunningProvider running={running}>
        <AgentTrace steps={steps} peerNames={{}} peerProse={{}} />
      </TurnRunningProvider>
    </NextIntlClientProvider>
  );
}

describe("Failed step details", () => {
  it.each([true, false])("opens recorded failures only when selected (turn running: %s)", (running) => {
    render(trace([selfHealingStep(), terminalStep()], running));
    const recoverable = screen.getByRole("button", { name: /各省营业额/ });
    const permanent = screen.getByRole("button", { name: /退款率/ });
    expect(recoverable.getAttribute("aria-expanded")).toBe("false");
    expect(permanent.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText(zh.step_list.step_failed_generic)).toBeNull();

    fireEvent.click(permanent);
    expect(permanent.getAttribute("aria-expanded")).toBe("true");
    expect(recoverable.getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByText(zh.step_list.step_failed_generic)).toBeTruthy();
  });

  it("keeps the user's disclosure choices when the turn ends", () => {
    const steps = [selfHealingStep(), terminalStep()];
    const { rerender } = render(trace(steps, true));
    const recoverable = screen.getByRole("button", { name: /各省营业额/ });
    const permanent = screen.getByRole("button", { name: /退款率/ });
    fireEvent.click(permanent);
    rerender(trace(steps, false));
    expect(recoverable.getAttribute("aria-expanded")).toBe("false");
    expect(permanent.getAttribute("aria-expanded")).toBe("true");
  });
});
