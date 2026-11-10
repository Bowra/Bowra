import "@testing-library/jest-dom/vitest";

import { useState } from "react";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { StepConfirmation } from "@/lib/turn-store";

const mocks = vi.hoisted(() => ({ viewerId: "u_owner", getDetail: vi.fn() }));

vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { id: mocks.viewerId } }),
}));
vi.mock("@/lib/tool-confirmation-api", () => ({
  getToolConfirmation: mocks.getDetail,
  decideToolConfirmation: vi.fn(),
}));

import { PendingToolConfirmations } from "../tool-confirmation/PendingToolConfirmations";
import { AgentBlock } from "./AgentBlock";
import { AgentTrace } from "./AgentTrace";
import type { StepView } from "./types";

function confirmation(over: Partial<StepConfirmation> = {}): StepConfirmation {
  return {
    id: "ttc_one",
    kind: "team_mcp",
    requesterUserId: "u_owner",
    title: "migrate_pages_to_workers_guide",
    sourceLabel: "Cloudflare Docs",
    category: "unknown",
    choices: ["reject", "approve", "approve_session"],
    expiresAt: "2026-11-08T10:03:00Z",
    timeoutSeconds: 180,
    receivedAt: Date.now(),
    ...over,
  };
}

function step(over: Partial<StepView>): StepView {
  return {
    stepId: "step_call_1",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "teammcp.cloudflare.migrate" as StepView["toolName"],
    headerZh: "Team tool",
    label: "migrate_pages_to_workers_guide",
    state: "active",
    ...over,
  } as StepView;
}

beforeEach(() => {
  mocks.viewerId = "u_owner";
  mocks.getDetail.mockReturnValue(new Promise(() => undefined));
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("confirmation card in the trace", () => {
  it("renders below the waiting row, outside its collapsible detail, and says it is waiting for you", () => {
    const { container } = renderIntl(
      <AgentTrace steps={[step({ confirmation: confirmation() })]} peerNames={{}} peerProse={{}} />,
    );
    const card = container.querySelector("[data-tool-confirmation-id='ttc_one']");
    expect(card).not.toBeNull();
    expect(card?.closest(".stp")).toBeNull();
    expect(card?.previousElementSibling?.classList.contains("stp")).toBe(true);
    expect(screen.getByText("等你确认")).toBeInTheDocument();
    expect(mocks.getDetail).toHaveBeenCalledWith("ttc_one");
  });

  it("tells other viewers it waits for the requester", () => {
    mocks.viewerId = "u_teammate";
    renderIntl(<AgentTrace steps={[step({ confirmation: confirmation() })]} peerNames={{}} peerProse={{}} />);
    expect(screen.getByText("等待确认")).toBeInTheDocument();
    expect(screen.getByText("等待发起人确认")).toBeInTheDocument();
    expect(mocks.getDetail).not.toHaveBeenCalled();
  });

  it("keeps the one-line record after the decision", () => {
    renderIntl(
      <AgentTrace
        steps={[step({ state: "done", confirmation: confirmation({ outcome: "expired" }) })]}
        peerNames={{}}
        peerProse={{}}
      />,
    );
    expect(screen.getByText("已过期，未执行")).toBeInTheDocument();
    expect(screen.queryByText("等你确认")).toBeNull();
  });

  it("opens the Agent block holding a pending confirmation", () => {
    renderIntl(
      <AgentTrace
        steps={[step({ subagentId: "peer_a", confirmation: confirmation() })]}
        peerNames={{ peer_a: "compute" }}
        peerProse={{}}
      />,
    );
    expect(document.querySelector("[data-tool-confirmation-id='ttc_one']")).not.toBeNull();
  });

  it("re-opens a collapsed Agent block when a confirmation arrives", () => {
    function Harness() {
      const [force, setForce] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setForce(true)}>arrive</button>
          <AgentBlock agent="compute" status="run" forceOpen={force}>
            <p>inside</p>
          </AgentBlock>
        </>
      );
    }
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { expanded: true }));
    expect(screen.queryByText("inside")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "arrive" }));
    expect(screen.getByText("inside")).toBeInTheDocument();
  });
});

describe("PendingToolConfirmations (collapsed running band)", () => {
  it("shows only the pending cards, named after their Agent", () => {
    mocks.viewerId = "u_teammate";
    const { container } = renderIntl(
      <PendingToolConfirmations
        steps={[
          step({ stepId: "a", subagentId: "peer_a", confirmation: confirmation({ id: "ttc_a" }) }),
          step({ stepId: "b", confirmation: confirmation({ id: "ttc_b", outcome: "approved" }) }),
          step({ stepId: "c" }),
        ]}
        peerNames={{ peer_a: "compute" }}
      />,
    );
    expect([...container.querySelectorAll("[data-tool-confirmation-id]")].map((el) => el.getAttribute("data-tool-confirmation-id"))).toEqual(["ttc_a"]);
  });

  it("renders nothing without a pending confirmation", () => {
    const { container } = renderIntl(<PendingToolConfirmations steps={[step({})]} />);
    expect(container.innerHTML).toBe("");
  });
});
