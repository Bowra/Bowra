import { StrictMode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { captureComposerSubmissionWireSnapshot, useTurnRunner } from "./use-turn-runner";
import { setPlanningModePref } from "./planning-mode-pref";
import type { ComposerChatAction } from "./composer-chat-action";

const capabilityMocks = vi.hoisted(() => ({ planning: false }));

vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({
    status: "authenticated",
    me: { capabilities: { planning: capabilityMocks.planning } },
  }),
}));

vi.mock("./composer-submission-recovery", () => ({
  preparePendingSubmission: vi.fn(async () => ({
    clientSubmissionId: globalThis.crypto.randomUUID(),
    contentChecksum: "test-checksum",
  })),
  clearPendingSubmission: vi.fn(async () => undefined),
}));


const recoveryScope = {
  userId: "u_1",
  teamId: "team_1",
  workspaceId: "ws_1",
  requestedSessionId: null,
  authSessionGeneration: "auth-generation-1",
};

const originalFetch = globalThis.fetch;
const originalEventSource = globalThis.EventSource;
const originalLocalStorage = window.localStorage;

class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readyState = FakeEventSource.OPEN;
  onerror: (() => void) | null = null;
  static instances: FakeEventSource[] = [];
  constructor(public readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener() {}
  removeEventListener() {}
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
}

function response(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  FakeEventSource.instances = [];
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
});

afterEach(() => {
  capabilityMocks.planning = false;
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: originalLocalStorage,
  });
  globalThis.fetch = originalFetch;
  (globalThis as Record<string, unknown>).EventSource = originalEventSource;
  vi.restoreAllMocks();
});

