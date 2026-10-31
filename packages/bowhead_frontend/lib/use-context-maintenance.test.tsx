import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { fetchSessionDetail } from "./api";
import { lookupComposerChatAction } from "./composer-chat-action";
import { clearContextMaintenanceJournal, prepareContextMaintenanceJournal, readContextMaintenanceJournal, type PendingContextMaintenance } from "./composer-submission-recovery";
import { postSendMessage } from "./use-turn-runner";
import { useContextMaintenance } from "./use-context-maintenance";

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("./api", () => ({ fetchSessionDetail: vi.fn() }));
vi.mock("./composer-chat-action", async (original) => ({
  ...(await original<typeof import("./composer-chat-action")>()), lookupComposerChatAction: vi.fn(),
}));
vi.mock("./composer-submission-recovery", () => ({
  clearContextMaintenanceJournal: vi.fn(), prepareContextMaintenanceJournal: vi.fn(), readContextMaintenanceJournal: vi.fn(),
}));
vi.mock("./use-turn-runner", () => ({
  postSendMessage: vi.fn(),
  captureComposerSubmissionWireSnapshot: () => ({ composerSurface: "workbench", userProfile: null, preferredProvider: "provider", preferredModel: "model", planningMode: "off" }),
}));

const scope = { userId: "u_context", teamId: "team_context", workspaceId: "ws_context", requestedSessionId: "s_context", authSessionGeneration: "auth_1" };
const context = { contract: "context-maintenance.v1" as const, expected_generation: `mcg_${"a".repeat(32)}` };
const receipt = { session_id: scope.requestedSessionId, turn_id: "t_context", stream_url: "/api/v1/chat/stream/t_context", execution_generation: 1, terminal_protocol_version: 1, context_maintenance: context };
let journal: PendingContextMaintenance | null;
const attach = vi.fn(() => true);
const onMessage = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  journal = null;
  vi.mocked(readContextMaintenanceJournal).mockImplementation(async () => structuredClone(journal));
  vi.mocked(prepareContextMaintenanceJournal).mockImplementation(async (_scope, draft) => {
    journal = { journalId: "11111111-1111-4111-8111-111111111111", action: draft.terminalAction ?? null, context: draft.wireSnapshot!.contextMaintenance!,
      draft: { ...structuredClone(draft), schemaVersion: 4, clientSubmissionId: "11111111-1111-4111-8111-111111111111", contentChecksum: "checksum", createdAt: "2026-10-30" } };
    return structuredClone(journal);
  });
  vi.mocked(clearContextMaintenanceJournal).mockImplementation(async (_scope, id) => { if (journal?.journalId === id) journal = null; });
  vi.mocked(fetchSessionDetail).mockResolvedValue({ context_generation: context.expected_generation, turns: [], chat_execution: { write_protocol: "terminal_v1" } } as never);
  vi.mocked(postSendMessage).mockResolvedValue(receipt as never);
  vi.mocked(lookupComposerChatAction).mockResolvedValue(receipt as never);
});

function mount() {
  return renderHook(({ selected, blocked }) => useContextMaintenance({ scope: selected, blocked, attach, onMessage }), {
    initialProps: { selected: scope, blocked: false },
  });
}

async function seed() {
  const action = { actionId: `act_start_${"b".repeat(32)}`, actionKind: "start" as const, attempted: true as const, sourceRevision: null, sessionId: scope.requestedSessionId };
  await prepareContextMaintenanceJournal(scope, { message: "", attachments: [], terminalAction: action,
    wireSnapshot: { composerSurface: "workbench", userProfile: null, preferredProvider: "provider", preferredModel: "model", planningMode: "off", contextMaintenance: context } });
  vi.mocked(prepareContextMaintenanceJournal).mockClear();
}

it("uses a separate action and never invents a Composer submission ID for native admission", async () => {
  const { result } = mount();
  await act(async () => { await result.current.run(); });
  expect(postSendMessage).toHaveBeenCalledTimes(1);
  expect(postSendMessage).toHaveBeenCalledWith(scope.workspaceId, "", "", scope.requestedSessionId, [], null, null,
    expect.objectContaining({ contextMaintenance: context, planningMode: "off" }),
    expect.objectContaining({ actionKind: "start", sourceRevision: null, sessionId: scope.requestedSessionId }));
  expect(attach).toHaveBeenCalledWith(receipt);
  expect(journal).toBeNull();
});

