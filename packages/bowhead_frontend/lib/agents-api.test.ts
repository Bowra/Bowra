import { afterEach, describe, expect, it, vi } from "vitest";

import {
  forkAgent,
  acceptAgentProposalSuggestedCases,
  type AgentChangeProposal,
  getAgentCapabilityReport,
  getAgentDetail,
  getAgentUnhireImpact,
  listAgentCatalog,
  setAgentPersonalStatus,
  startAgentVersionComparison,
  type AgentVersionWorkbench,
  unhireAgent,
} from "./agents-api";

afterEach(() => vi.unstubAllGlobals());

describe("Agent Marketplace API", () => {
  it("encodes polymorphic agent ids in consumer detail paths", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ agent_id: "system:analyst" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await getAgentDetail("system:analyst");

    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/agents/system%3Aanalyst");
  });

  it("requests localized capability evidence for polymorphic agent ids", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify({ kind: "agent_capability_report" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await getAgentCapabilityReport("system:analyst", "zh-CN");

    const url = new URL(String(fetchMock.mock.calls[0][0]), "https://example.test");
    expect(url.pathname).toContain("/api/v1/agents/system%3Aanalyst/capabilities");
    expect(url.searchParams.get("locale")).toBe("zh-CN");
  });

  it("binds catalog filters to a stable pagination cursor", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify({ items: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await listAgentCatalog({
      q: "finance",
      source: "team",
      capability: "cap_report",
      cursor: "opaque-cursor",
      limit: 48,
    });

    const url = new URL(String(fetchMock.mock.calls[0][0]), "https://example.test");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      q: "finance",
      source: "team",
      capability: "cap_report",
      cursor: "opaque-cursor",
      limit: "48",
    });
  });

  it("passes the catalog cancellation signal through to fetch", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ items: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    await listAgentCatalog({ signal: controller.signal });
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("signal=");
  });

  it("sends row_version for personal status instead of optimistic state", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ kind: "agent_assignment_mutation" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await setAgentPersonalStatus("osa_1", "disabled", 7);

    const [, init] = fetchMock.mock.calls[0];
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ status: "disabled", row_version: 7 });
  });

  it("round-trips the signed impact token before unhire", async () => {
    const impact = {
      kind: "agent_unhire_impact" as const,
      schema_version: 1 as const,
      assignment_id: "asa_1",
      assignment_row_version: 4,
      impact_token: "signed-token",
      expires_in_seconds: 300,
      active_count: 1,
      paused_count: 0,
      running_count: 0,
      jobs: [],
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(impact), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const loaded = await getAgentUnhireImpact("osa_1");
    await unhireAgent("osa_1", loaded, "pause");

    const [, init] = fetchMock.mock.calls[1];
    expect(JSON.parse(String(init?.body))).toEqual({
      row_version: 4,
      impact_token: "signed-token",
      schedule_action: "pause",
    });
  });

  it("pins Fork to the previewed revision with an idempotency key", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ kind: "agent_fork_result" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await forkAgent("osa:source", {
      sourceRevisionRef: "arv_exact",
      displayName: "My Agent",
      idempotencyKey: "fork-request-123456",
    });

    const [input, init] = fetchMock.mock.calls[0];
    expect(String(input)).toContain("/api/v1/agents/osa%3Asource/fork");
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("fork-request-123456");
    expect(JSON.parse(String(init?.body))).toEqual({
      source_revision_ref: "arv_exact",
      display_name: "My Agent",
    });
  });
});


it("preserves a reviewed case contract in the actual acceptance request", async () => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("{}", {
    status: 200, headers: { "content-type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const caseSpec = {
    case_key: "quality.empty", fixture_ref: {}, expected_contract: { answer_contains: ["No results"] },
    scorer_config: { max_tool_calls: 0 }, effect_policy: { mode: "no_effects" }, valid_until: null,
  };
  await acceptAgentProposalSuggestedCases("osa_1", { proposal_id: "acp_1", row_version: 1 } as AgentChangeProposal, "aev_1", [{
    suggestedCaseId: "empty", caseKey: caseSpec.case_key, task: "Reviewed task", failureMode: "Fabricated records", caseSpec,
  }]);
  const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(body.cases[0].edited_case).toEqual({ ...caseSpec, task: "Reviewed task" });
  expect(body.expected_active_suite_revision_id).toBe("aev_1");
});

describe("Agent version comparison", () => {
  it("asks the server to refresh the plan only when the workbench says it is behind", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ execution_id: "aex_1" }), {
      status: 202,
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const workbench = {
      candidate_identity: { candidate_id: "arc_1", candidate_hash: "sha256:c", row_version: 2 },
      active_plan_identity: { id: "aep_1", hash: "sha256:p" },
      control_row_version: 4,
    } as unknown as AgentVersionWorkbench;

    await startAgentVersionComparison("osa_1", workbench, "key-current");
    await startAgentVersionComparison("osa_1", { ...workbench, evaluation_plan_behind: true }, "key-behind");

    const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init?.body)));
    expect(bodies[0]).not.toHaveProperty("refresh_plan");
    expect(bodies[1]).toMatchObject({ plan_id: "aep_1", refresh_plan: true, run_kind: "release" });
  });
});
