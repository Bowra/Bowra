import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSubagent,
  deleteSubagent,
  getToolsCatalog,
  getSkillsCatalog,
  getSubagentManager,
  listBuiltinSubagents,
  listSubagents,
  patchSubagent,
  putBuiltinSubagentOverride,
  setSubagentAudience,
  setSubagentStatus,
  TeamSubagentApiError,
} from "./team-subagents-api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("team Subagent creation resources", () => {
  it("passes the server's text-only admission to the creation card", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ text_only_allowed: true, team_tools: [], native_tools: ["web_search"] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })));

    await expect(getToolsCatalog()).resolves.toEqual({
      text_only_allowed: true,
      team_tools: [],
      native_tools: ["web_search"],
    });
  });

  it("never offers Skill operation Tools even when an older server lists them", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ team_tools: [], native_tools: ["web_search", "skill_read", "skill_invoke", "skill_list"] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })));

    await expect(getToolsCatalog()).resolves.toMatchObject({ native_tools: ["web_search"] });
  });

  it("names each blocked Skill with its reason from detail.blocked_items", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({
        detail: {
          code: "agent_dependency_blocked",
          reason_codes: ["skill_pending_reapproval", "skill_risk_report_invalid"],
          blocked_items: [
            { kind: "skill", name: "Sales report", identity: "team:sk_1", reason_codes: ["skill_pending_reapproval"] },
            { kind: "skill", name: "Forecast", identity: "team:sk_2", reason_codes: ["skill_risk_report_invalid"] },
          ],
        },
      }), {
        status: 422,
        headers: { "content-type": "application/json" },
      })));

    const failure = await createSubagent({ name: "summary", tool_refs: [] }).catch((cause: unknown) => cause);
    expect(failure).toBeInstanceOf(TeamSubagentApiError);
    const error = failure as TeamSubagentApiError;
    expect(error.code).toBe("agent_dependency_blocked");
    expect(error.blockedItems.map((item) => item.name)).toEqual(["Sales report", "Forecast"]);
    expect(error.message.split("\n")).toEqual([
      "技能「Sales report」：已修改，正在等待团队管理员重新审核。请联系团队管理员审核新版本，或移除该技能。",
      "技能「Forecast」：安全检查记录已损坏。请重新上传该技能。",
    ]);
  });

  it("explains an exact Skill scope rejection without exposing raw server text", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({
        detail: { code: "agent_dependency_blocked", reason_codes: ["precise_skill_scope_required"] },
      }), {
        status: 422,
        headers: { "content-type": "application/json" },
      })));

    await expect(createSubagent({ name: "summary", tool_refs: ["skill_read"] })).rejects.toMatchObject({
      status: 422,
      code: "agent_dependency_blocked",
      message: expect.stringContaining("明确的技能范围"),
    });
  });

  it("points to the unselectable native tool", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({
        detail: { code: "subagent_tool_not_selectable", tool_ref: "skill_list" },
      }), {
        status: 422,
        headers: { "content-type": "application/json" },
      })));

    await expect(createSubagent({ name: "summary", tool_refs: ["skill_list"] })).rejects.toMatchObject({
      status: 422,
      code: "subagent_tool_not_selectable",
      message: expect.stringContaining("列出技能"),
    });
  });
});

describe("team Subagent API discriminators", () => {
  it("fails closed on the legacy full-definition list shape", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({
        items: [{
          id: "osa_1",
          system_prompt: "must not be accepted as a summary",
        }],
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })));

    await expect(listSubagents()).rejects.toMatchObject({
      status: 502,
      message: "Unsupported Subagent list response",
    } satisfies Partial<TeamSubagentApiError>);
  });

  it("requests explicit manager detail and checks its discriminator", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      new Response(JSON.stringify({
        schema_version: 1,
        kind: "team_owner_manager_detail",
        id: "osa_1",
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getSubagentManager("osa_1")).resolves.toMatchObject({
      schema_version: 1,
      kind: "team_owner_manager_detail",
      id: "osa_1",
    });
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      "/api/v1/team-subagents/osa_1?detail=manager",
    );
  });

  it("rejects an unknown manager detail schema version", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({
        schema_version: 2,
        kind: "team_owner_manager_detail",
        id: "osa_1",
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })));

    await expect(getSubagentManager("osa_1")).rejects.toMatchObject({
      status: 502,
      message: "Unsupported Subagent response",
    });
  });
});

