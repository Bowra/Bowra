import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useTurnRunner, type UseTurnRunnerArgs } from "./use-turn-runner";
import { singleAttemptChatActionFetch } from "./composer-chat-action";
import {
  captureReferenceSubmissionDraft,
  clearPendingSubmission,
  preparePendingReferenceSubmission,
} from "./composer-submission-recovery";
import type { InputDocument } from "./composer-input-document";
import fixtures from "../../../contracts/chat-references/input-document-v1.json";

// Pass-through spy: behaviour unchanged, so a test can see which deadline the @ send asked for.
vi.mock("./composer-chat-action", async (importOriginal) => {
  const original = await importOriginal<typeof import("./composer-chat-action")>();
  return { ...original, singleAttemptChatActionFetch: vi.fn(original.singleAttemptChatActionFetch) };
});
vi.mock("./composer-submission-recovery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./composer-submission-recovery")>()),
  preparePendingReferenceSubmission: vi.fn(),
  clearPendingSubmission: vi.fn(async () => undefined),
}));
const source = fixtures.valid.find(
  (value) => value.name === "exact_agent_and_parameterized_skill",
)!;
const identity = {
  clientSubmissionId: "00000000-0000-4000-8000-000000000001",
  contentChecksum: "test-checksum",
};
const base: UseTurnRunnerArgs = {
  mode: "sse",
  message: source.display_text,
  workspaceId: "ws_ref",
  sendTick: 1,
  sendAttemptKey: "send-original",
  currentChatScopeKey: "original",
  referenceInputDocument: source.normalized as InputDocument,
  referenceSourceDraftRevision: "rev_original",
  referenceSubmissionEnabled: true,
  submissionProtocolEnabled: true,
  submissionRecoveryScope: {
    teamId: "team_ref",
    userId: "u_ref",
    workspaceId: "ws_ref",
    requestedSessionId: null,
    authSessionGeneration: "original",
  },
  submissionWireSnapshot: {
    composerSurface: "home",
    userProfile: null,
    preferredProvider: null,
    preferredModel: null,
    planningMode: "off",
  },
};
const originalFetch = globalThis.fetch;
beforeEach(() => {
  vi.mocked(preparePendingReferenceSubmission)
    .mockReset()
    .mockResolvedValue(identity);
  vi.mocked(clearPendingSubmission).mockClear();
  globalThis.fetch = vi.fn(async () => new Response("{}", { status: 404 }));
});
afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

it("rejects a recovered wire with a different source before reusing a submission", () => {
  const root = structuredClone(base.referenceInputDocument!);
  const other = structuredClone(root);
  const skill = other.nodes.find(
    (node) => node.type === "reference" && node.resource.kind === "skill",
  )!;
  if (skill.type === "reference") skill.parameters.period = "2026Q4";
  expect(() =>
    captureReferenceSubmissionDraft(base.submissionRecoveryScope!, {
      schemaVersion: 2,
      input_document: root,
      message: base.message!,
      attachments: [],
      wireSnapshot: {
        ...base.submissionWireSnapshot!,
        schemaVersion: 2,
        input_document: other,
      } as typeof base.submissionWireSnapshot & {
        schemaVersion: number;
        input_document: InputDocument;
      },
    }),
  ).toThrow();
  expect(root).toEqual(base.referenceInputDocument);
});

it("does not adopt an acknowledgement for a different existing session", async () => {
  const settled = vi.fn();
  globalThis.fetch = vi.fn(async (_input, init) =>
    init?.method === "POST"
      ? new Response(
          JSON.stringify({
            status: "started",
            session_id: "s_other",
            turn_id: "t_other",
            execution_generation: 1,
            stream_url: "/api/v1/chat/stream/t_other",
          }),
        )
      : new Response("{}", { status: 404 }),
  );
  const hook = renderHook(() =>
    useTurnRunner({
      ...base,
      activeSessionId: "s_original",
      submissionRecoveryScope: {
        ...base.submissionRecoveryScope!,
        requestedSessionId: "s_original",
      },
      submissionWireSnapshot: {
        ...base.submissionWireSnapshot!,
        composerSurface: "workbench",
      },
      onSendInitSettled: settled,
    }),
  );
  await waitFor(() =>
    expect(settled).toHaveBeenCalledWith("send-original", false),
  );
  expect(preparePendingReferenceSubmission).toHaveBeenCalledOnce();
  expect(clearPendingSubmission).not.toHaveBeenCalled();
  expect(hook.result.current.turnId).toBeNull();
});

