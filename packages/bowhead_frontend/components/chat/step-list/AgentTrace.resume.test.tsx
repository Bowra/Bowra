/** A Continue that could not pick up saved work says so on the helper's step row. */
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { AgentTrace } from "./AgentTrace";
import type { StepView } from "./types";

const spawn: StepView = {
  stepId: "spawn-1",
  parentStepId: null,
  concurrencyGroupId: null,
  stepType: "tool_run",
  toolName: "subagent_spawn",
  headerZh: "Analyse sales",
  label: "Analyse sales",
  state: "done",
  summary: "",
  durationText: "4s",
  detail: null,
  peerId: "sub_a",
} as StepView;

describe("resume refusal on the step row", () => {
  it("explains that the helper started over from the original task", () => {
    renderIntl(
      <AgentTrace
        steps={[spawn]}
        peerNames={{ sub_a: "compute" }}
        peerProse={{}}
        peerTerminalById={{ sub_a: { status: "succeeded", resumeRefused: "tools_changed" } }}
      />,
      { locale: "en-US" },
    );
    const text = "This Agent's tools changed after its earlier run, so it started over from the original task instead of picking up its saved work.";
    if (!screen.queryByText(text)) fireEvent.click(screen.getAllByRole("button")[0]);
    expect(screen.getAllByText(text).length).toBeGreaterThan(0);
  });
});
