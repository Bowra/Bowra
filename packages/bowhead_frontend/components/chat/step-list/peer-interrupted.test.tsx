/**
 * A peer whose runner died with the process (service restart) never emits its
 * own terminal fact. Once the turn itself has ended, its block must stop
 * spinning; while the turn is running it keeps the live "run" state.
 * A restart-interrupted peer must render its terminal state consistently.
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "@/messages/zh-CN.json";

import { AgentTrace } from "./AgentTrace";
import { TurnRunningProvider } from "./turn-running";
import type { StepView } from "./types";

afterEach(() => cleanup());

function spawnStillActive(): StepView {
  return {
    stepId: "s_spawn",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "subagent_spawn",
    headerZh: "派 compute",
    state: "active",
    label: "query provinces",
    subagentId: "LEAD",
    peerProseKey: "sub_x",
  } as unknown as StepView;
}

function mount(running: boolean) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <TurnRunningProvider running={running}>
        <AgentTrace steps={[spawnStillActive()]} peerNames={{ sub_x: "compute" }} peerProse={{}} />
      </TurnRunningProvider>
    </NextIntlClientProvider>,
  );
}

describe("peer without a terminal fact", () => {
  it("keeps the live run state while the turn is running", () => {
    const { container } = mount(true);
    expect(container.querySelector(".agb")?.classList.contains("is-run")).toBe(true);
  });

  it("stops spinning once the turn has ended", () => {
    const { container } = mount(false);
    const block = container.querySelector(".agb");
    expect(block).not.toBeNull();
    expect(block?.classList.contains("is-run")).toBe(false);
  });
});