it.each(["off", "planning"] as const)("sends an @ input once with frozen %s mode and no client deadline", async (planningMode) => {
  vi.mocked(singleAttemptChatActionFetch).mockClear();
  const settled = vi.fn();
  globalThis.fetch = vi.fn(async (_input, init) =>
    init?.method === "POST"
      ? new Response(JSON.stringify({
          status: "started", session_id: "s_original", turn_id: "t_1", execution_generation: 1, stream_url: "/api/v1/chat/stream/t_1",
        }))
      : new Response("{}", { status: 404 }),
  );
  renderHook(() =>
    useTurnRunner({
      ...base,
      activeSessionId: "s_original",
      submissionRecoveryScope: { ...base.submissionRecoveryScope!, requestedSessionId: "s_original" },
      submissionWireSnapshot: { ...base.submissionWireSnapshot!, composerSurface: "workbench", planningMode },
      onSendInitSettled: settled,
    }),
  );
  await waitFor(() => expect(settled).toHaveBeenCalled());
  const sends = vi.mocked(singleAttemptChatActionFetch).mock.calls.filter(([input]) => String(input).includes("/api/v1/chat/send"));
  expect(sends).toHaveLength(1);
  expect(sends[0][2]).toBeNull();
  expect(JSON.parse(String(sends[0][1]?.body))).toMatchObject({
    planning_mode: planningMode,
    input_document: base.referenceInputDocument,
  });
});

