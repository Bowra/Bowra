import { afterEach, describe, expect, it, vi } from "vitest";

import {
  canonicalComposerStartSessionId,
  createComposerChatAction,
  isComposerChatActionResponse,
  isDefinitiveComposerChatActionRejection,
  lookupComposerChatAction,
  singleAttemptChatActionFetch,
} from "./composer-chat-action";
import {
  chatReplayAdmission,
  legacyChatActionAttachmentsSupported,
  maySkipChatExecutionPreflight,
  shouldRouteSecondaryChatEntryThroughComposer,
  terminalChatActionAttachmentsSupported,
} from "./terminal-chat-capability";

const originalFetch = globalThis.fetch;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  vi.useRealTimers();
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("Terminal v1 Composer action client", () => {
  it("keeps legacy attachment steer only behind the submission journal", () => {
    const attachment = {
      fileId: "art_1",
      filename: "evidence.pdf",
      size: 1,
      mime: "application/pdf",
    };

    expect(legacyChatActionAttachmentsSupported("start", [attachment], false))
      .toBe(true);
    expect(legacyChatActionAttachmentsSupported("steer", [attachment], false))
      .toBe(false);
    expect(legacyChatActionAttachmentsSupported("steer", [attachment], true))
      .toBe(true);
  });

  it("fails closed on typed attachments until the serving cluster advertises v1", () => {
    const attachment = {
      fileId: "art_1",
      filename: "evidence.pdf",
      size: 1,
      mime: "application/pdf",
    };
    const bootstrap = {
      write_protocol: "terminal_v1" as const,
      config_generation: "config-1",
      readiness_generation: "ready-1",
    };

    expect(terminalChatActionAttachmentsSupported("start", [], bootstrap))
      .toBe(true);
    expect(terminalChatActionAttachmentsSupported("start", [attachment], bootstrap))
      .toBe(false);
    expect(terminalChatActionAttachmentsSupported("start", [attachment], {
      ...bootstrap,
      attachment_refs_wire_version: 1,
    })).toBe(true);
    expect(terminalChatActionAttachmentsSupported("steer", [attachment], {
      ...bootstrap,
      attachment_refs_wire_version: 1,
    })).toBe(false);
  });

  it("allows replay only under an explicit legacy authority decision", () => {
    const bootstrap = (writeProtocol: "terminal_v1" | "legacy_v0" | "refresh_required" | "unavailable") => ({
      write_protocol: writeProtocol,
      config_generation: "config-1",
      readiness_generation: "ready-1",
    });

    expect(chatReplayAdmission(bootstrap("legacy_v0"))).toBe("legacy_v0");
    expect(chatReplayAdmission(bootstrap("refresh_required")))
      .toBe("refresh_required");
    expect(chatReplayAdmission(bootstrap("terminal_v1"))).toBe("unavailable");
    expect(chatReplayAdmission(bootstrap("unavailable"))).toBe("unavailable");
    expect(chatReplayAdmission(null)).toBe("unavailable");
  });
  it("preflights an existing session instead of borrowing the Home legacy hint", () => {
    const homeLegacy = {
      write_protocol: "legacy_v0" as const,
      config_generation: "config-2",
      readiness_generation: "ready-2",
    };

    expect(maySkipChatExecutionPreflight("s_sticky_v1", false, null)).toBe(false);
    expect(maySkipChatExecutionPreflight(null, false, homeLegacy)).toBe(true);
    expect(maySkipChatExecutionPreflight("s_legacy", true, homeLegacy)).toBe(true);
  });

  it("routes secondary entry points through Composer only when durable authority may apply", () => {
    const legacy = {
      write_protocol: "legacy_v0" as const,
      config_generation: "config-2",
      readiness_generation: "ready-2",
    };
    const terminal = {
      ...legacy,
      write_protocol: "terminal_v1" as const,
    };

    expect(shouldRouteSecondaryChatEntryThroughComposer(null, false, null))
      .toBe(false);
    expect(shouldRouteSecondaryChatEntryThroughComposer(null, false, legacy))
      .toBe(false);
    expect(shouldRouteSecondaryChatEntryThroughComposer(null, false, terminal))
      .toBe(true);
    expect(shouldRouteSecondaryChatEntryThroughComposer("s_unknown", false, null))
      .toBe(true);
    expect(shouldRouteSecondaryChatEntryThroughComposer("s_legacy", true, legacy))
      .toBe(false);
  });

  it("derives the bounded start identity from one random suffix", () => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(
      "00112233-4455-4677-8899-aabbccddeeff",
    );

    const action = createComposerChatAction({
      actionKind: "start",
      sourceRevision: "revision-1",
    });

    expect(action).toEqual({
      actionId: "act_start_00112233445546778899aabbccddeeff",
      actionKind: "start",
      attempted: true,
      sourceRevision: "revision-1",
      sessionId: null,
    });
  });

  it("freezes the exact steer target in the marker", () => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(
      "ffeeddcc-bbaa-4988-8766-554433221100",
    );

    expect(createComposerChatAction({
      actionKind: "steer",
      sourceRevision: "revision-2",
      sessionId: "s_1",
      expectedTurnId: "t_1",
      expectedExecutionGeneration: 3,
    })).toMatchObject({
      actionId: "act_steer_ffeeddccbbaa49888766554433221100",
      actionKind: "steer",
      sessionId: "s_1",
      expectedTurnId: "t_1",
      expectedExecutionGeneration: 3,
    });
  });

  it("navigates only accepted start actions to a canonical session", () => {
    expect(canonicalComposerStartSessionId({
      actionId: "act_start_00112233445546778899aabbccddeeff",
      actionKind: "start",
      attempted: true,
      sourceRevision: "revision-1",
      sessionId: null,
    })).toBe("s_00112233445546778899aabbccddeeff");
    expect(canonicalComposerStartSessionId({
      actionId: "act_steer_ffeeddccbbaa49888766554433221100",
      actionKind: "steer",
      attempted: true,
      sourceRevision: "revision-2",
      sessionId: "s_1",
      expectedTurnId: "t_1",
      expectedExecutionGeneration: 3,
    })).toBeNull();
  });

  it("never replays a marker-backed mutation after a 401", async () => {
    globalThis.fetch = vi.fn(async () => jsonResponse(401, {
      detail: { error: { code: "unauthorized" } },
    })) as typeof fetch;

    const response = await singleAttemptChatActionFetch("/api/v1/chat/send", {
      method: "POST",
    });

    expect(response.status).toBe(401);
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/v1/chat/send",
      expect.objectContaining({ redirect: "error" }),
    );
  });

  it("retains post-admission-like and unknown 409 outcomes for receipt lookup", () => {
    const start = createComposerChatAction({
      actionKind: "start",
      sourceRevision: "revision-1",
    });
    const steer = createComposerChatAction({
      actionKind: "steer",
      sourceRevision: "revision-2",
      sessionId: "s_1",
      expectedTurnId: "t_1",
      expectedExecutionGeneration: 2,
    });
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "terminal_dependency_recovering",
    }, start)).toBe(false);
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "terminal_protocol_admission_rejected",
    }, start)).toBe(false);
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "session_turn_active",
    }, start)).toBe(false);
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "terminal_protocol_admission_rejected",
    }, steer)).toBe(false);
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "session_turn_active",
    }, steer)).toBe(true);
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "session_transitioning",
    }, start)).toBe(false);
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "session_transitioning",
    }, steer)).toBe(true);
    expect(isDefinitiveComposerChatActionRejection({ status: 409 })).toBe(false);
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "session_turn_active",
    })).toBe(false);
  });

  it("clears only proven pre-admission rejections", () => {
    const start = createComposerChatAction({
      actionKind: "start",
      sourceRevision: "revision-start",
    });
    const steer = createComposerChatAction({
      actionKind: "steer",
      sourceRevision: "revision-steer",
      sessionId: "s_1",
      expectedTurnId: "t_1",
      expectedExecutionGeneration: 2,
    });
    expect(isDefinitiveComposerChatActionRejection({
      status: 401,
      code: "auth_required",
    })).toBe(true);
    expect(isDefinitiveComposerChatActionRejection({ status: 409, code: "chart_selection_unavailable" }, start)).toBe(true);
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "team_model_unconfigured",
    })).toBe(true);
    expect(isDefinitiveComposerChatActionRejection({
      status: 409,
      code: "subagent_draft_unsaved_changes",
    }, start)).toBe(true);
    expect(isDefinitiveComposerChatActionRejection({
      status: 422,
      code: "request_validation_failed",
    })).toBe(true);
    expect(isDefinitiveComposerChatActionRejection({
      status: 503,
      code: "planning_unavailable",
    })).toBe(true);
    for (const code of [
      "plan_already_running",
      "plan_not_approvable",
      "plan_not_found",
    ]) {
      expect(isDefinitiveComposerChatActionRejection({
        status: code === "plan_not_found" ? 404 : 409,
        code,
      }, start)).toBe(true);
    }
    for (const code of [
      "invalid_token",
      "password_changed_relogin_required",
      "user_inactive",
      "email_not_verified",
    ]) {
      expect(isDefinitiveComposerChatActionRejection({
        status: code === "user_inactive" || code === "email_not_verified" ? 403 : 401,
        code,
      })).toBe(true);
    }
    for (const code of [
      "over_grace",
      "suspended",
      "needs_license",
      "tamper",
      "clock_tamper",
    ]) {
      expect(isDefinitiveComposerChatActionRejection({
        status: 402,
        code,
      })).toBe(true);
    }
    for (const code of [
      "attachment_validation_unavailable",
      "authz_unavailable",
      "datarc_identity_required",
      "datarc_member_not_found",
      "datarc_member_resolve_failed",
      "datarc_unavailable",
      "entitlement_unavailable",
      "member_not_in_workspace",
      "orchestrator_not_ready",
      "public_entry_team_mismatch",
      "team_membership_required",
    ]) {
      expect(isDefinitiveComposerChatActionRejection({
        status: 503,
        code,
      }, start)).toBe(true);
      expect(isDefinitiveComposerChatActionRejection({
        status: 503,
        code,
      }, steer)).toBe(true);
    }
    expect(isDefinitiveComposerChatActionRejection({
      status: 503,
      code: "authz_lookup_failed",
    }, start)).toBe(false);
    expect(isDefinitiveComposerChatActionRejection({
      status: 503,
      code: "authz_lookup_failed",
    }, steer)).toBe(false);
  });

  it("keeps the deadline active while the response body is stalled", async () => {
    vi.useFakeTimers();
    globalThis.fetch = vi.fn(async (_input, init) => new Response(
      new ReadableStream({
        start(controller) {
          init?.signal?.addEventListener("abort", () => {
            controller.error(init.signal?.reason);
          }, { once: true });
        },
      }),
      { status: 200 },
    )) as typeof fetch;

    const pending = singleAttemptChatActionFetch("/api/v1/chat/send", {
      method: "POST",
    });
    const rejection = expect(pending).rejects.toBeTruthy();
    await vi.advanceTimersByTimeAsync(15_000);
    await rejection;
    vi.useRealTimers();
  });

  it("lets an @ send take as long as an ordinary send (no client deadline), still one attempt", async () => {
    vi.useFakeTimers();
    let release!: () => void;
    globalThis.fetch = vi.fn(async (_input, init) => {
      expect(init?.redirect).toBe("error");
      await new Promise<void>((resolve) => { release = resolve; });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;

    let settled = false;
    const pending = singleAttemptChatActionFetch("/api/v1/chat/send", { method: "POST" }, null)
      .finally(() => { settled = true; });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(settled).toBe(false);
    release();
    expect((await pending).status).toBe(200);
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("bounds lookup even while an automatic refresh body is stalled", async () => {
    vi.useFakeTimers();
    globalThis.fetch = vi.fn(async (input, init) => {
      if (String(input).includes("/api/v1/auth/refresh")) {
        return new Response(new ReadableStream({
          start(controller) {
            init?.signal?.addEventListener("abort", () => {
              controller.error(init.signal?.reason);
            }, { once: true });
          },
        }), { status: 200 });
      }
      return jsonResponse(401, {
        detail: { error: { code: "unauthorized" } },
      });
    }) as typeof fetch;

    const action = createComposerChatAction({
      actionKind: "start",
      sourceRevision: "revision-1",
    });
    const pending = lookupComposerChatAction(action);
    const assertion = expect(pending).rejects.toMatchObject({
      name: "TimeoutError",
    });
    await vi.advanceTimersByTimeAsync(15_000);
    await assertion;
    await vi.advanceTimersByTimeAsync(5_000);
  });

  it("rejects a start response with the wrong generation or stream identity", () => {
    const action = {
      actionId: "act_start_00112233445546778899aabbccddeeff",
      actionKind: "start" as const,
      attempted: true as const,
      sourceRevision: "revision-1",
      sessionId: null,
    };
    const valid = {
      status: "started",
      turn_id: "t_00112233445546778899aabbccddeeff",
      session_id: "s_00112233445546778899aabbccddeeff",
      stream_url: "/api/v1/chat/stream/t_00112233445546778899aabbccddeeff",
      execution_generation: 1,
      terminal_protocol_version: 1,
    };

    expect(isComposerChatActionResponse({
      ...valid,
      execution_generation: 2,
    }, action)).toBe(false);
    expect(isComposerChatActionResponse({
      ...valid,
      stream_url: "/api/v1/chat/stream/t_other",
    }, action)).toBe(false);
  });

  it("looks up a steer marker with its frozen scope and validates the receipt", async () => {
    let requestedUrl = "";
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      requestedUrl = String(input);
      return jsonResponse(200, {
        action_id: "act_steer_00112233445546778899aabbccddeeff",
        action_kind: "steer",
        outcome: "accepted",
        session_id: "s_1",
        turn_id: "t_1",
        execution_generation: 3,
        terminal_protocol_version: 1,
        stream_url: "/api/v1/chat/stream/t_1",
      });
    }) as typeof fetch;

    const receipt = await lookupComposerChatAction({
      actionId: "act_steer_00112233445546778899aabbccddeeff",
      actionKind: "steer",
      attempted: true,
      sourceRevision: "revision-2",
      sessionId: "s_1",
      expectedTurnId: "t_1",
      expectedExecutionGeneration: 3,
    });

    expect(requestedUrl).toContain("/api/v1/chat/actions/act_steer_");
    expect(requestedUrl).toContain("session_id=s_1");
    expect(requestedUrl).toContain("expected_turn_id=t_1");
    expect(requestedUrl).toContain("expected_execution_generation=3");
    expect(receipt.turn_id).toBe("t_1");
  });

  it("rejects a start lookup receipt with the wrong generation or stream", async () => {
    const action = {
      actionId: "act_start_00112233445546778899aabbccddeeff",
      actionKind: "start" as const,
      attempted: true as const,
      sourceRevision: "revision-1",
      sessionId: null,
    };
    const valid = {
      action_id: action.actionId,
      action_kind: "start",
      outcome: "admitted",
      session_id: "s_00112233445546778899aabbccddeeff",
      turn_id: "t_00112233445546778899aabbccddeeff",
      execution_generation: 1,
      terminal_protocol_version: 1,
      stream_url: "/api/v1/chat/stream/t_00112233445546778899aabbccddeeff",
    };
    globalThis.fetch = vi.fn()
      .mockResolvedValueOnce(jsonResponse(200, {
        ...valid,
        execution_generation: 2,
      }))
      .mockResolvedValueOnce(jsonResponse(200, {
        ...valid,
        stream_url: "/api/v1/chat/stream/t_other",
      })) as typeof fetch;

    await expect(lookupComposerChatAction(action)).rejects.toThrow(
      "composer chat action receipt is invalid",
    );
    await expect(lookupComposerChatAction(action)).rejects.toThrow(
      "composer chat action receipt is invalid",
    );
  });
});