describe("batch A submission receipt client", () => {
  const settingsBinding = { controllerId: "original-page", clientInfo: JSON.stringify({
    form_id: "workspace_i18n", user_id: "u_1", team_id: "team_1", workspace_id: "ws_1",
    edit_generation: 3, saved_ready: true, pending_writes: 0, unconfirmed_fields: [],
  }) };

  it("retains the exact settings wire for receipt-backed recovery and associates only the accepted Turn", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    const accepted = vi.fn();
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        body = JSON.parse(String(init?.body));
        return response(200, { status: "started", turn_id: "t_settings", session_id: "s_settings",
          stream_url: "/api/v1/chat/stream/t_settings", execution_generation: 1 });
      }
      return response(200, {});
    }) as typeof fetch;
    const hook = renderHook(() => useTurnRunner({
      mode: "sse", message: "Change week start", workspaceId: "ws_1", sendTick: 1,
      submissionProtocolEnabled: true, submissionRecoveryScope: recoveryScope,
      submissionWireSnapshot: { ...captureComposerSubmissionWireSnapshot(null, false), workspaceSettingsBinding: settingsBinding },
      draftChatBinding: { draftId: "sad_later", version: 4, dirty: false },
      onWorkspaceSettingsTurnAccepted: accepted,
    }));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_settings"));
    expect(body.client_info).toMatchObject({ configuration_settings_target: settingsBinding.clientInfo });
    expect(body.client_info).not.toHaveProperty("agent_draft_id");
    expect(body.client_info).not.toHaveProperty("controllerId");
    expect(accepted).toHaveBeenCalledExactlyOnceWith("t_settings", settingsBinding);
  });

  it.each(["terminal", "replay", "planning", "mixed"])("refuses an unsupported %s settings entry without a fallback POST", async (entry) => {
    const accepted = vi.fn();
    const settled = vi.fn();
    globalThis.fetch = vi.fn(async () => response(200, {})) as typeof fetch;
    const hook = renderHook(() => useTurnRunner({
      mode: "sse", message: "Change week start", workspaceId: "ws_1", sendTick: 1,
      submissionWireSnapshot: { ...captureComposerSubmissionWireSnapshot(null, false), workspaceSettingsBinding: settingsBinding,
        ...(entry === "planning" ? { planningMode: "planning" as const } : {}),
        ...(entry === "mixed" ? { draftChatBinding: { draftId: "sad_1", version: 1, dirty: false } } : {}),
      },
      ...(entry === "terminal" ? { chatAction: { actionKind: "start" as const, sessionId: null, actionId: "act_settings", attempted: true, sourceRevision: null } } : {}),
      ...(entry === "replay" ? { replaySourceTurnId: "t_old" } : {}),
      onWorkspaceSettingsTurnAccepted: accepted, onSendInitSettled: settled,
    }));
    await waitFor(() => expect(settled).toHaveBeenCalledWith(expect.any(String), false));
    expect(hook.result.current.turnId).toBeNull();
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(accepted).not.toHaveBeenCalled();
  });

  it("clears a settings admission refusal and invalidates its original page without adopting a Turn", async () => {
    const { clearPendingSubmission } = await import("./composer-submission-recovery");
    vi.mocked(clearPendingSubmission).mockClear();
    const denied = vi.fn();
    const accepted = vi.fn();
    globalThis.fetch = vi.fn(async () => response(403, {
      detail: { error: { code: "configuration_target_access_denied", admission: "not_started" } },
    })) as typeof fetch;
    const hook = renderHook(() => useTurnRunner({
      mode: "sse", message: "Change week start", workspaceId: "ws_1", sendTick: 1,
      submissionProtocolEnabled: true, submissionRecoveryScope: recoveryScope,
      submissionWireSnapshot: { ...captureComposerSubmissionWireSnapshot(null, false), workspaceSettingsBinding: settingsBinding },
      onWorkspaceSettingsAccessDenied: denied, onWorkspaceSettingsTurnAccepted: accepted,
    }));
    await waitFor(() => expect(clearPendingSubmission).toHaveBeenCalled());
    expect(denied).toHaveBeenCalledExactlyOnceWith(settingsBinding);
    expect(accepted).not.toHaveBeenCalled();
    expect(hook.result.current.turnId).toBeNull();
  });

  const terminalStartAction: ComposerChatAction = {
    actionId: "act_start_00112233445546778899aabbccddeeff",
    actionKind: "start",
    attempted: true,
    sourceRevision: null,
    sessionId: null,
  };

  it("attaches the exact right-side draft version to a normal Chat submission", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return response(200, { status: "started", turn_id: "t_bound", session_id: "s_bound",
          stream_url: "/api/v1/chat/stream/t_bound", execution_generation: 1 });
      }
      return response(200, {});
    }) as typeof fetch;
    const hook = renderHook(() => useTurnRunner({
      mode: "sse", message: "把说明改短", workspaceId: "ws_1", sendTick: 1,
      draftChatBinding: { draftId: "sad_review", version: 3, dirty: false },
    }));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_bound"));
    expect(body.client_info).toMatchObject({ agent_draft_id: "sad_review", agent_draft_version: "3" });
  });

  it("keeps the submitted draft version when recovering after the right side changes", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return response(200, { status: "started", turn_id: "t_frozen", session_id: "s_frozen",
          stream_url: "/api/v1/chat/stream/t_frozen", execution_generation: 1 });
      }
      return response(200, {});
    }) as typeof fetch;
    const frozen = captureComposerSubmissionWireSnapshot(null, false, {
      draftId: "sad_review", version: 3, dirty: false,
    });
    const hook = renderHook(() => useTurnRunner({
      mode: "sse", message: "把说明改短", workspaceId: "ws_1", sendTick: 1,
      submissionWireSnapshot: frozen,
      draftChatBinding: { draftId: "sad_review", version: 4, dirty: true },
    }));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_frozen"));
    expect(body.client_info).toMatchObject({ agent_draft_id: "sad_review", agent_draft_version: "3" });
    expect(body.client_info).not.toHaveProperty("agent_draft_dirty");
  });

  it.each([true, false])("uses only the persisted action target after the selected draft changes (%s)", async (hasTarget) => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    const target = { kind: "agent_draft" as const, id: "sad_original", version: 3 };
    const action = { ...terminalStartAction, ...(hasTarget ? { agentDraftTarget: target } : {}) };
    const bodies: Record<string, unknown>[] = [];
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        bodies.push(JSON.parse(String(init?.body)));
        return response(200, {
          status: "started", session_id: "s_00112233445546778899aabbccddeeff",
          turn_id: "t_00112233445546778899aabbccddeeff", stream_url: "/api/v1/chat/stream/t_00112233445546778899aabbccddeeff",
          execution_generation: 1, terminal_protocol_version: 1,
          ...(hasTarget ? { agent_draft_target: target } : {}),
        });
      }
      return response(200, {});
    }) as typeof fetch;
    const hook = renderHook(() => useTurnRunner({
      mode: "sse", message: "Shorten instructions", workspaceId: "ws_1", sendTick: 1,
      chatAction: action,
      draftChatBinding: { draftId: "sad_other", version: 9, dirty: true },
      submissionWireSnapshot: captureComposerSubmissionWireSnapshot(null, false, { draftId: "sad_other", version: 9, dirty: true }),
    }));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_00112233445546778899aabbccddeeff"));
    expect(bodies).toHaveLength(1);
    if (hasTarget) expect(bodies[0].client_info).toMatchObject({ agent_draft_id: "sad_original", agent_draft_version: "3" });
    else expect(bodies[0].client_info).not.toHaveProperty("agent_draft_id");
    expect(bodies[0].client_info).not.toHaveProperty("agent_draft_dirty");
  });

  it("uses one marker-backed POST and emits the complete Terminal identity", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let postCount = 0;
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        postCount += 1;
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return response(200, {
          status: "started",
          turn_id: "t_00112233445546778899aabbccddeeff",
          session_id: "s_00112233445546778899aabbccddeeff",
          stream_url: "/api/v1/chat/stream/t_00112233445546778899aabbccddeeff",
          execution_generation: 1,
          terminal_protocol_version: 1,
        });
      }
      return response(200, {});
    }) as typeof fetch;
    const settled = vi.fn();

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "durable once",
      workspaceId: "ws_1",
      sendTick: 1,
      attachments: [{
        fileId: "art_terminal",
        filename: "terminal.pdf",
        size: 11,
        mime: "application/pdf",
      }],
      submissionProtocolEnabled: true,
      submissionRecoveryScope: recoveryScope,
      chatAction: terminalStartAction,
      onChatActionSettled: settled,
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe(
      "t_00112233445546778899aabbccddeeff",
    ));
    expect(postCount).toBe(1);
    expect(body).toMatchObject({
      attachments: ["art_terminal"],
      attachment_refs: [{
        fileId: "art_terminal",
        filename: "terminal.pdf",
        size: 11,
        mime: "application/pdf",
      }],
      client_info: {
        request_id: terminalStartAction.actionId,
        action_kind: "start",
        user_msg_id: "u_s_00112233445546778899aabbccddeeff",
      },
    });
    expect(body).not.toHaveProperty("client_submission_id");
    expect(settled).toHaveBeenCalledWith(
      terminalStartAction.actionId,
      "adopted",
    );
  });

  it("keeps one POST owner through React StrictMode effect replay", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let postCount = 0;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        postCount += 1;
        return response(200, {
          status: "started",
          turn_id: "t_00112233445546778899aabbccddeeff",
          session_id: "s_00112233445546778899aabbccddeeff",
          stream_url: "/api/v1/chat/stream/t_00112233445546778899aabbccddeeff",
          execution_generation: 1,
          terminal_protocol_version: 1,
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "StrictMode once",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: terminalStartAction,
      sendAttemptKey: terminalStartAction.actionId,
      chatActionScopeKey: "home",
      currentChatScopeKey: "home",
    }), { wrapper: StrictMode });

    await waitFor(() => expect(hook.result.current.turnId).toBe(
      "t_00112233445546778899aabbccddeeff",
    ));
    expect(postCount).toBe(1);
  });

  it("does not close the accepted Home stream when its scope becomes the session", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return response(200, {
          status: "started",
          turn_id: "t_00112233445546778899aabbccddeeff",
          session_id: "s_00112233445546778899aabbccddeeff",
          stream_url: "/api/v1/chat/stream/t_00112233445546778899aabbccddeeff",
          execution_generation: 1,
          terminal_protocol_version: 1,
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const hook = renderHook(({ scopeKey }) => useTurnRunner({
      mode: "sse",
      message: "Home transition",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: terminalStartAction,
      sendAttemptKey: terminalStartAction.actionId,
      chatActionScopeKey: "home",
      currentChatScopeKey: scopeKey,
    }), { initialProps: { scopeKey: "home" } });

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    hook.rerender({ scopeKey: "session:s_00112233445546778899aabbccddeeff" });
    expect(FakeEventSource.instances[0]?.readyState).toBe(FakeEventSource.OPEN);
  });

  it("closes the accepted stream on an explicit new-work reset", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return response(200, {
          status: "started",
          turn_id: "t_reset",
          session_id: "s_reset",
          stream_url: "/api/v1/chat/stream/t_reset",
          execution_generation: 1,
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const hook = renderHook(({ resetSignal }) => useTurnRunner({
      mode: "sse",
      message: "reset closes transport",
      workspaceId: "ws_1",
      sendTick: 1,
      resetSignal,
    }), { initialProps: { resetSignal: 0 } });

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    hook.rerender({ resetSignal: 1 });

    expect(FakeEventSource.instances[0]?.readyState).toBe(FakeEventSource.CLOSED);
    expect(hook.result.current.turnId).toBeNull();
    expect(hook.result.current.started).toBe(false);
  });

  it("settles an accepted legacy attempt after its presentation scope changed", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let releaseSend!: () => void;
    const sendReleased = new Promise<void>((resolve) => {
      releaseSend = resolve;
    });
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        await sendReleased;
        return response(200, {
          status: "started",
          turn_id: "t_legacy_scope_change",
          session_id: "s_legacy_scope_change",
          stream_url: "/api/v1/chat/stream/t_legacy_scope_change",
          execution_generation: 1,
        });
      }
      return response(200, {});
    }) as typeof fetch;
    const sendSettled = vi.fn();

    const hook = renderHook(({ scopeKey }) => useTurnRunner({
      mode: "sse",
      message: "legacy scope settlement",
      workspaceId: "ws_1",
      sendTick: 1,
      sendAttemptKey: "send_scope_change",
      currentChatScopeKey: scopeKey,
      onSendInitSettled: sendSettled,
    }), { initialProps: { scopeKey: "session:s_old" } });

    hook.rerender({ scopeKey: "home" });
    releaseSend();

    await waitFor(() => expect(sendSettled).toHaveBeenCalledWith(
      "send_scope_change",
      true,
    ));
    expect(hook.result.current.turnId).toBeNull();
  });

  it("retains an in-memory Terminal identity after scope change and response loss", async () => {
    let rejectSend!: (error: Error) => void;
    const sendResponse = new Promise<Response>((_resolve, reject) => {
      rejectSend = reject;
    });
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) return await sendResponse;
      return response(200, {});
    }) as typeof fetch;
    const unknown = vi.fn();
    const sendSettled = vi.fn();

    const hook = renderHook(({ scopeKey }) => useTurnRunner({
      mode: "sse",
      message: "unknown action across scope",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: terminalStartAction,
      sendAttemptKey: terminalStartAction.actionId,
      chatActionScopeKey: "home",
      currentChatScopeKey: scopeKey,
      onChatActionUnknown: unknown,
      onSendInitSettled: sendSettled,
    }), { initialProps: { scopeKey: "home" } });

    hook.rerender({ scopeKey: "session:s_other" });
    rejectSend(new TypeError("response lost"));

    await waitFor(() => expect(unknown).toHaveBeenCalledWith(terminalStartAction));
    expect(sendSettled).toHaveBeenCalledWith(terminalStartAction.actionId, false);
  });

  it("does not let a late action A response clear a replacement action B", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    const actionA = { ...terminalStartAction, sourceRevision: "rev_marker_a" };
    const actionB = {
      ...terminalStartAction,
      actionId: "act_start_ffeeddccbbaa49888766554433221100",
      sourceRevision: "rev_marker_b",
    };
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return response(200, {
          status: "started",
          turn_id: "t_00112233445546778899aabbccddeeff",
          session_id: "s_00112233445546778899aabbccddeeff",
          stream_url: "/api/v1/chat/stream/t_00112233445546778899aabbccddeeff",
          execution_generation: 1,
          terminal_protocol_version: 1,
        });
      }
      return response(200, {});
    }) as typeof fetch;
    const settled = vi.fn(async () => "superseded" as const);
    const sendSettled = vi.fn();

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "old action A",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: actionA,
      chatActionScopeKey: "home",
      currentChatScopeKey: "home",
      sendAttemptKey: actionA.actionId,
      onChatActionSettled: settled,
      onSendInitSettled: sendSettled,
    }));

    await waitFor(() => expect(settled).toHaveBeenCalledWith(
      actionA.actionId,
      "adopted",
    ));
    expect(sendSettled).toHaveBeenCalledWith(actionA.actionId, false);
    expect(hook.result.current.turnId).toBeNull();
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("finishes a rejected attempt after its marker was superseded", async () => {
    const actionA = { ...terminalStartAction, sourceRevision: "rev_marker_a" };
    const actionB = {
      ...terminalStartAction,
      actionId: "act_start_ffeeddccbbaa49888766554433221100",
      sourceRevision: "rev_marker_b",
    };
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return response(401, { detail: { error: { code: "auth_required" } } });
      }
      return response(200, { ok: true, token: "refreshed", user: {} });
    }) as typeof fetch;
    const settled = vi.fn(async () => "superseded" as const);
    const sendSettled = vi.fn();

    renderHook(() => useTurnRunner({
      mode: "sse",
      message: "old action A",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: actionA,
      chatActionScopeKey: "home",
      currentChatScopeKey: "home",
      sendAttemptKey: actionA.actionId,
      onChatActionSettled: settled,
      onSendInitSettled: sendSettled,
    }));

    await waitFor(() => expect(settled).toHaveBeenCalledWith(
      actionA.actionId,
      "rejected",
    ));
    expect(sendSettled).toHaveBeenCalledWith(actionA.actionId, false);
  });

  it("adopts an accepted receipt but keeps recovery locked when marker settlement fails", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    const persistedAction = {
      ...terminalStartAction,
      sourceRevision: "rev_marker_a",
    };
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return response(200, {
          status: "started",
          turn_id: "t_00112233445546778899aabbccddeeff",
          session_id: "s_00112233445546778899aabbccddeeff",
          stream_url: "/api/v1/chat/stream/t_00112233445546778899aabbccddeeff",
          execution_generation: 1,
          terminal_protocol_version: 1,
        });
      }
      return response(200, {});
    }) as typeof fetch;
    const unknown = vi.fn();
    const settled = vi.fn(async () => "deferred" as const);
    const sendSettled = vi.fn();

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "accepted but cleanup failed",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: persistedAction,
      chatActionScopeKey: "home",
      currentChatScopeKey: "home",
      sendAttemptKey: persistedAction.actionId,
      onChatActionUnknown: unknown,
      onChatActionSettled: settled,
      onSendInitSettled: sendSettled,
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe(
      "t_00112233445546778899aabbccddeeff",
    ));
    expect(unknown).not.toHaveBeenCalled();
    expect(settled).toHaveBeenCalledWith(
      persistedAction.actionId,
      "adopted",
    );
    expect(sendSettled).toHaveBeenCalledWith(persistedAction.actionId, true, { turnId: "t_00112233445546778899aabbccddeeff", sessionId: "s_00112233445546778899aabbccddeeff" });
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("refreshes auth without replaying a marker-backed POST after deferred 401 settlement", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let postCount = 0;
    let refreshCount = 0;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        postCount += 1;
        return response(401, { detail: { error: { code: "auth_required" } } });
      }
      if (String(url).includes("/auth/refresh")) {
        refreshCount += 1;
        return response(200, { ok: true, token: "refreshed", user: {} });
      }
      return response(200, {});
    }) as typeof fetch;
    const settled = vi.fn(async () => "deferred" as const);

    renderHook(() => useTurnRunner({
      mode: "sse",
      message: "do not replay",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: terminalStartAction,
      onChatActionSettled: settled,
    }));

    await waitFor(() => expect(settled).toHaveBeenCalledWith(
      terminalStartAction.actionId,
      "rejected",
    ));
    await waitFor(() => expect(refreshCount).toBe(1));
    expect(postCount).toBe(1);
  });

  it("keeps an unknown marker and never retries after transport loss", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let postCount = 0;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        postCount += 1;
        throw new TypeError("response lost");
      }
      return response(200, {});
    }) as typeof fetch;
    const unknown = vi.fn();

    renderHook(() => useTurnRunner({
      mode: "sse",
      message: "unknown once",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: terminalStartAction,
      onChatActionUnknown: unknown,
    }));

    await waitFor(() => expect(unknown).toHaveBeenCalledWith(terminalStartAction));
    expect(postCount).toBe(1);
  });

  it("keeps an ambiguous post-admission 409 for receipt lookup", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let postCount = 0;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        postCount += 1;
        return response(409, {
          detail: { error: { code: "terminal_dependency_recovering" } },
        });
      }
      return response(200, {});
    }) as typeof fetch;
    const unknown = vi.fn();
    const settled = vi.fn();

    renderHook(() => useTurnRunner({
      mode: "sse",
      message: "admission outcome is ambiguous",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: terminalStartAction,
      onChatActionUnknown: unknown,
      onChatActionSettled: settled,
    }));

    await waitFor(() => expect(unknown).toHaveBeenCalledWith(terminalStartAction));
    expect(settled).not.toHaveBeenCalled();
    expect(postCount).toBe(1);
  });

  it("keeps an authz lookup failure unresolved because an action may already exist", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return response(503, {
          detail: { error: { code: "authz_lookup_failed" } },
        });
      }
      return response(200, {});
    }) as typeof fetch;
    const unknown = vi.fn();
    const settled = vi.fn();

    renderHook(() => useTurnRunner({
      mode: "sse",
      message: "lookup may follow admission",
      workspaceId: "ws_1",
      sendTick: 1,
      chatAction: terminalStartAction,
      onChatActionUnknown: unknown,
      onChatActionSettled: settled,
    }));

    await waitFor(() => expect(unknown).toHaveBeenCalledWith(terminalStartAction));
    expect(settled).not.toHaveBeenCalled();
  });

  it("rejects mixed Terminal action and legacy replay identity before network", async () => {
    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy as typeof fetch;
    const unknown = vi.fn();

    renderHook(() => useTurnRunner({
      mode: "sse",
      message: "invalid mixed replay",
      workspaceId: "ws_1",
      sendTick: 1,
      replaySourceTurnId: "t_source",
      chatAction: terminalStartAction,
      onChatActionUnknown: unknown,
    }));

    await waitFor(() => expect(unknown).toHaveBeenCalledWith(terminalStartAction));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("uses the workspace and session frozen with a prepared action", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    const frozenAction: ComposerChatAction = {
      ...terminalStartAction,
      sessionId: "s_scope_a",
    };
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return response(200, {
          status: "started",
          turn_id: "t_00112233445546778899aabbccddeeff",
          session_id: "s_scope_a",
          stream_url: "/api/v1/chat/stream/t_00112233445546778899aabbccddeeff",
          execution_generation: 1,
          terminal_protocol_version: 1,
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "scope A survives a later selection",
      workspaceId: "ws_scope_b",
      activeSessionId: "s_scope_b",
      sendTick: 1,
      chatAction: frozenAction,
      chatActionWorkspaceId: "ws_scope_a",
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe(
      "t_00112233445546778899aabbccddeeff",
    ));
    expect(body).toMatchObject({
      workspace_id: "ws_scope_a",
      session_id: "s_scope_a",
    });
  });

  it("uses the latest shared planning capability for request capture", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    setPlanningModePref("planning");
    const bodies: Record<string, unknown>[] = [];
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        const index = bodies.length;
        return response(200, {
          turn_id: `t_planning_${index}`,
          session_id: `s_planning_${index}`,
          stream_url: `/api/v1/chat/stream/t_planning_${index}`,
        });
      }
      return response(200, {});
    }) as typeof fetch;

    type RenderProps = {
      planning: boolean;
      message: string | null;
      sendTick: number;
    };
    const hook = renderHook((props: RenderProps) => {
      capabilityMocks.planning = props.planning;
      return useTurnRunner({
        mode: "sse",
        message: props.message,
        workspaceId: "ws_1",
        sendTick: props.sendTick,
        submissionProtocolEnabled: false,
      });
    }, {
      initialProps: {
        planning: false,
        message: null,
        sendTick: 0,
      } as RenderProps,
    });

    hook.rerender({ planning: true, message: "planning on", sendTick: 1 });
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toHaveProperty("planning_mode", "planning");

    hook.rerender({ planning: false, message: "planning off", sendTick: 2 });
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).not.toHaveProperty("planning_mode");
  });

  // Three-state wiring (2026-08-12): `off` must reach the wire. This is the change's **wiring gate**:
  // backend `should_propose_plan(mode="off")` already returned False with unit coverage; what was missing was the frontend
  // sending the value. Conversely, `auto` must **not** appear in the body (backend defaults to Auto;
  // omission keeps normal-turn payload byte-for-byte identical to before wiring off).
  it("wires off onto the request and keeps auto off the wire", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    const bodies: Record<string, unknown>[] = [];
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        const index = bodies.length;
        return response(200, {
          turn_id: `t_tri_${index}`,
          session_id: `s_tri_${index}`,
          stream_url: `/api/v1/chat/stream/t_tri_${index}`,
        });
      }
      return response(200, {});
    }) as typeof fetch;

    type TriProps = { message: string | null; sendTick: number };
    setPlanningModePref("off");
    const hook = renderHook((props: TriProps) => {
      capabilityMocks.planning = true;
      return useTurnRunner({
        mode: "sse",
        message: props.message,
        workspaceId: "ws_1",
        sendTick: props.sendTick,
        submissionProtocolEnabled: false,
      });
    }, { initialProps: { message: null, sendTick: 0 } as TriProps });

    hook.rerender({ message: "never plan this", sendTick: 1 });
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toHaveProperty("planning_mode", "off");

    setPlanningModePref("auto");
    hook.rerender({ message: "auto decides", sendTick: 2 });
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).not.toHaveProperty("planning_mode");
  });

  it("uses exactly one legacy POST without submission metadata when disabled", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    const bodies: Record<string, unknown>[] = [];
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return response(200, {
          turn_id: "t_legacy_default",
          session_id: "s_legacy_default",
          stream_url: "/api/v1/chat/stream/t_legacy_default",
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "默认发送路径",
      workspaceId: "ws_1",
      sendTick: 1,
      submissionProtocolEnabled: false,
      submissionRecoveryScope: recoveryScope,
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe("t_legacy_default"));
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).not.toHaveProperty("client_submission_id");
    expect(bodies[0]).not.toHaveProperty("submission_contract_version");
    expect(bodies[0]).not.toHaveProperty("submission_kind");
  });

  it("emits the A discriminator and settles only after the accepted receipt", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return response(200, {
          turn_id: "t_a",
          session_id: "s_a",
          stream_url: "/api/v1/chat/stream/t_a",
        });
      }
      return response(200, {});
    }) as typeof fetch;
    const settled = vi.fn();

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "发送一次",
      workspaceId: "ws_1",
      sendTick: 1,
      submissionProtocolEnabled: true,
      submissionRecoveryScope: recoveryScope,
      onSendInitSettled: settled,
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe("t_a"));
    expect(body).toMatchObject({
      submission_kind: "start",
      submission_contract_version: "chat-submit.v1",
      model_control_batch: "a",
    });
    expect(String(body.client_submission_id)).toMatch(/^[0-9a-f-]{36}$/);
    expect(settled).toHaveBeenCalledWith("legacy_1", true, { turnId: "t_a", sessionId: "s_a" });
  });

  it("reconciles a response-loss through GET receipt without creating a new ID", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let submissionId = "";
    let postCount = 0;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const target = String(url);
      if (target.includes("/chat/send")) {
        postCount += 1;
        submissionId = String((JSON.parse(String(init?.body)) as Record<string, unknown>).client_submission_id);
        throw new TypeError("response lost");
      }
      if (target.includes("/chat/submissions/")) {
        expect(target).toContain(submissionId);
        return response(200, {
          outcome: "started",
          response: {
            turn_id: "t_recovered",
            session_id: "s_recovered",
            stream_url: "/api/v1/chat/stream/t_recovered",
          },
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "网络断了也不能重复",
      workspaceId: "ws_1",
      sendTick: 1,
      submissionProtocolEnabled: true,
      submissionRecoveryScope: recoveryScope,
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe("t_recovered"));
    expect(postCount).toBe(1);
  });

  it("preserves ordered attachment references without the submission protocol", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    const requests: Record<string, unknown>[] = [];
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        requests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return response(200, {
          turn_id: "t_noproto",
          session_id: "s_noproto",
          stream_url: "/api/v1/chat/stream/t_noproto",
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const attachments = [{
      fileId: "art_bundle",
      filename: "data.zip",
      size: 240,
      mime: "application/zip",
      bundleArtifactIds: ["art_data_a", "art_data_b"],
    }, {
      fileId: "art_tpl",
      filename: "deck-template.pptx",
      size: 29191,
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }];
    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "Use the first attachment as data and the second as the template.",
      attachments,
      workspaceId: "ws_1",
      sendTick: 1,
      submissionProtocolEnabled: false,
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe("t_noproto"));
    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({ attachments: ["art_data_a", "art_data_b", "art_tpl"] });
    expect(requests[0]).toHaveProperty("attachment_refs", attachments);
    expect(requests[0]).not.toHaveProperty("client_submission_id");
    expect(requests[0]).not.toHaveProperty("submission_contract_version");
  });

  it("starts an attachment-only turn", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    const requests: Record<string, unknown>[] = [];
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        requests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return response(200, {
          turn_id: "t_file",
          session_id: "s_file",
          stream_url: "/api/v1/chat/stream/t_file",
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "",
      attachments: [{
        fileId: "art_1",
        filename: "evidence.pdf",
        size: 10,
        mime: "application/pdf",
      }],
      workspaceId: "ws_1",
      sendTick: 1,
      submissionProtocolEnabled: true,
      submissionRecoveryScope: recoveryScope,
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe("t_file"));
    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({
      message: "",
      attachments: ["art_1"],
      attachment_refs: [{
        fileId: "art_1",
        filename: "evidence.pdf",
        size: 10,
        mime: "application/pdf",
      }],
    });
  });

  it("sends replay source identity without trusting the client preview", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return response(200, {
          turn_id: "t_replay",
          session_id: "s_replay",
          stream_url: "/api/v1/chat/stream/t_replay",
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "untrusted preview",
      attachments: [{
        fileId: "art_preview",
        filename: "preview.csv",
        size: 10,
        mime: "text/csv",
      }],
      workspaceId: "ws_1",
      sendTick: 1,
      submissionProtocolEnabled: true,
      submissionRecoveryScope: recoveryScope,
      replaySourceTurnId: "t_failed_source",
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe("t_replay"));
    expect(body).toMatchObject({
      message: "",
      submission_kind: "replay",
      replay_source_turn_id: "t_failed_source",
    });
    expect(body).not.toHaveProperty("attachments");
    expect(body).not.toHaveProperty("attachment_refs");
  });

  it("reuses the frozen semantic wire when preferences drift before F5 recovery", async () => {
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/chat/send")) {
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return response(200, {
          turn_id: "t_frozen",
          session_id: "s_frozen",
          stream_url: "/api/v1/chat/stream/t_frozen",
        });
      }
      return response(200, {});
    }) as typeof fetch;

    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "same semantic wire",
      workspaceId: "ws_1",
      sendTick: 1,
      submissionProtocolEnabled: true,
      submissionRecoveryScope: recoveryScope,
      submissionWireSnapshot: {
        composerSurface: "home",
        userProfile: { display_name: "frozen" },
        preferredProvider: "provider-before",
        preferredModel: "model-before",
        planningMode: "planning",
      },
    }));

    await waitFor(() => expect(hook.result.current.turnId).toBe("t_frozen"));
    expect(body).toMatchObject({
      user_profile: { display_name: "frozen" },
      preferred_provider: "provider-before",
      preferred_model: "model-before",
      planning_mode: "planning",
      client_info: { composer_surface: "home" },
    });
  });
});


