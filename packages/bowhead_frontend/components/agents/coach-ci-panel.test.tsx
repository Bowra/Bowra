import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import { authorizeCoachCI, cancelCoachCI, listCoachCI, startCoachCI, type CoachCIDelegation } from "@/lib/coach-ci-api";
import type { AgentEvalProgress } from "@/lib/agents-api";
import { CoachCIPanel, coachRequestView } from "./coach-ci-panel";

vi.mock("@/lib/coach-ci-api", () => ({ authorizeCoachCI: vi.fn(), cancelCoachCI: vi.fn(), listCoachCI: vi.fn(), startCoachCI: vi.fn() }));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ activeId: "ws_original", items: [{ id: "ws_original", name: "East China" }], loading: false, error: null, setActiveId: vi.fn(), refresh: vi.fn() }),
}));

const MONEY = /[¥$€]|USD|credit|额度|费用|金额/i;

const REQUEST: CoachCIDelegation = {
  delegation_id: "acig_original",
  agent_id: "osa_one",
  proposal_id: "acp_original",
  workspace_id: "ws_original",
  state: "requested",
  accepted_baseline: { working_copy_id: "awc_original", working_copy_version: 3 },
  row_version: 1,
  expires_at: null,
  current_admission: "consent_required",
  stop_reason: null,
  budget: { max_provider_calls: 120, max_comparisons: 2, duration_seconds: 1800 },
  remaining_provider_calls: 120,
  executions: [],
  usage: [],
};

const ESTIMATE: NonNullable<CoachCIDelegation["estimate"]> = {
  schema_version: 1,
  status: "available",
  unavailable_reason: null,
  per_comparison: { tokens: { low: 12_000, high: 18_000 }, tokens_cached: null, model_calls: { low: 90, high: 115 }, duration_ms: { low: 300_000, high: 540_000 } },
  max_comparisons: 2,
  model_call_cap: 120,
  tokens_ceiling_at_cap: 24_000,
  may_exhaust_call_cap: false,
  basis: { window_days: 30, scope: "agent", sample_attempts: 31 },
};

const PROGRESS: AgentEvalProgress = {
  schema_version: 1,
  cases: { completed: 10, total: 24 },
  attempts: { completed: 30, total: 72, in_flight: 1 },
  model_calls: { used: 63, basis: "dispatch_admissions", limit: 120, delegation_used: 63, estimated_total: null },
  usage: { tokens: 9800, tokens_input: 7000, tokens_output: 2800, tokens_cached: null, estimated_total_tokens: null, settled_only: false, partial: false },
  elapsed_ms: 200_000,
  eta_ms: null,
  as_of: "2026-10-05T06:18:12Z",
};

function renderPanel(props: Partial<ComponentProps<typeof CoachCIPanel>> = {}) {
  return renderIntl(<CoachCIPanel
    agentId="osa_one"
    liveSequence={7}
    proposals={[{
      kind: "agent_change_proposal", schema_version: 1, proposal_id: "acp_original", agent_id: "osa_one", source_type: "coach", source_ref: "c",
      rationale: "Lead with the conclusion", status: "accepted", row_version: 2, created_at: "2026-10-05T05:00:00Z", decided_at: null,
      operations: [{ op: "replace", path: "/definition/system_prompt" }, { op: "replace", path: "/definition/output_schema" }],
      atomic_groups: [], suggested_cases: [], base: { release_id: null, working_copy_id: null, working_copy_row_version: null },
    }]}
    suites={[{ key: "aes_1", name: "Quarterly business Q&A", count: 24 }]}
    expanded={null}
    onExpandedChange={vi.fn()}
    onPendingChange={vi.fn()}
    onPresenceChange={vi.fn()}
    onChanged={vi.fn(async () => {})}
    {...props}
  />, { locale: "en-US" });
}

describe("coachRequestView", () => {
  it.each([
    [{ state: "requested", current_admission: "consent_required" }, "consent"],
    [{ state: "requested", current_admission: "context_changed" }, "expired"],
    [{ state: "authorized", current_admission: "current_db_authority" }, "start_authorized"],
    [{ state: "authorized", current_admission: "expired" }, "expired"],
    [{ state: "authorized", executions: [{ id: "aex_1", status: "running", run_kind: "release", operation_id: "op" }] }, "running"],
    [{ state: "authorized", executions: [{ id: "aex_1", status: "completed", run_kind: "release", operation_id: "op" }] }, "completed"],
    [{ state: "revoked" }, "stopped"],
    [{ state: "revoked", stop_reason: "coach_ci_owner_cancelled" }, "stopped"],
    [{ state: "revoked", stop_reason: "coach_ci_delegation_expired" }, "expired"],
    [{ state: "exhausted" }, "exhausted"],
    [{ state: "unknown" }, "unknown"],
  ] as const)("maps %o to %s", (patch, view) => {
    expect(coachRequestView({ ...REQUEST, ...patch } as CoachCIDelegation)).toBe(view);
  });
});

