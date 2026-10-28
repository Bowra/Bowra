import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { AgentTrace } from "./AgentTrace";
import type { StepView } from "./types";

describe("unknown operation feedback", () => {
  it("explains uncertainty without offering a retry or exposing a machine identifier", () => {
    const step: StepView = {
      stepId: "uncertain-step",
      parentStepId: null,
      concurrencyGroupId: null,
      stepType: "tool_run",
      toolName: "subagent_spawn",
      headerZh: "Inspect source",
      label: "Inspect source",
      state: "failed",
      summary: "",
      durationText: "1s",
      detail: null,
      error: { kind: "unknown", code: "peer_execution_uncertain", message: "subagent_execution_uncertain", retryable: false },
    };
    const view = renderIntl(<AgentTrace steps={[step]} peerNames={{}} peerProse={{}} />, { locale: "en-US" });
    expect(screen.getByText("An earlier operation is still unconfirmed. You can continue the conversation; further operations are paused.")).toBeTruthy();
    expect(view.container.textContent).not.toContain("subagent_execution_uncertain");
    expect(view.container.textContent).not.toContain("Please try again later");
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });
});
