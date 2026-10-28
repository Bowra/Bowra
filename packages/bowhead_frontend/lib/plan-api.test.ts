import { afterEach, describe, expect, it, vi } from "vitest";

import { amendPlan, approvePlanViaSend, patchPlanSteps, replanPreview, reviseStepPreview } from "./plan-api";
import { viewerTimeZone } from "./time-zone";

const originalFetch = globalThis.fetch;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function sessionBootstrap(
  writeProtocol: "terminal_v1" | "legacy_v0" | "refresh_required" | "unavailable",
): Response {
  return jsonResponse(200, {
    chat_execution: {
      write_protocol: writeProtocol,
      config_generation: "config-1",
      readiness_generation: "ready-1",
    },
    session: {},
    turns: [],
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("Plan previews send the viewer zone", () => {
  it.each([
    ["amend", () => amendPlan("plan_1", "add a step", { workspaceId: "ws_1", userId: "u_1" })],
    ["revise", () => reviseStepPreview("plan_1", "step_1", { workspaceId: "ws_1" })],
    ["replan", () => replanPreview("plan_1", { workspaceId: "ws_1" })],
  ])("%s preview dates its model call on the viewer's calendar", async (_name, call) => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse(200, {}));
    globalThis.fetch = fetcher as typeof fetch;
    await call();
    expect(new Headers(fetcher.mock.calls[0][1].headers).get("X-Bowhead-Time-Zone")).toBe(viewerTimeZone());
  });
});

describe("Plan edit revision", () => {
  it("sends the displayed revision with the edit and reads the committed revision", async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse(200, { plan_id: "plan_1", status: "proposed", current_rev: 4, steps: [] }));
    globalThis.fetch = fetcher as typeof fetch;
    const result = await patchPlanSteps("plan_1", [{ id: "step_1", agent: "compute", objective: "compare sales", level: 0, dependsOn: [], needsCorpus: false, status: "pending", included: true }], { workspaceId: "ws_1", userId: "u_1", expectedRev: 3 });
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toMatchObject({ expected_rev: 3, workspace_id: "ws_1" });
    expect(result.current_rev).toBe(4);
  });

  it.each([undefined, 0, -1, 1.5, Number.NaN])("refuses to save an unversioned preview (%s)", async (expectedRev) => {
    const fetcher = vi.fn();
    globalThis.fetch = fetcher as typeof fetch;
    await expect(patchPlanSteps("plan_1", [], { workspaceId: "ws_1", userId: "u_1", expectedRev })).rejects.toMatchObject({ code: "plan_not_approvable" });
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("Plan approval chat routing", () => {
  it.each([undefined, 0, -1, 1.5, Number.NaN])("requires a displayed revision before approval (%s)", async (expectedRev) => {
    const fetcher = vi.fn();
    globalThis.fetch = fetcher as typeof fetch;
    await expect(approvePlanViaSend("plan_unversioned", { workspaceId: "ws_1", sessionId: "s_1", expectedRev })).rejects.toMatchObject({ code: "plan_not_approvable" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("requires a fresh confirmation when ambiguous admission resolves to a changed plan", async () => {
    let postCount = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/api/v1/sessions/")) return sessionBootstrap("terminal_v1");
      if (init?.method === "POST") {
        postCount += 1;
        throw new TypeError("response lost");
      }
      if (String(input).includes("/state?")) return jsonResponse(200, { plan: { status: "proposed", retryable: true, current_rev: 2 }, steps: [] });
      return jsonResponse(404, { detail: { error: { code: "chat_action_not_found" } } });
    }) as typeof fetch;
    const identity = { workspaceId: "ws_1", sessionId: "s_changed", expectedRev: 1 };
    await expect(approvePlanViaSend("plan_changed_revision", identity)).rejects.toThrow("response lost");
    await expect(approvePlanViaSend("plan_changed_revision", identity)).rejects.toMatchObject({ code: "plan_not_approvable" });
    expect(postCount).toBe(1);
  });

  it("does not choose a writer without the exact Plan Session", async () => {
    const fetcher = vi.fn();
    globalThis.fetch = fetcher as typeof fetch;

    const error = await approvePlanViaSend("plan_1", {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: null,
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(Error);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("keeps legacy approval on the server-selected legacy writer", async () => {
    const requests: Array<{ input: string; init?: RequestInit }> = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input: String(input), init });
      if (!init?.method) return sessionBootstrap("legacy_v0");
      return jsonResponse(200, {
        turn_id: "t_legacy",
        stream_url: "/api/v1/chat/stream/t_legacy",
        status: "started",
      });
    }) as typeof fetch;

    await expect(approvePlanViaSend("plan_1", {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan",
    })).resolves.toMatchObject({ turn_id: "t_legacy" });

    expect(requests).toHaveLength(2);
    expect(new Headers(requests[0].init?.headers).get(
      "X-Bowhead-Terminal-Protocol-Capability",
    )).toBe("1");
    const body = JSON.parse(String(requests[1].init?.body)) as Record<string, unknown>;
    expect(body).toMatchObject({
      workspace_id: "ws_1",
      session_id: "s_plan",
      plan_execution_id: "plan_1",
      expected_plan_revision: 1,
    });
    expect(body).not.toHaveProperty("client_info");
    // The execution Turn's model "today" follows the approving viewer's zone.
    expect(new Headers(requests[1].init?.headers).get("X-Bowhead-Time-Zone")).toBe(viewerTimeZone());
  });

  it("does not issue a second mutation after the selected legacy writer rejects", async () => {
    const requests: Array<{ input: string; init?: RequestInit }> = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input: String(input), init });
      if (!init?.method) return sessionBootstrap("legacy_v0");
      return jsonResponse(409, {
        detail: { error: { code: "client_refresh_required" } },
      });
    }) as typeof fetch;

    await expect(approvePlanViaSend("plan_1", {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan",
    })).rejects.toMatchObject({ code: "client_refresh_required" });

    expect(requests).toHaveLength(2);
    expect(requests.filter(({ init }) => init?.method === "POST")).toHaveLength(1);
  });

  it("sends a Terminal plan approval with one canonical start action", async () => {
    let postCount = 0;
    let postedBody: Record<string, unknown> = {};
    let postedHeaders: HeadersInit | undefined;
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (!init?.method) return sessionBootstrap("terminal_v1");
      postCount += 1;
      postedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
      postedHeaders = init.headers;
      const clientInfo = postedBody.client_info as Record<string, string>;
      const suffix = clientInfo.request_id.slice("act_start_".length);
      return jsonResponse(200, {
        session_id: "s_plan",
        turn_id: `t_${suffix}`,
        stream_url: `/api/v1/chat/stream/t_${suffix}`,
        status: "started",
        execution_generation: 1,
        terminal_protocol_version: 1,
      });
    }) as typeof fetch;

    const result = await approvePlanViaSend("plan_1", {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan",
    });

    expect(postCount).toBe(1);
    expect(result.status).toBe("started");
    expect(new Headers(postedHeaders).get(
      "X-Bowhead-Terminal-Protocol-Capability",
    )).toBe("1");
    expect(postedBody).toMatchObject({
      workspace_id: "ws_1",
      session_id: "s_plan",
      plan_execution_id: "plan_1",
      expected_plan_revision: 1,
      client_info: {
        action_kind: "start",
      },
    });
    const clientInfo = postedBody.client_info as Record<string, string>;
    expect(clientInfo.request_id).toMatch(/^act_start_[0-9a-f]{32}$/);
    expect(clientInfo.user_msg_id).toBe(
      `u_s_${clientInfo.request_id.slice("act_start_".length)}`,
    );
  });

  it("coalesces concurrent approval clicks into one writer attempt", async () => {
    let preflightCount = 0;
    let postCount = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/api/v1/sessions/")) {
        preflightCount += 1;
        return sessionBootstrap("terminal_v1");
      }
      postCount += 1;
      const body = JSON.parse(String(init?.body)) as {
        client_info: { request_id: string };
      };
      const suffix = body.client_info.request_id.slice("act_start_".length);
      await Promise.resolve();
      return jsonResponse(200, {
        session_id: "s_plan_double",
        turn_id: `t_${suffix}`,
        stream_url: `/api/v1/chat/stream/t_${suffix}`,
        status: "started",
        execution_generation: 1,
        terminal_protocol_version: 1,
      });
    }) as typeof fetch;

    const identity = {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan_double",
    };
    const [first, second] = await Promise.all([
      approvePlanViaSend("plan_double", identity),
      approvePlanViaSend("plan_double", identity),
    ]);

    expect(first).toEqual(second);
    expect(preflightCount).toBe(1);
    expect(postCount).toBe(1);
  });

  it("recovers an ambiguous Terminal POST through action lookup without replay", async () => {
    let postCount = 0;
    let actionId = "";
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/api/v1/sessions/")) {
        return sessionBootstrap("terminal_v1");
      }
      if (init?.method === "POST") {
        postCount += 1;
        const body = JSON.parse(String(init.body)) as {
          client_info: { request_id: string };
        };
        actionId = body.client_info.request_id;
        throw new TypeError("connection reset after commit");
      }
      const suffix = actionId.slice("act_start_".length);
      return jsonResponse(200, {
        action_id: actionId,
        action_kind: "start",
        outcome: "admitted",
        session_id: "s_plan",
        turn_id: `t_${suffix}`,
        execution_generation: 1,
        terminal_protocol_version: 1,
        stream_url: `/api/v1/chat/stream/t_${suffix}`,
      });
    }) as typeof fetch;

    await expect(approvePlanViaSend("plan_1", {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan",
    })).resolves.toMatchObject({ status: "started" });
    expect(postCount).toBe(1);
  });

  it("keeps the exact action when a concurrent Plan winner commits after a 404", async () => {
    let postCount = 0;
    let lookupCount = 0;
    let actionId = "";
    let winnerCommitted = false;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/sessions/")) {
        return sessionBootstrap("terminal_v1");
      }
      if (init?.method === "POST") {
        postCount += 1;
        const body = JSON.parse(String(init.body)) as {
          client_info: { request_id: string };
        };
        actionId = body.client_info.request_id;
        return jsonResponse(503, {
          detail: { error: { code: "terminal_dependency_recovering" } },
        });
      }
      lookupCount += 1;
      if (!winnerCommitted) {
        return jsonResponse(404, {
          detail: { error: { code: "chat_action_not_found" } },
        });
      }
      const suffix = actionId.slice("act_start_".length);
      return jsonResponse(200, {
        action_id: actionId,
        action_kind: "start",
        outcome: "admitted",
        session_id: "s_plan_claim_race",
        turn_id: `t_${suffix}`,
        execution_generation: 1,
        terminal_protocol_version: 1,
        stream_url: `/api/v1/chat/stream/t_${suffix}`,
      });
    }) as typeof fetch;

    const identity = {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan_claim_race",
    };
    await expect(
      approvePlanViaSend("plan_claim_race", identity),
    ).rejects.toMatchObject({ code: "terminal_dependency_recovering" });
    winnerCommitted = true;
    await expect(
      approvePlanViaSend("plan_claim_race", identity),
    ).resolves.toMatchObject({ status: "started" });

    expect(postCount).toBe(1);
    expect(lookupCount).toBe(2);
  });

  it("starts a new action only after lookup proves the first action absent", async () => {
    let postCount = 0;
    let lookupCount = 0;
    const actionIds: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/sessions/")) {
        return sessionBootstrap("terminal_v1");
      }
      if (init?.method === "POST") {
        postCount += 1;
        const body = JSON.parse(String(init.body)) as {
          client_info: { request_id: string };
        };
        const actionId = body.client_info.request_id;
        actionIds.push(actionId);
        if (postCount === 1) {
          throw new TypeError("connection reset before admission");
        }
        const suffix = actionId.slice("act_start_".length);
        return jsonResponse(200, {
          session_id: "s_plan_unknown",
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        });
      }
      if (url.includes("/state?")) {
        return jsonResponse(200, {
          plan: { id: "plan_unknown", status: "proposed", retryable: true, current_rev: 1 },
          steps: [],
        });
      }
      lookupCount += 1;
      return jsonResponse(404, {
        detail: { error: { code: "chat_action_not_found" } },
      });
    }) as typeof fetch;

    const identity = {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan_unknown",
    };
    await expect(approvePlanViaSend("plan_unknown", identity)).rejects.toThrow(
      "connection reset before admission",
    );
    await expect(approvePlanViaSend("plan_unknown", identity)).resolves.toMatchObject({
      status: "started",
    });

    expect(postCount).toBe(2);
    // One lookup follows the ambiguous POST; the later explicit click checks
    // the same identity again before authoritative Plan state permits retry.
    expect(lookupCount).toBe(2);
    expect(actionIds).toHaveLength(2);
    expect(actionIds[0]).not.toBe(actionIds[1]);
  });

  it("reconciles an unresolved action before a later explicit retry", async () => {
    let postCount = 0;
    let lookupCount = 0;
    const actionIds: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/sessions/")) {
        return sessionBootstrap("terminal_v1");
      }
      if (init?.method === "POST") {
        postCount += 1;
        const body = JSON.parse(String(init.body)) as {
          client_info: { request_id: string };
        };
        const actionId = body.client_info.request_id;
        actionIds.push(actionId);
        if (postCount === 1) throw new TypeError("response lost");
        const suffix = actionId.slice("act_start_".length);
        return jsonResponse(200, {
          session_id: "s_plan_reconcile",
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        });
      }
      if (url.includes("/state?")) {
        return jsonResponse(200, {
          plan: { id: "plan_reconcile", status: "proposed", retryable: true, current_rev: 1 },
          steps: [],
        });
      }
      lookupCount += 1;
      if (lookupCount === 1) throw new TypeError("lookup timeout");
      return jsonResponse(404, {
        detail: { error: { code: "chat_action_not_found" } },
      });
    }) as typeof fetch;

    const identity = {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan_reconcile",
    };
    await expect(approvePlanViaSend("plan_reconcile", identity)).rejects.toThrow(
      "response lost",
    );
    await expect(approvePlanViaSend("plan_reconcile", identity)).resolves.toMatchObject({
      status: "started",
    });

    expect(postCount).toBe(2);
    expect(lookupCount).toBe(2);
    expect(actionIds).toHaveLength(2);
    expect(actionIds[0]).not.toBe(actionIds[1]);
  });

  it("resolves a pending Terminal action before a writer rollback", async () => {
    let preflightCount = 0;
    let postCount = 0;
    let lookupCount = 0;
    let actionId = "";
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/sessions/")) {
        preflightCount += 1;
        return sessionBootstrap(
          preflightCount === 1 ? "terminal_v1" : "legacy_v0",
        );
      }
      if (init?.method === "POST") {
        postCount += 1;
        const body = JSON.parse(String(init.body)) as {
          client_info: { request_id: string };
        };
        actionId = body.client_info.request_id;
        throw new TypeError("response lost");
      }
      lookupCount += 1;
      if (lookupCount === 1) throw new TypeError("lookup timeout");
      const suffix = actionId.slice("act_start_".length);
      return jsonResponse(200, {
        action_id: actionId,
        action_kind: "start",
        outcome: "admitted",
        session_id: "s_plan_rollback",
        turn_id: `t_${suffix}`,
        execution_generation: 1,
        terminal_protocol_version: 1,
        stream_url: `/api/v1/chat/stream/t_${suffix}`,
      });
    }) as typeof fetch;

    const identity = {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan_rollback",
    };
    await expect(approvePlanViaSend("plan_rollback", identity)).rejects.toThrow(
      "response lost",
    );
    await expect(approvePlanViaSend("plan_rollback", identity)).resolves.toMatchObject({
      status: "started",
    });

    expect(preflightCount).toBe(1);
    expect(postCount).toBe(1);
    expect(lookupCount).toBe(2);
  });

  it("fails closed when the Session requires refresh", async () => {
    let postCount = 0;
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") postCount += 1;
      return sessionBootstrap("refresh_required");
    }) as typeof fetch;

    const error = await approvePlanViaSend("plan_1", {
      workspaceId: "ws_1",
      expectedRev: 1,
      sessionId: "s_plan",
    }).catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      code: "client_refresh_required",
      status: 409,
    });
    expect(postCount).toBe(0);
  });
});