describe("Coach evaluation consent", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listCoachCI).mockResolvedValue({ items: [{ ...REQUEST, estimate: ESTIMATE }], next_offset: null });
    vi.mocked(authorizeCoachCI).mockResolvedValue({ ...REQUEST, state: "authorized", row_version: 2 });
    vi.mocked(startCoachCI).mockResolvedValue({ delegation: REQUEST, execution_requested: true, replayed: false });
    vi.mocked(cancelCoachCI).mockResolvedValue({ ...REQUEST, state: "revoked" });
  });
  afterEach(cleanup);

  it("states the limits and estimated usage, then authorizes only after the confirmation restates them", async () => {
    const changed = vi.fn(async () => {});
    const onPendingChange = vi.fn();
    const view = renderPanel({ onChanged: changed, onPendingChange });
    const card = await screen.findByRole("group", { name: "Coach wants to evaluate this draft" });
    await waitFor(() => expect(onPendingChange).toHaveBeenLastCalledWith(1));
    expect(within(card).getByText("Accepted Coach proposal “Lead with the conclusion” (2 changes)")).toBeTruthy();
    expect(within(card).getByText("This change (your latest saved draft) vs released v7")).toBeTruthy();
    expect(within(card).getByText("Quarterly business Q&A · 24 cases")).toBeTruthy();
    expect(within(card).getByText("East China")).toBeTruthy();
    expect(within(card).getByText("120")).toBeTruthy();
    expect(within(card).getByText("30 min")).toBeTruthy();
    expect(within(card).getByText("Estimated token limit")).toBeTruthy();
    expect(within(card).getByText("About 24K")).toBeTruthy();
    expect(within(card).getByText("About 12K–18K tokens · 90–115 model requests · 5–9 min")).toBeTruthy();
    // No internal identifiers and no money anywhere.
    expect(view.container.textContent).not.toMatch(/acp_|acig_|ws_original|awc_/);
    expect(view.container.textContent).not.toMatch(MONEY);

    fireEvent.click(within(card).getByRole("button", { name: "Allow and start evaluation…" }));
    const dialog = await screen.findByRole("dialog", { name: "Allow Coach to start this evaluation?" });
    expect(dialog.textContent).toContain("Your latest saved draft runs the same cases as released v7.");
    expect(dialog.textContent).toMatch(/30 min \(until \d{2}:\d{2}\)/);
    expect(dialog.textContent).toContain("About 12K–18K tokens · 90–115 model requests · 5–9 min");
    expect(authorizeCoachCI).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(authorizeCoachCI).not.toHaveBeenCalled();

    fireEvent.click(within(card).getByRole("button", { name: "Allow and start evaluation…" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Allow and start" }));
    await waitFor(() => expect(startCoachCI).toHaveBeenCalledWith("osa_one", "acig_original"));
    expect(authorizeCoachCI).toHaveBeenCalledWith("osa_one", "acig_original", 1);
    expect(vi.mocked(authorizeCoachCI).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(startCoachCI).mock.invocationCallOrder[0]);
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  });

  it("says there is no usage estimate when run insights are off (no estimate field)", async () => {
    vi.mocked(listCoachCI).mockResolvedValue({ items: [REQUEST], next_offset: null });
    renderPanel();
    const card = await screen.findByRole("group", { name: "Coach wants to evaluate this draft" });
    expect(within(card).getByText("No usage estimate yet")).toBeTruthy();
    expect(within(card).queryByText("Estimated token limit")).toBeNull();
  });

  it("collapses on Not now and keeps the request waiting", async () => {
    const onExpandedChange = vi.fn();
    renderPanel({ onExpandedChange });
    fireEvent.click(await screen.findByRole("button", { name: "Not now" }));
    expect(onExpandedChange).toHaveBeenCalledWith(false);
    expect(cancelCoachCI).not.toHaveBeenCalled();
    expect(authorizeCoachCI).not.toHaveBeenCalled();
  });

  it("recovers an unknown consent response without automatically starting or authorizing again", async () => {
    vi.mocked(authorizeCoachCI).mockRejectedValueOnce(new Error("Response lost"));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Allow and start evaluation…" }));
    vi.mocked(listCoachCI).mockResolvedValue({
      items: [{ ...REQUEST, state: "authorized", row_version: 2, current_admission: "current_db_authority", expires_at: "2026-10-05T06:52:00Z" }],
      next_offset: null,
    });
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Allow and start" }));
    expect((await screen.findByRole("alert")).textContent).toContain("outcome couldn't be confirmed");
    const recovered = await screen.findByRole("button", { name: "Start authorized evaluation" });
    expect(screen.getByText("Consent expires")).toBeTruthy();
    expect(startCoachCI).not.toHaveBeenCalled();
    fireEvent.click(recovered);
    await waitFor(() => expect(startCoachCI).toHaveBeenCalledTimes(1));
    expect(authorizeCoachCI).toHaveBeenCalledTimes(1);
  });

  it("reports a known admission failure without describing it as an unknown send", async () => {
    vi.mocked(authorizeCoachCI).mockRejectedValueOnce(new ApiError(422, "Context changed"));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Allow and start evaluation…" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Allow and start" }));
    expect((await screen.findByRole("alert")).textContent).toContain("can't continue right now");
    expect(startCoachCI).not.toHaveBeenCalled();
  });

  it("shows running progress against the limits and stops only after the exact-consequence confirmation", async () => {
    vi.mocked(listCoachCI).mockResolvedValue({
      items: [{
        ...REQUEST,
        state: "authorized",
        current_admission: "current_db_authority",
        remaining_provider_calls: 57,
        expires_at: new Date(Date.now() + 18 * 60_000).toISOString(),
        executions: [{ id: "aex_1", status: "running", run_kind: "release", operation_id: "op_1", progress: PROGRESS }],
        usage: [{ state: "settled", count: 63, tokens_input: 7000, tokens_output: 2800 }],
      }],
      next_offset: null,
    });
    const view = renderPanel();
    const card = await screen.findByRole("group", { name: "Coach evaluation running" });
    expect(within(card).getByText("Running case 11 / 24")).toBeTruthy();
    expect(within(card).getByRole("progressbar", { name: "Model requests 63 / 120" })).toBeTruthy();
    expect(within(card).getByText("1 / 2")).toBeTruthy();
    expect(view.container.textContent).not.toMatch(MONEY);

    fireEvent.click(within(card).getByRole("button", { name: "Stop evaluation…" }));
    const dialog = await screen.findByRole("dialog", { name: "Stop this evaluation?" });
    expect(dialog.textContent).toContain("The running comparison will be cancelled. What was already used (9,800 tokens, 63 model requests) is not refunded.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep running" }));
    expect(cancelCoachCI).not.toHaveBeenCalled();
    fireEvent.click(within(card).getByRole("button", { name: "Stop evaluation…" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Stop evaluation" }));
    await waitFor(() => expect(cancelCoachCI).toHaveBeenCalledWith("osa_one", "acig_original"));
  });

  it("does not offer execution for stale or unknown authority", async () => {
    vi.mocked(listCoachCI).mockResolvedValue({
      items: [{ ...REQUEST, state: "unknown", current_admission: "coach_ci_invocation_unknown" }],
      next_offset: null,
    });
    renderPanel();
    await screen.findByText("Couldn't confirm whether the evaluation started");
    expect(screen.getByRole("button", { name: "Refresh status" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /start/i })).toBeNull();
  });

  it("shows nothing (and reports no presence) when Coach has no requests", async () => {
    vi.mocked(listCoachCI).mockResolvedValue({ items: [], next_offset: null });
    const onPresenceChange = vi.fn();
    const rendered = renderPanel({ onPresenceChange });
    await waitFor(() => expect(listCoachCI).toHaveBeenCalled());
    await waitFor(() => expect(onPresenceChange).toHaveBeenLastCalledWith(false));
    expect(rendered.container.textContent).toBe("");
  });

  it("hides the disabled feature after an unavailable response", async () => {
    vi.mocked(listCoachCI).mockRejectedValue(new ApiError(404, "Disabled"));
    const onPendingChange = vi.fn();
    const rendered = renderPanel({ onPendingChange });
    await waitFor(() => expect(rendered.container.textContent).toBe(""));
    expect(onPendingChange).toHaveBeenLastCalledWith(0);
  });
});