it("can manually organize an old conversation without a previously published context generation", async () => {
  const initial = { ...context, expected_generation: null };
  vi.mocked(fetchSessionDetail).mockResolvedValue({ context_generation: null, turns: [{ status: "completed" }], chat_execution: { write_protocol: "terminal_v1" } } as never);
  vi.mocked(postSendMessage).mockResolvedValue({ ...receipt, context_maintenance: initial } as never);
  const { result } = mount();
  await act(async () => { await result.current.run(); });
  expect(postSendMessage).toHaveBeenCalledTimes(1);
  expect(attach).toHaveBeenCalledWith({ ...receipt, context_maintenance: initial });
  expect(journal).toBeNull();
});

it("recovers a lost acknowledgement by lookup without resending the summary", async () => {
  vi.mocked(postSendMessage).mockRejectedValueOnce(new Error("Acknowledgement lost"));
  const first = mount();
  await act(async () => { await first.result.current.run(); });
  const original = journal!.action;
  expect(first.result.current.pending).toBe(true);
  first.unmount();
  const recovered = mount();
  await waitFor(() => expect(attach).toHaveBeenCalledWith(receipt));
  expect(lookupComposerChatAction).toHaveBeenCalledWith(original);
  expect(postSendMessage).toHaveBeenCalledTimes(1);
  expect(prepareContextMaintenanceJournal).toHaveBeenCalledTimes(1);
  expect(recovered.result.current.pending).toBe(false);
});

it("requires an explicit click to retry the same readable request after a missing receipt", async () => {
  await seed();
  const original = structuredClone(journal);
  vi.mocked(lookupComposerChatAction).mockRejectedValue(Object.assign(new Error("Missing"), { status: 404 }));
  const { result } = mount();
  await waitFor(() => expect(result.current.pending).toBe(true));
  expect(postSendMessage).not.toHaveBeenCalled();
  await act(async () => { await result.current.run(); });
  expect(postSendMessage).toHaveBeenCalledTimes(1);
  expect(vi.mocked(postSendMessage).mock.calls[0]?.at(-1)).toEqual(original!.action);
  expect(prepareContextMaintenanceJournal).not.toHaveBeenCalled();
});

it.each([401, 409, 503])("keeps the original identity without resending on lookup status %s", async (status) => {
  await seed();
  vi.mocked(lookupComposerChatAction).mockRejectedValue(Object.assign(new Error("Lookup unavailable"), { status }));
  const { result } = mount();
  await waitFor(() => expect(result.current.pending).toBe(true));
  await act(async () => { await result.current.run(); });
  expect(postSendMessage).not.toHaveBeenCalled();
  expect(clearContextMaintenanceJournal).not.toHaveBeenCalled();
});

it("keeps corrupt or stale-auth payloads lookup-only, even after an explicit click", async () => {
  await seed(); journal!.draft = null;
  vi.mocked(lookupComposerChatAction).mockRejectedValue(Object.assign(new Error("Missing"), { status: 404 }));
  const { result } = mount();
  await waitFor(() => expect(result.current.pending).toBe(true));
  await act(async () => { await result.current.run(); });
  expect(postSendMessage).not.toHaveBeenCalled();
  expect(onMessage).toHaveBeenLastCalledWith("compact_context_restore_failed");
});

it("rejects a receipt for a different source without settling the journal", async () => {
  await seed();
  vi.mocked(lookupComposerChatAction).mockResolvedValue({ ...receipt, context_maintenance: { ...context, expected_generation: `mcg_${"c".repeat(32)}` } } as never);
  const { result } = mount();
  await waitFor(() => expect(result.current.pending).toBe(true));
  expect(attach).not.toHaveBeenCalled();
  expect(clearContextMaintenanceJournal).not.toHaveBeenCalled();
});

it("does not attach a late receipt after leaving the session", async () => {
  await seed();
  let resolve!: (value: never) => void;
  vi.mocked(lookupComposerChatAction).mockImplementation(() => new Promise((done) => { resolve = done; }));
  const { unmount } = mount();
  await waitFor(() => expect(lookupComposerChatAction).toHaveBeenCalled());
  unmount();
  await act(async () => resolve(receipt as never));
  expect(attach).not.toHaveBeenCalled();
  expect(clearContextMaintenanceJournal).not.toHaveBeenCalled();
});

it("can settle a receipt already hydrated by the original runner without opening another stream", async () => {
  await seed();
  renderHook(() => useContextMaintenance({ scope, blocked: true, attach, onMessage }));
  await waitFor(() => expect(clearContextMaintenanceJournal).toHaveBeenCalledTimes(1));
  expect(attach).toHaveBeenCalledWith(receipt, true);
  expect(postSendMessage).not.toHaveBeenCalled();
});