it("requires the original draft in both POST and read-only recovery receipts", async () => {
  const target = { kind: "agent_draft" as const, id: "sad_original", version: 3 };
  const action = createComposerChatAction({ actionKind: "start", sourceRevision: "rev_1", agentDraftTarget: target });
  target.version = 4;
  const suffix = action.actionId.slice("act_start_".length);
  const savedTarget = { kind: "agent_draft", id: "sad_original", version: 3 };
  const receipt = {
    action_id: action.actionId, action_kind: "start", outcome: "admitted", status: "started",
    session_id: `s_${suffix}`, turn_id: `t_${suffix}`, stream_url: `/api/v1/chat/stream/t_${suffix}`,
    execution_generation: 1, terminal_protocol_version: 1, agent_draft_target: savedTarget,
  };
  expect(isComposerChatActionResponse(receipt, action)).toBe(true);
  for (const replacement of [undefined, null, { ...savedTarget, id: "sad_other" }, { ...savedTarget, version: 4 }, { ...savedTarget, grant: "write" }]) {
    const wrong = { ...receipt, agent_draft_target: replacement };
    expect(isComposerChatActionResponse(wrong, action)).toBe(false);
    globalThis.fetch = vi.fn(async () => jsonResponse(200, wrong)) as typeof fetch;
    await expect(lookupComposerChatAction(action)).rejects.toThrow("receipt is invalid");
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  }
  globalThis.fetch = vi.fn(async () => jsonResponse(200, receipt)) as typeof fetch;
  expect(await lookupComposerChatAction(action)).toMatchObject({ agent_draft_target: savedTarget });
});
