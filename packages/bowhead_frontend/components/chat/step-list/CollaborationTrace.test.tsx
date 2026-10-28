import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { apiJson } from "@/lib/api-client";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "@/lib/turn-store";
import { dispatchToTurnStore, _resetNarrativeCursorsForTesting } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";
import { setActiveSpaceId } from "@/lib/space-storage";
import { CANVAS_FOCUS_DATA_EVENT } from "@/lib/canvas-workspace-focus";
import { CollaborationTrace } from "./CollaborationTrace";
import { CollaborationTraceProvider, collaborationTopicState, type CollaborationProjection } from "./collaboration-trace-state";

vi.mock("@/lib/api-client", async (importOriginal) => ({ ...await importOriginal<typeof import("@/lib/api-client")>(), apiJson: vi.fn() }));
const read = vi.mocked(apiJson);
const turnId = "turn-collaboration";
const projection: CollaborationProjection = {
  turn_id: turnId, revision: 1, turn_status: "running",
  participants: [
    { execution_id: "one", profile: "compute", is_owner: false, status: "running", waiting: true },
    { execution_id: "two", profile: "compute", is_owner: false, status: "running", waiting: false },
  ],
  topics: [{ topic_id: "question", sender_id: "one", recipient_id: "two", required: true, response_status: "answered", dependency_status: "open",
    messages: [{ operation_id: "reply", action: "reply", sender_id: "two", recipient_id: "one", body: "Refunds exclude shipping." }],
  }],
};

function event(seq: number) {
  return { card_id: turnId, collaboration_revision: seq, evt_seq: seq, body: "Must not enter replay state" };
}
function renderTrace() {
  return renderIntl(<CollaborationTraceProvider turnId={turnId}><CollaborationTrace /><input aria-label="Draft" defaultValue="Keep my draft" /></CollaborationTraceProvider>, { locale: "en-US" });
}
beforeEach(() => { read.mockReset(); _resetAllTurnsForTesting(); _resetNarrativeCursorsForTesting(); setActiveSpaceId("space-one"); });
afterEach(cleanup);

it("replays only committed revisions, without private bodies or false completion", () => {
  dispatchToTurnStore("agent.collaboration", event(2));
  const live = getTurnSnapshot(turnId);
  expect(live?.collaborationRevision).toBe(2);
  expect(live?.cardFinalState).toBeNull();
  expect(JSON.stringify(live)).not.toContain("Must not enter");
  _resetAllTurnsForTesting(); _resetNarrativeCursorsForTesting();
  replayTurnEventRows(turnId, [{ seq: 2, event_type: "agent.collaboration", payload: event(2), occurred_at: "" }]);
  dispatchToTurnStore("agent.collaboration", event(1));
  expect(getTurnSnapshot(turnId)).toEqual(live);
});

it("keeps replies unverified and preserves expansion, focus and the draft across revisions", async () => {
  read.mockResolvedValue(projection);
  dispatchToTurnStore("agent.collaboration", event(1));
  const view = renderTrace();
  await screen.findByText("Reply received · awaiting verification");
  expect(screen.queryByText("Dependency verified")).toBeNull();
  const details = view.container.querySelector("details")!;
  const summary = details.querySelector("summary")!;
  details.open = true;
  summary.focus();
  expect(summary.textContent).toMatch(/1.*2/);
  read.mockResolvedValue({ ...projection, revision: 2, topics: [{ ...projection.topics[0], dependency_status: "resolved" }] });
  act(() => { dispatchToTurnStore("agent.collaboration", event(2)); });
  await screen.findByText("Dependency verified");
  expect(view.container.querySelector("details")).toBe(details);
  expect(details.open).toBe(true);
  expect(document.activeElement).toBe(summary);
  expect(screen.getByRole("textbox", { name: "Draft" })).toHaveValue("Keep my draft");
});

it("removes revoked content and ignores a late response from the previous Space", async () => {
  read.mockResolvedValueOnce(projection);
  dispatchToTurnStore("agent.collaboration", event(1));
  renderTrace();
  await screen.findByText("Refunds exclude shipping.");
  let late!: (value: CollaborationProjection) => void;
  read.mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(screen.queryByText("Refunds exclude shipping.")).toBeNull();
  read.mockRejectedValueOnce(new Error("Forbidden"));
  act(() => { setActiveSpaceId("space-two"); window.dispatchEvent(new Event("bowhead:workspace-changed")); });
  await screen.findByText("Collaboration details are unavailable. Refresh to try again.");
  await act(async () => { late(projection); });
  expect(screen.queryByText("Refunds exclude shipping.")).toBeNull();
  expect(screen.queryByText("Dependency verified")).toBeNull();
});

