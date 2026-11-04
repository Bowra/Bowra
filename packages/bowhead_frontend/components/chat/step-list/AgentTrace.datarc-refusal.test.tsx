import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { renderIntl } from "@/__tests__/intl-render";
import { normalizeStepError } from "@/lib/sse-events";
import { AgentTrace } from "./AgentTrace";
import type { StepView } from "./types";

afterEach(cleanup);

it("shows the finite query refusal once in the original wrapper row", () => {
  const error = normalizeStepError({ type: "non_query_text", code: "tool_execution_failed", message_zh: "Private native instructions", is_transient: false });
  const step = { stepId: "wrapper", parentStepId: null, concurrencyGroupId: null, stepType: "tool_run", toolName: "teammcp.test.datarc_query", headerZh: "Query", label: "how to query", state: "failed", error } as unknown as StepView;
  const { container } = renderIntl(<AgentTrace steps={[step]} peerNames={{}} peerProse={{}} />);
  if (!screen.queryByText("这段文字不是数据查询，因此未执行查询。")) fireEvent.click(container.querySelector(".stp-head")!);
  expect(screen.getAllByText("这段文字不是数据查询，因此未执行查询。")).toHaveLength(1);
  expect(container.textContent).not.toMatch(/Private native instructions|管理员|工具未能完成/);
  expect(container.querySelector(".stp.is-fail")).toBeTruthy();
  expect(step.state).toBe("failed");
});

it.each(["tool_execution_unknown", "source_access_denied", "provider_unavailable"])("does not replace a stronger failure with the non-query reason: %s", (code) => {
  const error = normalizeStepError({ type: "non_query_text", code, message_zh: "", is_transient: false });
  expect(error?.code).toBe(code);
  expect(error?.message).toBe("");
});