describe("original resource editor sends", () => {
  const binding = { schema_version: 1 as const, user_id: "u_1", team_id: "team_1", workspace_id: "ws_1", session_id: "s_original",
    target: { kind: "skill" as const, id: "sk_original", source_kind: "user" as const, revision: `sha256:${"a".repeat(64)}` }, dirty: false, pending_write: false };

  it("sends the captured original resource without substituting a later editor", async () => {
    globalThis.fetch = vi.fn(async () => response(200, { session_id: "s_original", turn_id: "t_original", stream_url: "/api/v1/chat/stream/t_original" })) as typeof fetch;
    globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
    const hook = renderHook(() => useTurnRunner({ mode: "sse", message: "Edit this method", workspaceId: "ws_1", activeSessionId: "s_original", sendTick: 1,
      submissionWireSnapshot: { ...captureComposerSubmissionWireSnapshot("s_original", false), coachCanvasBinding: binding },
    }));
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    const body = JSON.parse(String(vi.mocked(globalThis.fetch).mock.calls[0][1]?.body));
    expect(JSON.parse(body.client_info.coach_canvas_target)).toEqual(binding);
    expect(body.client_info.agent_draft_id).toBeUndefined();
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_original"));
  });

  it("rejects a dirty recovered resource send before any HTTP preparation", async () => {
    globalThis.fetch = vi.fn() as typeof fetch;
    const settled = vi.fn();
    renderHook(() => useTurnRunner({ mode: "sse", message: "Edit this method", workspaceId: "ws_1", activeSessionId: "s_original", sendTick: 1,
      submissionWireSnapshot: { ...captureComposerSubmissionWireSnapshot("s_original", false), coachCanvasBinding: { ...binding, dirty: true } },
      onSendInitSettled: settled,
    }));
    await waitFor(() => expect(settled).toHaveBeenCalledWith(expect.any(String), false));
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});

describe("Continue sends", () => {
  it("carries the saved runs to resume with the visible message", async () => {
    globalThis.fetch = vi.fn(async () => response(200, { session_id: "s_1", turn_id: "t_continue", stream_url: "/api/v1/chat/stream/t_continue" })) as typeof fetch;
    globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
    const ids = ["rsc_0123456789abcdef0123", "rsc_abcdef0123456789abcd"];
    const hook = renderHook(() => useTurnRunner({ mode: "sse", message: "继续", workspaceId: "ws_1", activeSessionId: "s_1", sendTick: 1,
      submissionWireSnapshot: { ...captureComposerSubmissionWireSnapshot("s_1", false), continueCheckpointIds: ids },
    }));
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    const body = JSON.parse(String(vi.mocked(globalThis.fetch).mock.calls[0][1]?.body));
    expect(body.message).toBe("继续");
    expect(JSON.parse(body.client_info.continue_checkpoints)).toEqual(ids);
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_continue"));
  });
});
