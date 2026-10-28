import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import zh from "@/messages/zh-CN.json";

import { AgentTrace } from "./AgentTrace";
import { FailureAckCard, type FailureAckCardProps } from "./FailureAckCard";
import { TurnRunningProvider } from "./turn-running";
import type { StepView } from "./types";

const reason = "The service did not complete this step.";

function card(running: boolean, props: Partial<FailureAckCardProps> = {}) {
  return (
    <TurnRunningProvider running={running}>
      <FailureAckCard kind="transient" message={reason} {...props} />
    </TurnRunningProvider>
  );
}

describe("FailureAckCard task activity", () => {
  it("describes active work without promising an automatic retry", () => {
    renderIntl(card(true), { locale: "en-US" });
    expect(screen.getByText(reason)).toBeTruthy();
    expect(screen.getByText("This task is still running. You can add instructions in the composer.")).toBeTruthy();
    expect(screen.queryByText(/will adjust|retry on its own/)).toBeNull();
  });

  it("removes active guidance when the same task ends while retaining its reason", () => {
    const tree = (running: boolean) => (
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        {card(running)}
      </NextIntlClientProvider>
    );
    const { rerender } = renderIntl(card(true));
    expect(screen.getByText(/任务仍在进行/)).toBeTruthy();
    rerender(tree(false));
    expect(screen.getByText(reason)).toBeTruthy();
    expect(screen.queryByText(/任务仍在进行|自行调整后重试/)).toBeNull();
  });

  it("does not infer active work without enclosing lifecycle authority", () => {
    renderIntl(<FailureAckCard kind="transient" message={reason} />);
    expect(screen.getByText(reason)).toBeTruthy();
    expect(screen.queryByText(/任务仍在进行/)).toBeNull();
  });

  it.each([
    { kind: "permanent" as const },
    { retryable: false },
    { diagnostics: { schemaVersion: 1 as const, disclosure: "authenticated" as const, retryable: false } },
  ])("respects an explicit refusal of correction guidance: %j", (props) => {
    renderIntl(card(true, props));
    expect(screen.getByText(reason)).toBeTruthy();
    expect(screen.queryByText(/任务仍在进行/)).toBeNull();
  });

  it("retains known partial outcomes after the task ends", () => {
    renderIntl(card(false, { failedCount: 1, succeededCount: 2 }));
    expect(screen.getByText("1 个失败、2 个成功")).toBeTruthy();
    expect(screen.queryByText(/任务仍在进行|自行调整/)).toBeNull();
  });
});

describe("AgentTrace partial outcomes", () => {
  function partial(over: Partial<StepView> = {}): StepView {
    return {
      stepId: "partial-query",
      parentStepId: null,
      concurrencyGroupId: null,
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "Query batch",
      state: "partial",
      ...over,
    };
  }

  function trace(step: StepView, running = false) {
    return renderIntl(
      <TurnRunningProvider running={running}>
        <AgentTrace steps={[step]} peerNames={{}} peerProse={{}} />
      </TurnRunningProvider>,
    );
  }

  it("keeps a missing-query explanation factual after the task ends", () => {
    trace(partial({ state: "failed", error: { kind: "transient", code: "pbi_query_input_required", message: "" } }));
    expect(screen.getByText("本次 Power BI 查询缺少 DAX 内容。")).toBeTruthy();
    expect(screen.queryByText(/任务仍在进行|将调整后重试/)).toBeNull();
  });

  it("does not count pending or unknown substeps as successful", () => {
    trace(partial({ subSteps: [
      { id: "failed", label: "Failed", status: "failed" },
      { id: "done", label: "Done", status: "done" },
      { id: "pending", label: "Pending", status: "pending" },
      { id: "unknown", label: "Unknown" },
    ] }));
    expect(screen.getByText("1 个失败、1 个成功")).toBeTruthy();
    expect(screen.queryByText(/已自动调整并继续|自行调整/)).toBeNull();
  });

  it("does not fabricate zero outcomes when batch counters are unavailable", () => {
    trace(partial());
    expect(screen.getByText("部分子任务未完成。")).toBeTruthy();
    expect(screen.queryByText(/个失败、.*个成功/)).toBeNull();
  });

  it.each([
    { kind: "transient" as const, message: reason, retryable: false },
    { kind: "permanent" as const, message: reason },
  ])("keeps nonretryable partial failures expanded during active work: %j", (error) => {
    trace(partial({ error }), true);
    expect(screen.getByText("部分子任务未完成。")).toBeTruthy();
    expect(screen.queryByText(/任务仍在进行/)).toBeNull();
  });
});
