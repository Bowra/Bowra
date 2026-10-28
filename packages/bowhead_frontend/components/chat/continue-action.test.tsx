/**
 * Partial-answer footer with Continue (agent run limits, phase 2 §6): shown only for helpers that
 * stopped partway and saved their work; one click sends their checkpoint ids and disables the button.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ContinueAction, continueTargets } from "./continue-action";
import type { PeerTerminal } from "@/lib/turn-store";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vals?: Record<string, unknown>) => (vals ? `${key}:${Object.values(vals).join("|")}` : key),
}));

afterEach(cleanup);

const target = { checkpointId: "rsc_a", agent: "Data analyst", stopReason: "time_limit" };

describe("continueTargets", () => {
  it("keeps partial helpers with saved work, in settle order, with their names", () => {
    const peers: Record<string, PeerTerminal> = {
      sub_b: { status: "partial", resumeCheckpointId: "rsc_b", stopReason: "step_limit", evtSeq: 9 },
      sub_a: { status: "partial", resumeCheckpointId: "rsc_a", stopReason: "time_limit", evtSeq: 3 },
      sub_done: { status: "succeeded", resumeCheckpointId: "rsc_done", evtSeq: 1 },
      sub_unsaved: { status: "partial", evtSeq: 2 },
    };
    const targets = continueTargets(peers, { sub_a: "compute", sub_b: "kb-analyst" }, { sub_a: "Sales desk" });
    expect(targets.map((t) => [t.checkpointId, t.stopReason])).toEqual([["rsc_a", "time_limit"], ["rsc_b", "step_limit"]]);
    expect(targets[0].agent).toBe("Sales desk");
    expect(continueTargets(undefined)).toEqual([]);
  });
});

describe("ContinueAction", () => {
  it("renders nothing without saved work", () => {
    const { container } = render(<ContinueAction targets={[]} onContinue={vi.fn()} />);
    expect(container.innerHTML).toBe("");
  });

  it("names the agent and its limit, sends the ids once and stays disabled", async () => {
    const onContinue = vi.fn(async () => true);
    render(<ContinueAction targets={[target]} onContinue={onContinue} />);
    expect(screen.getByText("partial_footer.body:Data analyst|partial_footer.reason.time_limit")).toBeTruthy();
    const button = screen.getByRole("button", { name: "partial_footer.continue" });
    await act(async () => {
      fireEvent.click(button);
    });
    expect(onContinue).toHaveBeenCalledWith(["rsc_a"]);
    expect(button).toHaveProperty("disabled", true);
  });

  it("summarizes several stopped agents", () => {
    render(<ContinueAction targets={[target, { ...target, checkpointId: "rsc_b" }]} onContinue={vi.fn()} />);
    expect(screen.getByText("partial_footer.body_many:2")).toBeTruthy();
  });

  it("lets the user try again when the send was refused", async () => {
    const onContinue = vi.fn(async () => false);
    render(<ContinueAction targets={[target]} onContinue={onContinue} />);
    const button = screen.getByRole("button", { name: "partial_footer.continue" });
    await act(async () => {
      fireEvent.click(button);
    });
    expect(button).toHaveProperty("disabled", false);
  });
});