it("does not fetch ordinary historical turns and stops claiming a reply is pending after termination", async () => {
  renderTrace();
  await waitFor(() => expect(read).not.toHaveBeenCalled());
  expect(collaborationTopicState({ ...projection.topics[0], required: false, response_status: "pending" }, true)).toBe("unfinished");
  expect(collaborationTopicState(projection.topics[0], true)).toBe("unresolved");
  expect(collaborationTopicState({ ...projection.topics[0], response_status: "cancelled", dependency_status: "cancelled" }, true)).toBe("unresolved");
});

it("opens evidence through the existing Results surface only after an explicit click", async () => {
  const open = vi.fn();
  window.addEventListener(CANVAS_FOCUS_DATA_EVENT, open);
  try {
    read.mockResolvedValue({ ...projection, topics: [{ ...projection.topics[0], messages: [{ ...projection.topics[0].messages[0], references: [{ identity: "art_result", version: "art_result" }] }] }] });
    dispatchToTurnStore("agent.collaboration", event(1));
    const view = renderTrace();
    await screen.findByText("Reply received · awaiting verification");
    view.container.querySelector("details")!.open = true;
    expect(open).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Evidence 1" }));
    expect(open).toHaveBeenCalledOnce();
    expect((open.mock.calls[0][0] as CustomEvent).detail).toMatchObject({ artifactId: "art_result", entryPoint: "chat" });
  } finally {
    window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, open);
  }
});

it.each(["completed", "failed", "cancelled"] as const)("settles collaboration from done alone despite a stale details response: %s", async (status) => {
  const pending: CollaborationProjection = { ...projection, topics: [
    { ...projection.topics[0], response_status: "pending" },
    { ...projection.topics[0], topic_id: "verified", dependency_status: "resolved" },
  ] };
  read.mockResolvedValueOnce(pending);
  dispatchToTurnStore("agent.collaboration", event(1));
  renderTrace();
  await screen.findByText("Awaiting reply");
  let finishRead!: (value: CollaborationProjection) => void;
  read.mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve; }));
  act(() => { dispatchToTurnStore("done", { turn_id: turnId, status, seq: 2 }); });
  await screen.findByText("Ended with an unresolved dependency");
  expect(screen.getByText("Dependency verified")).toBeInTheDocument();
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  await act(async () => { finishRead(pending); });
  expect(screen.queryByText("Awaiting reply")).toBeNull();
  expect(screen.getByText("Ended with an unresolved dependency")).toBeInTheDocument();
  expect(getTurnSnapshot(turnId)?.cardFinalState).toBe(status);

  cleanup();
  _resetAllTurnsForTesting(); _resetNarrativeCursorsForTesting();
  replayTurnEventRows(turnId, [
    { seq: 1, event_type: "agent.collaboration", payload: event(1), occurred_at: "" },
    { seq: 2, event_type: "done", payload: { turn_id: turnId, status, seq: 2 }, occurred_at: "" },
  ]);
  read.mockResolvedValue({ ...pending, turn_status: status });
  renderTrace();
  await screen.findByText("Ended with an unresolved dependency");
  expect(getTurnSnapshot(turnId)?.cardFinalState).toBe(status);

  read.mockResolvedValue(pending);
  act(() => { dispatchToTurnStore("turn.resumed", { turn_id: turnId, seq: 3 }); });
  await screen.findByText("Awaiting reply");
  expect(getTurnSnapshot(turnId)?.cardFinalState).toBeNull();
});

it.each([
  { status: "clarification_pending" },
  { status: "completed", parent_agent_id: "specialist" },
])("does not finish the parent on a pause or child terminal: %j", async (terminal) => {
  read.mockResolvedValue(projection);
  dispatchToTurnStore("agent.collaboration", event(1));
  renderTrace();
  await screen.findByText("Reply received · awaiting verification");
  act(() => { dispatchToTurnStore("done", { turn_id: turnId, ...terminal, seq: 2 }); });
  expect(getTurnSnapshot(turnId)?.cardFinalState).toBeNull();
  expect(screen.queryByText("Ended with an unresolved dependency")).toBeNull();
});
