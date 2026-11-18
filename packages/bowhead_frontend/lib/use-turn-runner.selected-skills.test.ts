import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useTurnRunner, type UseTurnRunnerArgs } from "./use-turn-runner";
import { singleAttemptChatActionFetch } from "./composer-chat-action";
import {
  captureReferenceSubmissionDraft,
  clearPendingSubmission,
  preparePendingReferenceSubmission,
  type ComposerSubmissionWireSnapshot,
} from "./composer-submission-recovery";
import { skillSelectionFromInputDocument } from "./composer-selected-skills";
import type { InputDocument } from "./composer-input-document";
import fixtures from "../../../contracts/chat-references/input-document-v1.json";

vi.mock("./composer-chat-action", async (importOriginal) => {
  const original = await importOriginal<typeof import("./composer-chat-action")>();
  return { ...original, singleAttemptChatActionFetch: vi.fn(original.singleAttemptChatActionFetch) };
});
vi.mock("./composer-submission-recovery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./composer-submission-recovery")>()),
  preparePendingReferenceSubmission: vi.fn(),
  clearPendingSubmission: vi.fn(async () => undefined),
}));

const source = fixtures.valid.find((value) => value.name === "same_label_different_skill_sources")!;
const document = source.normalized as InputDocument;
const selectedSkills = skillSelectionFromInputDocument(document)!;
const identity = {
  clientSubmissionId: "00000000-0000-4000-8000-000000000001",
  contentChecksum: "test-checksum",
};
const scope = {
  teamId: "team_ref",
  userId: "u_ref",
  workspaceId: "ws_ref",
  requestedSessionId: null,
  authSessionGeneration: "original",
};
const wire: ComposerSubmissionWireSnapshot = {
  composerSurface: "home",
  userProfile: { language: "zh-CN" },
  preferredProvider: "p_1",
  preferredModel: "m_1",
  planningMode: "off",
  selectedSkills,
};
const base: UseTurnRunnerArgs = {
  mode: "sse",
  message: source.display_text,
  workspaceId: "ws_ref",
  sendTick: 1,
  sendAttemptKey: "send-original",
  currentChatScopeKey: "original",
  referenceInputDocument: document,
  referenceSourceDraftRevision: "rev_original",
  referenceSubmissionEnabled: true,
  submissionProtocolEnabled: true,
  submissionRecoveryScope: scope,
  submissionWireSnapshot: wire,
};
const started = {
  turn_id: "t_skill",
  session_id: "s_skill",
  stream_url: "/api/v1/chat/stream/t_skill",
};
const originalFetch = globalThis.fetch;

function postBodies(): Record<string, unknown>[] {
  return vi.mocked(fetch).mock.calls
    .filter(([input, init]) => String(input).includes("/api/v1/chat/send") && init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body)));
}

beforeEach(() => {
  vi.mocked(preparePendingReferenceSubmission).mockReset().mockResolvedValue(identity);
  vi.mocked(clearPendingSubmission).mockClear();
  vi.mocked(singleAttemptChatActionFetch).mockClear();
  globalThis.fetch = vi.fn(async () => new Response("{}", { status: 404 }));
});
afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

it("sends a Skill-only start as an ordinary submission and clears its reference draft", async () => {
  globalThis.fetch = vi.fn(async (_input, init) => init?.method === "POST"
    ? new Response(JSON.stringify(started))
    : new Response("{}", { status: 404 }));
  const order: string[] = [];
  vi.mocked(clearPendingSubmission).mockImplementationOnce(async () => { order.push("journal"); });
  const accepted = vi.fn(async () => { order.push("draft"); return true; });
  const settled = vi.fn();
  const cleared = vi.fn();
  const hook = renderHook(() => useTurnRunner({
    ...base,
    onReferenceSubmissionAccepted: accepted,
    onSubmissionCleared: cleared,
    onSendInitSettled: settled,
  }));
  await waitFor(() => expect(settled).toHaveBeenCalledWith(
    "send-original", true, { turnId: "t_skill", sessionId: "s_skill" },
  ));

  const bodies = postBodies();
  expect(bodies).toHaveLength(1);
  expect(bodies[0]).toMatchObject({
    message: source.display_text,
    client_submission_id: identity.clientSubmissionId,
    submission_kind: "start",
    submission_contract_version: "chat-submit.v1",
    model_control_batch: "a",
    selected_skills: selectedSkills,
    user_profile: { language: "zh-CN" },
    preferred_provider: "p_1",
    preferred_model: "m_1",
    planning_mode: "off",
  });
  expect(bodies[0]).not.toHaveProperty("input_document");
  expect(bodies[0].client_info).not.toHaveProperty("agent_draft_id");
  expect(bodies[0].client_info).not.toHaveProperty("request_id");
  const [, init] = vi.mocked(fetch).mock.calls.find(([, call]) => call?.method === "POST")!;
  expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
  expect(singleAttemptChatActionFetch).not.toHaveBeenCalled();
  // The journal keeps the reference source with the frozen selection.
  expect(vi.mocked(preparePendingReferenceSubmission).mock.calls[0][1]).toMatchObject({
    input_document: document,
    wireSnapshot: { selectedSkills },
  });
  expect(accepted).toHaveBeenCalledWith("send-original", "original");
  expect(order).toEqual(["draft", "journal"]);
  expect(cleared).toHaveBeenCalledOnce();
  expect(hook.result.current.turnId).toBe("t_skill");
});