describe("team Subagent optimistic concurrency", () => {
  const etag = 'W/"osa_1:4"';
  const detail = {
    schema_version: 1,
    kind: "team_owner_manager_detail",
    id: "osa_1",
    etag,
    version: 4,
  };

  it("sends If-Match to the split configuration endpoint", async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) =>
      new Response(JSON.stringify(detail), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    await patchSubagent("osa_1", { description: "changed" }, etag);

    const [input, init] = fetchMock.mock.calls[0];
    expect(String(input)).toContain(
      "/api/v1/team-subagents/osa_1/configuration",
    );
    expect(init?.method).toBe("PATCH");
    expect(new Headers(init?.headers).get("If-Match")).toBe(etag);
  });

  it("uses the dedicated status endpoint with warning acknowledgements", async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) =>
      new Response(JSON.stringify(detail), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    await setSubagentStatus("osa_1", "disabled", etag);

    const [input, init] = fetchMock.mock.calls[0];
    expect(String(input)).toContain(
      "/api/v1/team-subagents/osa_1/status",
    );
    expect(init?.method).toBe("PUT");
    expect(new Headers(init?.headers).get("If-Match")).toBe(etag);
    expect(JSON.parse(String(init?.body))).toEqual({
      status: "disabled",
      acknowledged_warning_codes: [],
    });
  });

  it("protects delete with If-Match", async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await deleteSubagent("osa_1", etag);

    const [input, init] = fetchMock.mock.calls[0];
    expect(String(input)).toContain("/api/v1/team-subagents/osa_1");
    expect(init?.method).toBe("DELETE");
    expect(new Headers(init?.headers).get("If-Match")).toBe(etag);
  });

  it("explains schedule-bound delete conflicts without exposing an error code", async () => {
    const previousLanguage = document.documentElement.lang;
    document.documentElement.lang = "en-US";
    try {
      vi.stubGlobal("fetch", vi.fn(async () =>
        new Response(JSON.stringify({
          detail: { code: "agent_delete_schedule_conflict" },
        }), {
          status: 409,
          headers: { "content-type": "application/json" },
        })));

      await expect(deleteSubagent("osa_1", etag)).rejects.toMatchObject({
        status: 409,
        code: "agent_delete_schedule_conflict",
        message: "This Agent is still used by an active or paused schedule. Remove or rebind those schedules before deleting it.",
      });
    } finally {
      document.documentElement.lang = previousLanguage;
    }
  });

  it("replaces the exact audience without a version precondition", async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) =>
      new Response(JSON.stringify(detail), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    await setSubagentAudience(
      "osa_1",
      {
        mode: "custom",
        user_ids: ["u_1"],
        group_ids: ["grp_1"],
      },
    );

    const [input, init] = fetchMock.mock.calls[0];
    expect(String(input)).toContain(
      "/api/v1/team-subagents/osa_1/audience",
    );
    expect(init?.method).toBe("PUT");
    expect(new Headers(init?.headers).get("If-Match")).toBeNull();
    expect(JSON.parse(String(init?.body))).toEqual({
      mode: "custom",
      user_ids: ["u_1"],
      group_ids: ["grp_1"],
    });
  });
});

describe("built-in Subagent removal", () => {
  it("explains that a stale built-in card was removed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: { error: { code: "builtin_subagent_removed" } },
          }),
          {
            status: 410,
            headers: { "content-type": "application/json" },
          },
        ),
      ),
    );

    await expect(putBuiltinSubagentOverride("canvas-writer", { enabled: false }, 'W/"builtin:canvas-writer:1"')).rejects.toMatchObject({
      status: 410,
      code: "builtin_subagent_removed",
      message: "该系统内置智能体已移除，已从当前列表隐藏。",
    } satisfies Partial<TeamSubagentApiError>);
  });
});

describe("built-in Subagent management errors", () => {
  it.each([
    ["builtin_override_version_conflict", "该系统内置 Agent 的配置版本已变化，已加载最新配置，请重试。"],
    ["builtin_override_store_unavailable", "系统内置 Agent 的团队配置尚未就绪或暂不可用，请联系系统管理员检查服务升级状态后重试。"],
  ])("publishes a concrete reason for %s", async (code, message) => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ detail: { error: { code } } }), {
        status: code === "builtin_override_version_conflict" ? 409 : 503,
        headers: { "content-type": "application/json" },
      })));

    await expect(putBuiltinSubagentOverride("consultant", { enabled: false }, 'W/"builtin:consultant:0"')).rejects.toMatchObject({
      code,
      message,
    } satisfies Partial<TeamSubagentApiError>);
  });

  it("always bypasses caches when loading ETags for built-in Agents", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ items: [], manageable: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    await listBuiltinSubagents();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/team-subagents/builtins",
      expect.objectContaining({ cache: "no-store", credentials: "include" }),
    );
  });
});


it("reads creation resource catalogs using the original draft scope", async () => {
  const context = { teamId: "team_original", workspaceId: "ws_original" };
  const fetch = vi.fn(async () => new Response(JSON.stringify({ native_tools: [], team_tools: [], builtin: [], team_skills: [] }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  await getToolsCatalog(context);
  await getSkillsCatalog(context);
  for (const [, init] of fetch.mock.calls as unknown as [string, RequestInit][]) {
    const headers = new Headers(init.headers);
    expect(headers.get("X-Team-Id")).toBe(context.teamId);
    expect(headers.get("X-Space-Id")).toBe(context.workspaceId);
  }
});


it("recovers created Agent metadata using the original draft scope", async () => {
  const context = { teamId: "team_original", workspaceId: "ws_original" };
  const fetch = vi.fn(async () => new Response(JSON.stringify({ schema_version: 1, kind: "team_owner_manager_detail", id: "osa_original" }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  await getSubagentManager("osa_original", context);
  const headers = new Headers((fetch.mock.calls as unknown as [string, RequestInit][])[0][1].headers);
  expect(headers.get("X-Team-Id")).toBe(context.teamId);
  expect(headers.get("X-Space-Id")).toBe(context.workspaceId);
});