it.each(["scope", "auth", "disabled", "unmounted"])(
  "does not POST after %s changes during persistence",
  async (change) => {
    let release!: (value: typeof identity) => void;
    vi.mocked(preparePendingReferenceSubmission).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const settled = vi.fn();
    const hook = renderHook(
      (props: UseTurnRunnerArgs) => useTurnRunner(props),
      { initialProps: { ...base, onSendInitSettled: settled } },
    );
    await waitFor(() =>
      expect(preparePendingReferenceSubmission).toHaveBeenCalledOnce(),
    );
    if (change === "scope")
      hook.rerender({
        ...base,
        onSendInitSettled: settled,
        currentChatScopeKey: "another",
      });
    else if (change === "auth")
      hook.rerender({
        ...base,
        onSendInitSettled: settled,
        submissionRecoveryScope: {
          ...base.submissionRecoveryScope!,
          authSessionGeneration: "another-login",
        },
      });
    else if (change === "disabled")
      hook.rerender({
        ...base,
        onSendInitSettled: settled,
        referenceSubmissionEnabled: false,
      });
    else hook.unmount();
    await act(async () => release(identity));
    await waitFor(() =>
      expect(settled).toHaveBeenCalledWith("send-original", false),
    );
    expect(
      vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toEqual([]);
    expect(clearPendingSubmission).not.toHaveBeenCalled();
  },
);

it.each([
  "scope",
  "missing_identity",
  "replay",
  "disabled",
  "display",
])(
  "rejects %s before writing an identity or making HTTP requests",
  async (change) => {
    const args = structuredClone(base);
    if (change === "scope") args.submissionRecoveryScope!.workspaceId = "wrong";
    if (change === "missing_identity") args.sendAttemptKey = null;
    if (change === "replay") args.replaySourceTurnId = "t_previous";
    if (change === "disabled") args.referenceSubmissionEnabled = false;
    if (change === "display") args.message = "Changed display text";
    const settled = vi.fn();
    renderHook(() => useTurnRunner({ ...args, onSendInitSettled: settled }));
    await waitFor(() => expect(settled).toHaveBeenCalled());
    expect(preparePendingReferenceSubmission).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  },
);

it("captures input, parameters and cleanup scope before the persistence await", async () => {
  const args = structuredClone(base);
  let release!: (value: typeof identity) => void;
  let savedScope: unknown;
  let savedSource: unknown;
  vi.mocked(preparePendingReferenceSubmission).mockImplementationOnce(
    (scope, draft) => {
      savedScope = structuredClone(scope);
      savedSource = structuredClone(draft);
      return new Promise((resolve) => {
        release = resolve;
      });
    },
  );
  const bodies: unknown[] = [];
  globalThis.fetch = vi.fn(async (_input, init) => {
    if (init?.method === "POST") bodies.push(JSON.parse(String(init.body)));
    return new Response(
      JSON.stringify({ detail: { error: { code: "session_deleted" } } }),
      { status: 409 },
    );
  });
  const settled = vi.fn();
  renderHook(() => useTurnRunner({ ...args, onSendInitSettled: settled }));
  await waitFor(() =>
    expect(preparePendingReferenceSubmission).toHaveBeenCalledOnce(),
  );
  args.referenceInputDocument!.nodes.splice(0);
  args.submissionRecoveryScope!.workspaceId = "mutated";
  args.submissionWireSnapshot!.preferredModel = "mutated";
  await act(async () => release(identity));
  await waitFor(() => expect(clearPendingSubmission).toHaveBeenCalledOnce());
  expect(clearPendingSubmission).toHaveBeenCalledWith(
    savedScope,
    identity.clientSubmissionId,
  );
  expect(bodies).toHaveLength(1);
  expect(bodies[0]).toMatchObject({
    input_document: source.normalized,
    workspace_id: "ws_ref",
  });
  expect(savedSource).toMatchObject({
    input_document: source.normalized,
    sourceDraftRevision: "rev_original",
  });
});


it.each([
  ["marked not started", { code: "resource_unavailable", admission: "not_started" }, 1],
  ["unmarked (may follow a committed Turn)", { code: "resource_unavailable" }, 0],
] as const)("releases the submission journal only for a rejection the server marked %s", async (_label, error, clears) => {
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ detail: { error } }), { status: 409 }));
  const settled = vi.fn();
  const cleared = vi.fn();
  renderHook(() => useTurnRunner({ ...base, onSendInitSettled: settled, onSubmissionCleared: cleared }));
  await waitFor(() => expect(settled).toHaveBeenCalledWith("send-original", false));
  expect(clearPendingSubmission).toHaveBeenCalledTimes(clears);
  expect(cleared).toHaveBeenCalledTimes(clears);
});

it.each(["success", "deferred", "error"])("settles reference draft cleanup before its journal: %s", async (mode) => {
  const order: string[] = [];
  const settled = vi.fn();
  const cleared = vi.fn();
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({
    status: "started", session_id: "s_reference", turn_id: "t_reference", stream_url: "/api/v1/chat/stream/t_reference", execution_generation: 1,
  })));
  vi.mocked(clearPendingSubmission).mockImplementationOnce(async () => { order.push("journal"); });
  const beforeClear = vi.fn(async () => {
    order.push("draft");
    if (mode === "error") throw new Error("storage unavailable");
    return mode === "success";
  });
  const hook = renderHook(() => useTurnRunner({ ...base, onReferenceSubmissionAccepted: beforeClear, onSubmissionCleared: cleared, onSendInitSettled: settled }));
  await waitFor(() => expect(settled).toHaveBeenCalledWith(
    "send-original",
    mode === "success",
    ...(mode === "success" ? [{ turnId: "t_reference", sessionId: "s_reference" }] : []),
  ));
  expect(beforeClear).toHaveBeenCalledWith("send-original", "original");
  expect(order).toEqual(mode === "success" ? ["draft", "journal"] : ["draft"]);
  expect(clearPendingSubmission).toHaveBeenCalledTimes(mode === "success" ? 1 : 0);
  expect(cleared).toHaveBeenCalledTimes(mode === "success" ? 1 : 0);
  expect(hook.result.current.turnId).toBe(
    mode === "success" ? "t_reference" : null,
  );
});