it("resends the frozen Skills and identity when a recovered submission is retried", async () => {
  globalThis.fetch = vi.fn(async (_input, init) => {
    if (init?.method === "POST") throw new TypeError("network down");
    return new Response("{}", { status: 404 });
  });
  const firstSettled = vi.fn();
  const first = renderHook(() => useTurnRunner({ ...base, onSendInitSettled: firstSettled }));
  await waitFor(() => expect(firstSettled).toHaveBeenCalledWith("send-original", false));
  // An unconfirmed send keeps its journal for explicit recovery.
  expect(clearPendingSubmission).not.toHaveBeenCalled();
  first.unmount();

  // The recovered record carries the stored envelope: the source copy plus the frozen choice.
  const recoveredWire = {
    ...structuredClone(wire),
    schemaVersion: 2,
    input_document: structuredClone(document),
  } as ComposerSubmissionWireSnapshot;
  globalThis.fetch = vi.fn(async (_input, init) => init?.method === "POST"
    ? new Response(JSON.stringify(started))
    : new Response("{}", { status: 404 }));
  const retrySettled = vi.fn();
  renderHook(() => useTurnRunner({
    ...base,
    sendAttemptKey: "send-retry",
    submissionWireSnapshot: recoveredWire,
    onReferenceSubmissionAccepted: async () => true,
    onSendInitSettled: retrySettled,
  }));
  await waitFor(() => expect(retrySettled).toHaveBeenCalledWith(
    "send-retry", true, { turnId: "t_skill", sessionId: "s_skill" },
  ));
  const [retry] = postBodies();
  expect(retry).toMatchObject({
    client_submission_id: identity.clientSubmissionId,
    model_control_batch: "a",
    selected_skills: selectedSkills,
  });
  expect(retry).not.toHaveProperty("input_document");
  expect(vi.mocked(preparePendingReferenceSubmission).mock.calls[1][1].wireSnapshot.selectedSkills)
    .toEqual(selectedSkills);
});

it("keeps a recovered record without a selection on the reference wire", async () => {
  const { selectedSkills: _skills, ...legacyWire } = wire;
  globalThis.fetch = vi.fn(async (_input, init) => init?.method === "POST"
    ? new Response(JSON.stringify({ ...started, status: "started", execution_generation: 1 }))
    : new Response("{}", { status: 404 }));
  const settled = vi.fn();
  renderHook(() => useTurnRunner({
    ...base,
    submissionWireSnapshot: { ...legacyWire, schemaVersion: 2, input_document: document } as ComposerSubmissionWireSnapshot,
    onReferenceSubmissionAccepted: async () => true,
    onSendInitSettled: settled,
  }));
  await waitFor(() => expect(settled).toHaveBeenCalledWith(
    "send-original", true, { turnId: "t_skill", sessionId: "s_skill" },
  ));
  const [body] = postBodies();
  expect(body).toMatchObject({ model_control_batch: "references", input_document: document });
  expect(body).not.toHaveProperty("selected_skills");
});

it.each([
  ["a different version", [{ ...selectedSkills[0], version: `sha256:${"b".repeat(64)}` }, selectedSkills[1]]],
  ["a missing Skill", selectedSkills.slice(0, 1)],
  ["a different order", [...selectedSkills].reverse()],
])("refuses a frozen selection with %s before any journal or HTTP request", async (_label, frozen) => {
  expect(() => captureReferenceSubmissionDraft(scope, {
    schemaVersion: 2,
    input_document: document,
    message: source.display_text,
    attachments: [],
    wireSnapshot: { ...wire, selectedSkills: frozen },
  })).toThrow();
  const settled = vi.fn();
  renderHook(() => useTurnRunner({
    ...base,
    submissionWireSnapshot: { ...wire, selectedSkills: frozen },
    onSendInitSettled: settled,
  }));
  await waitFor(() => expect(settled).toHaveBeenCalledWith("send-original", false));
  expect(preparePendingReferenceSubmission).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
});

it.each([
  "skill_selection_unavailable",
  "selected_skill_unavailable",
  "selected_skill_changed",
  "selected_skill_too_large",
])("releases the journal and keeps the draft when admission refuses with %s", async (code) => {
  globalThis.fetch = vi.fn(async () => new Response(
    JSON.stringify({ detail: { error: { code, admission: "not_started" } } }),
    { status: 409 },
  ));
  const accepted = vi.fn(async () => true);
  const settled = vi.fn();
  const cleared = vi.fn();
  const hook = renderHook(() => useTurnRunner({
    ...base,
    onReferenceSubmissionAccepted: accepted,
    onSubmissionCleared: cleared,
    onSendInitSettled: settled,
  }));
  await waitFor(() => expect(hook.result.current.sendErrorReason).toBe(code));
  expect(settled).toHaveBeenCalledWith("send-original", false);
  expect(clearPendingSubmission).toHaveBeenCalledWith(scope, identity.clientSubmissionId);
  expect(cleared).toHaveBeenCalledOnce();
  expect(accepted).not.toHaveBeenCalled();
});
