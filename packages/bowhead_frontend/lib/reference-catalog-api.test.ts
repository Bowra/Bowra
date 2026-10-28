import { afterEach, describe, expect, it, vi } from "vitest";
import fixtures from "../../../contracts/chat-references/catalog-v1.json";
import { ApiError, apiJson } from "./api-client";
import {
  getReferenceCatalog,
  parseReferenceCatalog,
  type ReferenceCatalogSelection,
} from "./reference-catalog-api";
import type { ComposerPersistenceScope } from "./composer-persistence-scope";

vi.mock("./api-client", async (original) => ({
  ...(await original<typeof import("./api-client")>()),
  apiJson: vi.fn(),
}));
afterEach(() => vi.resetAllMocks());
const scope: ComposerPersistenceScope = {
  userId: "user_one",
  teamId: "team_one",
  workspaceId: "ws_catalog",
  requestedSessionId: null,
  authSessionGeneration: "private-generation",
};
const selection: ReferenceCatalogSelection = {
  trigger: "@",
  query: "",
  entryMode: "ordinary",
  selectedRefs: [],
};
const sample = () => structuredClone(fixtures.cases[0].response);

describe("system Agent discovery", () => {
  it("preserves a system identity without inventing an assignment", () => {
    const data = structuredClone(fixtures.cases.find((item) => item.name === "system_agent")!.response);
    const item = parseReferenceCatalog(data, selection).groups[1].items[0];
    expect(item.resource).toEqual({ kind: "agent", source_kind: "system", id: "system:compute", version: data.groups[1].items[0].resource!.version });
    expect(item.availability).toBe("unavailable");
  });

  it("rejects a system item placed in the team group", () => {
    const data: any = structuredClone(fixtures.cases.find((item) => item.name === "system_agent")!.response);
    data.groups[0].items = data.groups[1].items;
    data.groups[1].items = [];
    expect(() => parseReferenceCatalog(data, selection)).toThrow();
  });
});

describe("reference catalog wire", () => {
  it("preserves selectable exact Agent metadata only with its released matching cell", () => {
    const data = sample() as any;
    Object.assign(data.entry_support[0], { adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null });
    Object.assign(data.groups[0].items[0], { availability: "baseline", reason_code: null });
    expect(parseReferenceCatalog(data, selection).groups[0].items[0]).toEqual(data.groups[0].items[0]);
    for (const patch of [
      (v: any) => { v.entry_support[0].supported = false; v.entry_support[0].reason_code = "unsupported_entry"; },
      (v: any) => { v.groups[0].items[0].resource = null; },
      (v: any) => { v.groups[0].items[0].reason_code = "resource_unavailable"; },
      (v: any) => { v.groups[0].items[0].availability = "unavailable"; },
    ]) {
      const invalid = structuredClone(data);
      patch(invalid);
      expect(() => parseReferenceCatalog(invalid, selection)).toThrow(ApiError);
    }
    const skill = fixtures.cases.find((item) => item.name === "builtin_skill")!.response.groups[0].items[0].resource!;
    expect(() => parseReferenceCatalog(data, { ...selection, selectedRefs: [skill as any] })).toThrow(ApiError);
  });

  it.each([true, false])("reads the known initial adapter with supported=%s", (supported) => {
    const data = sample() as any;
    Object.assign(data.entry_support[0], {
      adapter_id: "initial-agent-skill", adapter_version: 1, supported,
      reason_code: supported ? null : "unsupported_entry",
    });
    const parsed = parseReferenceCatalog(data, selection);
    expect(parsed.entry_support[0]).toEqual(data.entry_support[0]);
    expect(parsed.groups[0].items[0].availability).toBe("unavailable");
  });

  it.each(["steer", "plan"] as const)("reads matching Agent/Skill %s adapters", (entryMode) => {
    const data = sample() as any;
    for (const cell of data.entry_support) {
      cell.entry_mode = entryMode;
      if (["agent", "skill"].includes(cell.resource_kind)) Object.assign(cell, {
        adapter_id: "initial-agent-skill",
        adapter_version: 1,
        supported: true,
        reason_code: null,
      });
      if (cell.resource_kind === "capability" && entryMode === "steer") Object.assign(cell, {
        adapter_id: "initial-capability",
        adapter_version: 1,
        supported: true,
        reason_code: null,
      });
    }
    Object.assign(data.groups[0].items[0], {
      availability: "baseline",
      reason_code: null,
    });
    const parsed = parseReferenceCatalog(data, {
      ...selection,
      entryMode,
    });
    expect(parsed.groups[0].items[0].availability).toBe("baseline");
    expect(parsed.entry_support.filter((cell) => cell.supported).map((cell) => [
      cell.resource_kind,
      cell.authorization_mode,
    ])).toEqual([
      ["agent", "baseline"],
      ["skill", "baseline"],
      ["skill", "readonly_extension"],
      ...(entryMode === "steer" ? [["capability", "baseline"]] : []),
    ]);
  });

  it.each([
    { adapter_version: 2 },
    { supported: true, reason_code: "unsupported_entry" },
    { supported: false, reason_code: null },
    { supported: 1 },
    { entry_mode: "plan" },
    { resource_kind: "mcp_tool" },
    { authorization_mode: "readonly_extension" },
  ])("rejects an invalid initial adapter contract %j", (patch) => {
    const data = sample() as any;
    Object.assign(data.entry_support[0], {
      adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null, ...patch,
    });
    expect(() => parseReferenceCatalog(data, selection)).toThrow(ApiError);
  });

  it.each(fixtures.cases)(
    "reads the shared backend $name contract without claiming execution",
    (fixture) => {
      const input = structuredClone(fixture.response);
      const value = parseReferenceCatalog(input, {
        ...selection,
        trigger: fixture.request.trigger as "@" | "/",
      });
      expect(value).toEqual(input);
      expect(value).not.toBe(input);
      expect(value.entry_support.every((cell) => !cell.supported)).toBe(true);
    },
  );

  it.each([
    (data: any) => {
      data.input_schema_versions = [99];
    },
    (data: any) => {
      data.entry_support[0].supported = true;
    },
    (data: any) => {
      data.entry_support[0].entry_mode = "steer";
    },
    (data: any) => {
      data.entry_support[0].adapter_id = "future";
    },
    (data: any) => {
      data.entry_support[0] = data.entry_support[1];
    },
    (data: any) => {
      data.groups[0].items[0].availability = "ready";
    },
    (data: any) => {
      data.groups[0].items[0].resource.id = "osa_other";
    },
    (data: any) => {
      delete data.groups[0].items[0].resource.assignment_id;
    },
    (data: any) => {
      data.groups[0].items[0].resource = null;
    },
    (data: any) => {
      data.groups[0].items.push(data.groups[0].items[0]);
    },
    (data: any) => {
      data.groups[0].items = Array.from({ length: 21 }, (_, i) => ({
        ...data.groups[0].items[0],
        id: `osa_${i}`,
        resource: null,
        reason_code: "unsupported_binding",
      }));
    },
    (data: any) => {
      data.next_cursor = "cursor-with-an-incomplete-page";
    },
    (data: any) => {
      data.raw_internal_account = "secret";
    },
  ])("rejects unsupported or contradictory server metadata", (mutate) => {
    // Deliberately malformed network bodies; no typed fixture can express them.
    const data = sample();
    mutate(data);
    expect(() => parseReferenceCatalog(data, selection)).toThrow(ApiError);
  });

  it("allows a discoverable item without inventing an exact resource", () => {
    const data = sample() as any;
    data.groups[0].items[0].resource = null;
    data.groups[0].items[0].reason_code = "unsupported_binding";
    expect(
      parseReferenceCatalog(data, selection).groups[0].items[0].resource,
    ).toBeNull();
  });

  it.each([false, true])("reads MCP Prompt metadata with ordinary support=%s", (enabled) => {
    const data: any = structuredClone(
      fixtures.cases.find((item) => item.name === "slash_unavailable")!
        .response,
    );
    const hash = `sha256:${"a".repeat(64)}`;
    data.groups[2].error = null;
    data.groups[2].items.push({
      kind: "mcp_prompt",
      source_kind: "team",
      id: "mcpprompt.ots_finance.0123456789abcdef01234567",
      source_id: "ots_finance",
      prompt_name: "profitability",
      label: "Profitability",
      summary: "Explain profitability",
      resource: {
        kind: "mcp_prompt",
        source_id: "ots_finance",
        local_contract_revision: hash,
        connection_binding_id: "ots_finance",
        connection_subject_fingerprint: hash,
        prompt_name: "profitability",
        prompt_contract_revision: hash,
      },
      parameter_schema: {
        type: "object",
        properties: { period: { type: "string" } },
        required: ["period"],
        additionalProperties: false,
      },
      remote_version: { status: "unknown", value: null, evidence: null },
      availability: "unavailable",
      reason_code: "unsupported_entry",
    });

    if (enabled) {
      for (const cell of data.entry_support) if (["agent", "mcp_prompt"].includes(cell.resource_kind)) Object.assign(cell, {
        adapter_id: cell.resource_kind === "agent" ? "initial-agent-skill" : "initial-mcp-prompt", adapter_version: 1, supported: true, reason_code: null,
      });
      Object.assign(data.groups[2].items[0], { availability: "baseline", reason_code: null });
    }
    const parsed = parseReferenceCatalog(data, {
      ...selection,
      trigger: "/",
    });
    expect(parsed.groups[2].items[0]).toMatchObject({
      kind: "mcp_prompt",
      prompt_name: "profitability",
      availability: enabled ? "baseline" : "unavailable",
      reason_code: enabled ? null : "unsupported_entry",
    });
    if (enabled) {
      const agent: any = sample().groups[0].items[0].resource;
      const prompt = data.groups[2].items[0].resource;
      const selected = { ...selection, trigger: "/" as const, selectedRefs: [agent] };
      expect(parseReferenceCatalog(data, selected).groups[2].items[0].availability).toBe("baseline");
      for (const refs of [
        [{ kind: "agent", source_kind: "system", id: "system:compute", version: hash }],
        [agent, { kind: "capability", id: "cap_visualize", version: 1 }],
        [agent, { kind: "skill", source_kind: "builtin", id: "builtin:method", version: hash }],
        [agent, prompt],
        [agent, ...[1, 2, 3].map((i) => ({ ...prompt, prompt_name: `p${i}` }))],
      ]) expect(() => parseReferenceCatalog(data, { ...selected, selectedRefs: refs as any })).toThrow(ApiError);
      const steer = structuredClone(data);
      steer.entry_support.forEach((cell: any) => { cell.entry_mode = "steer"; });
      expect(parseReferenceCatalog(steer, { ...selected, entryMode: "steer" }).groups[2].items[0].availability).toBe("baseline");
      for (const entryMode of ["plan", "resume"] as const) {
        const unsupported = structuredClone(data);
        unsupported.entry_support.forEach((cell: any) => { cell.entry_mode = entryMode; });
        expect(() => parseReferenceCatalog(unsupported, { ...selected, entryMode })).toThrow(ApiError);
      }
    }
  });

  it("rejects the complete invalid identity including a final newline", async () => {
    const data = sample() as any;
    data.groups[0].items[0].resource = null;
    data.groups[0].items[0].reason_code = "unsupported_binding";
    data.groups[0].items[0].id = "osa_bad\n";
    expect(() => parseReferenceCatalog(data, selection)).toThrow(ApiError);
    await expect(
      getReferenceCatalog(
        { ...scope, workspaceId: "ws_bad\n" },
        selection,
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ code: "reference_input_invalid" });
    expect(apiJson).not.toHaveBeenCalled();
  });

  it("sends only requested space/session and exact selection using the shared transport", async () => {
    vi.mocked(apiJson).mockResolvedValue(sample());
    const controller = new AbortController();
    await getReferenceCatalog(scope, selection, controller.signal);
    const [url, init] = vi.mocked(apiJson).mock.calls[0];
    expect(url).toBe("/api/v1/chat/references/catalog");
    expect(init).toMatchObject({
      method: "POST",
      cache: "no-store",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
    });
    expect(JSON.parse(init!.body as string)).toEqual(fixtures.cases[0].request);
    expect(init!.body).not.toContain("private-generation");
    expect(init!.body).not.toContain("team_one");
  });

  it("keeps the request context fixed while the caller changes its selection", async () => {
    let resolve!: (value: unknown) => void;
    vi.mocked(apiJson).mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const input = { ...selection };
    const pending = getReferenceCatalog(
      scope,
      input,
      new AbortController().signal,
    );
    input.entryMode = "steer";
    resolve(sample());
    await expect(pending).resolves.toMatchObject({
      input_schema_versions: [1],
    });
  });

  it("rejects invalid local query or scope before posting", async () => {
    for (const [current, query] of [
      [{ ...scope, authSessionGeneration: "" }, selection],
      [scope, { ...selection, query: "长".repeat(129) }],
    ] as const) {
      await expect(
        getReferenceCatalog(current, query, new AbortController().signal),
      ).rejects.toMatchObject({ code: "reference_input_invalid" });
    }
    expect(apiJson).not.toHaveBeenCalled();
  });
});

const builtinSample = (): any =>
  structuredClone(
    fixtures.cases.find((item) => item.name === "builtin_skill")!.response,
  );
const slash = { ...selection, trigger: "/" as const };

describe("uploaded Skill discovery", () => {
  it.each(["personal_skill", "team_skill"])("keeps the exact source for %s", (name) => {
    const data = structuredClone(fixtures.cases.find((item) => item.name === name)!.response);
    const item = parseReferenceCatalog(data, slash).groups[0].items[0];
    expect(item.kind).toBe("skill");
    expect(item.resource).toMatchObject({ source_kind: item.source_kind, id: item.id });
    expect(item.availability).toBe("unavailable");
  });

  it("keeps missing metadata disabled without inventing a resource or schema", () => {
    const data: any = structuredClone(fixtures.cases.find((item) => item.name === "personal_skill_metadata_pending")!.response);
    const item = parseReferenceCatalog(data, slash).groups[0].items[0];
    expect(item.resource).toBeNull();
    expect(item.reason_code).toBe("catalog_metadata_unavailable");
    data.groups[0].items[0].read_only_parameter_schema = { type: "object", properties: {}, additionalProperties: false };
    expect(() => parseReferenceCatalog(data, slash)).toThrow();
  });
});

describe("builtin Skill discovery", () => {
  it("reads a detached schema and never turns its metadata into execution support", () => {
    const data = builtinSample();
    const result = parseReferenceCatalog(data, slash);
    const item = result.groups[0].items[0];
    expect(item.kind).toBe("skill");
    if (item.kind !== "skill") throw new Error("expected Skill");
    expect(item.resource?.version).toMatch(/^sha256:/);
    expect(item.availability).toBe("unavailable");
    (
      data.groups[0].items[0] as any
    ).read_only_parameter_schema.properties.company.default = "changed";
    expect(item.read_only_parameter_schema?.properties?.company.default).toBe(
      "Example",
    );
  });

  it.each([
    (d: any) => {
      d.groups[0].items[0].resource = null;
    },
    (d: any) => {
      d.groups[0].items[0].resource.id = "builtin:other";
    },
    (d: any) => {
      d.groups[0].items[0].resource.source_kind = "user";
    },
    (d: any) => {
      d.groups[0].items[0].resource.version = "1.2.3";
    },
    (d: any) => {
      d.groups[0].items[0].source_kind = "team";
    },
    (d: any) => {
      d.groups[0].items[0].id = d.groups[0].items[0].resource.id =
        "personal_id";
    },
    (d: any) => {
      d.groups[0].items[0].read_only_parameter_schema = {
        $ref: "https://private.example",
      };
    },
    (d: any) => {
      d.groups[0].items[0].read_only_parameter_schema.properties.company.default = 42;
    },
    (d: any) => {
      delete d.groups[0].items[0].read_only_parameter_schema;
    },
    (d: any) => {
      d.groups[1].items = d.groups[0].items;
      d.groups[0].items = [];
    },
    (d: any) => {
      d.groups[0].error = "catalog_adapter_unavailable";
    },
  ])("rejects a forged source, schema, version or group", (mutate) => {
    const data = builtinSample();
    mutate(data);
    expect(() => parseReferenceCatalog(data, slash)).toThrow(ApiError);
  });

  it("reads paged slash metadata with exact identities and a disabled partial group", () => {
    const data = builtinSample();
    const original = data.groups[0].items[0];
    data.groups[0].items = Array.from({ length: 20 }, (_, i) => ({
      ...original,
      id: `builtin:method_${i}`,
      resource: { ...original.resource!, id: `builtin:method_${i}` },
    }));
    data.next_cursor = "opaque_cursor" as any;
    data.groups[0].error = "catalog_metadata_unavailable";
    const result = parseReferenceCatalog(data, slash);
    expect(result.groups[0].items).toHaveLength(20);
    expect(result.next_cursor).toBe("opaque_cursor");
    data.groups[0].items[19] = data.groups[0].items[0];
    expect(() => parseReferenceCatalog(data, slash)).toThrow(ApiError);
  });
});

describe("MCP metadata discovery", () => {
  function mcpSample() {
    const data: any = builtinSample();
    const binding = {
      source_id: "ots_123456789abc",
      local_contract_revision: `sha256:${"1".repeat(64)}`,
      connection_binding_id: "ots_123456789abc",
      connection_subject_fingerprint: `sha256:${"2".repeat(64)}`,
    };
    data.groups[2] = {
      id: "mcp",
      error: null,
      items: [
        {
          kind: "mcp_source",
          source_kind: "team",
          id: binding.source_id,
          source_id: binding.source_id,
          label: "Finance API",
          summary: "OpenAPI connection",
          resource: { kind: "mcp_source", ...binding },
          remote_version: { status: "unknown", value: null, evidence: null },
          availability: "unavailable",
          reason_code: "unsupported_entry",
        },
        {
          kind: "mcp_tool",
          source_kind: "team",
          id: "teammcp.ots_123456789abc.get_margin",
          source_id: binding.source_id,
          label: "get_margin",
          summary: "Read margin data",
          resource: {
            kind: "mcp_tool",
            ...binding,
            operation_name: "teammcp.ots_123456789abc.get_margin",
            operation_contract_revision: `sha256:${"3".repeat(64)}`,
          },
          parameter_schema: {
            type: "object",
            properties: { year: { type: "integer", minimum: 2000 } },
            required: ["year"],
            additionalProperties: false,
          },
          remote_version: { status: "unknown", value: null, evidence: null },
          availability: "unavailable",
          reason_code: "unsupported_entry",
        },
      ],
    };
    return data;
  }

  it("reads exact disabled source and Tool identities without inventing a remote version", () => {
    const data = mcpSample();
    const items = parseReferenceCatalog(data, slash).groups[2].items;
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({
      kind: "mcp_source",
      source_id: "ots_123456789abc",
      remote_version: { status: "unknown", value: null, evidence: null },
      availability: "unavailable",
    });
    expect(items[1]).toMatchObject({
      kind: "mcp_tool",
      source_id: "ots_123456789abc",
      resource: { operation_name: "teammcp.ots_123456789abc.get_margin" },
      availability: "unavailable",
    });
    data.groups[2].items[1].parameter_schema.properties.year.minimum = 2020;
    if (items[1].kind !== "mcp_tool" || items[1].parameter_schema === null)
      throw new Error("expected exact MCP Tool");
    expect(items[1].parameter_schema.properties?.year.minimum).toBe(2000);
  });

  it("accepts baseline MCP metadata only with the released read adapter", () => {
    const data = mcpSample();
    for (const cell of data.entry_support) {
      if (cell.resource_kind === "agent") {
        Object.assign(cell, {
          adapter_id: "initial-agent-skill",
          adapter_version: 1,
          supported: true,
          reason_code: null,
        });
      } else if (cell.resource_kind === "skill") {
        Object.assign(cell, {
          adapter_id: "initial-agent-skill",
          adapter_version: 1,
          supported: true,
          reason_code: null,
        });
      } else if (cell.resource_kind === "mcp_source" || cell.resource_kind === "mcp_tool") {
        Object.assign(cell, {
          adapter_id: "initial-mcp-read",
          adapter_version: 1,
          supported: true,
          reason_code: null,
        });
      }
    }
    for (const item of data.groups[2].items) {
      item.availability = "baseline";
      item.reason_code = null;
    }
    expect(parseReferenceCatalog(data, slash).groups[2].items).toMatchObject([
      { kind: "mcp_source", availability: "baseline" },
      { kind: "mcp_tool", availability: "baseline" },
    ]);
    const tool = data.groups[2].items[1].resource;
    const threeTools = [1, 2, 3].map((index) => ({
      ...structuredClone(tool),
      operation_name: `${tool.operation_name}_${index}`,
      operation_contract_revision: `sha256:${String(index).repeat(64)}`,
    }));
    expect(() => parseReferenceCatalog(data, { ...slash, selectedRefs: threeTools })).toThrow(ApiError);
    const reorderedTool = Object.fromEntries(Object.entries(tool).reverse()) as any;
    expect(() => parseReferenceCatalog(data, { ...slash, selectedRefs: [reorderedTool] })).toThrow(ApiError);
    const skill = data.groups[0].items[0].resource;
    expect(() => parseReferenceCatalog(data, { ...slash, selectedRefs: [skill] })).toThrow(ApiError);
    Object.assign(data.groups[0].items[0], { availability: "baseline", reason_code: null });
    expect(() => parseReferenceCatalog(data, { ...slash, selectedRefs: [tool] })).toThrow(ApiError);
    data.groups[1] = {
      id: "capabilities",
      error: null,
      items: [{
        kind: "capability",
        source_kind: "platform",
        id: "cap_visualize",
        label: "Create visuals",
        summary: "Create a visual artifact",
        resource: { kind: "capability", id: "cap_visualize", version: 1 },
        parameter_schema: { type: "object", properties: {}, additionalProperties: false },
        availability: "baseline",
        reason_code: null,
      }],
    };
    Object.assign(data.entry_support.find((cell: any) => cell.resource_kind === "capability"), {
      adapter_id: "initial-capability",
      adapter_version: 1,
      supported: true,
      reason_code: null,
    });
    expect(() => parseReferenceCatalog(data, { ...slash, selectedRefs: [tool] })).toThrow(ApiError);
    for (const cell of data.entry_support) {
      if (cell.resource_kind === "skill") Object.assign(cell, { supported: false, reason_code: "unsupported_entry" });
    }
    expect(() => parseReferenceCatalog(data, { ...slash, selectedRefs: [skill] })).toThrow(ApiError);
    data.entry_support.find((cell: any) => cell.resource_kind === "mcp_tool").adapter_id = "initial-capability";
    expect(() => parseReferenceCatalog(data, slash)).toThrow(ApiError);
  });

  it("accepts the exact MCP read adapter for steer", () => {
    const data = mcpSample();
    for (const cell of data.entry_support) {
      cell.entry_mode = "steer";
      if (["agent", "skill"].includes(cell.resource_kind)) Object.assign(cell, {
        adapter_id: "initial-agent-skill",
        adapter_version: 1,
        supported: true,
        reason_code: null,
      });
      if (["mcp_source", "mcp_tool"].includes(cell.resource_kind)) Object.assign(cell, {
        adapter_id: "initial-mcp-read",
        adapter_version: 1,
        supported: true,
        reason_code: null,
      });
    }
    for (const item of data.groups[2].items) {
      item.availability = "baseline";
      item.reason_code = null;
    }
    expect(parseReferenceCatalog(data, {
      ...slash,
      entryMode: "steer",
    }).groups[2].items).toMatchObject([
      { kind: "mcp_source", availability: "baseline" },
      { kind: "mcp_tool", availability: "baseline" },
    ]);
  });

  it.each([
    (data: any) => { data.groups[2].items[0].source_id = "ots_other"; },
    (data: any) => { data.groups[2].items[1].resource.source_id = "ots_other"; },
    (data: any) => { data.groups[2].items[1].resource.operation_name = "teammcp.ots_123456789abc.other"; },
    (data: any) => { data.groups[2].items[1].resource.connection_subject_fingerprint = `sha256:${"f".repeat(63)}`; },
    (data: any) => { data.groups[2].items[1].remote_version.status = "known"; },
    (data: any) => { data.groups[2].items[1].parameter_schema.additionalProperties = true; },
    (data: any) => { data.groups[2].items[0].availability = "baseline"; data.groups[2].items[0].reason_code = null; },
    (data: any) => { data.groups[2].error = "catalog_adapter_unavailable"; },
  ])("rejects contradictory MCP metadata %#", (mutate) => {
    const data = mcpSample();
    mutate(data);
    expect(() => parseReferenceCatalog(data, slash)).toThrow(ApiError);
  });
});

describe("saved Skill parameter metadata", () => {
  const skillResponse = () => structuredClone(fixtures.cases.find((item) => item.name === "builtin_skill")!.response);
  it("captures exact source before the request and accepts only that schema", async () => {
    const { getReferenceParameterSchema } = await import("./reference-catalog-api");
    const response: any = skillResponse();
    const resource: any = Object.fromEntries(Object.entries(response.groups[0].items[0].resource).reverse());
    vi.mocked(apiJson).mockResolvedValue(response);
    const result = getReferenceParameterSchema(scope, resource, "ordinary", new AbortController().signal);
    resource.version = "sha256:" + "c".repeat(64);
    expect(await result).toEqual(response.groups[0].items[0].read_only_parameter_schema);
    const request = JSON.parse(vi.mocked(apiJson).mock.calls[0][1]!.body as string);
    expect(request.exact_resource).toEqual(response.groups[0].items[0].resource);
    expect(request.query).toBe("");
  });
  it.each(["version", "source", "disabled", "missing", "schema"])("preserves parameters when metadata is %s", async (changed) => {
    const { getReferenceParameterSchema } = await import("./reference-catalog-api");
    const response: any = skillResponse();
    const resource = structuredClone(response.groups[0].items[0].resource);
    const item = response.groups[0].items[0];
    if (changed === "version") item.resource.version = "sha256:" + "c".repeat(64);
    if (changed === "source") { item.source_kind = "team"; item.resource.source_kind = "team"; }
    if (changed === "disabled") item.reason_code = "resource_unavailable";
    if (changed === "missing") response.groups[0].items = [];
    if (changed === "schema") item.read_only_parameter_schema = null;
    vi.mocked(apiJson).mockResolvedValue(response);
    await expect(getReferenceParameterSchema(scope, resource, "ordinary", new AbortController().signal)).rejects.toThrow(ApiError);
  });
});

describe("selectable Skill protocol", () => {
  function ready() {
    const data: any = structuredClone(fixtures.cases.find((item) => item.name === "builtin_skill")!.response);
    for (const cell of data.entry_support) if (["agent", "skill"].includes(cell.resource_kind)) Object.assign(cell, { adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null });
    Object.assign(data.groups[0].items[0], { availability: "readonly_extension", reason_code: null });
    return data;
  }
  const selected = { ...selection, trigger: "/" as const, selectedRefs: [sample().groups[0].items[0].resource as any] };
  it("reads a finite extension choice only under its declared receiver and adapter", () => {
    const data = ready();
    expect(parseReferenceCatalog(data, selected).groups[0].items[0].availability).toBe("readonly_extension");
  });
  it("accepts a server-projected Skill-only automatic receiver choice", () => {
    const data = ready();
    expect(parseReferenceCatalog(data, slash).groups[0].items[0].availability).toBe("readonly_extension");
  });
  it.each(["duplicate", "disabled_cell", "schema", "unsupported_capability"])("rejects an incompatible %s choice", (changed) => {
    const data = ready();
    const request = structuredClone(selected);
    if (changed === "duplicate") request.selectedRefs.push(data.groups[0].items[0].resource);
    if (changed === "disabled_cell") Object.assign(data.entry_support.find((cell: any) => cell.resource_kind === "skill" && cell.authorization_mode === "readonly_extension"), { supported: false, reason_code: "unsupported_entry" });
    if (changed === "schema") data.groups[0].items[0].read_only_parameter_schema = null;
    if (changed === "unsupported_capability") request.selectedRefs.push({ kind: "capability", id: "cap_visualize", version: 1 });
    expect(() => parseReferenceCatalog(data, request)).toThrow(ApiError);
  });
});

describe("selectable Capability protocol", () => {
  function ready() {
    const data: any = builtinSample();
    data.groups[1] = {
      id: "capabilities",
      error: null,
      items: [{
        kind: "capability",
        source_kind: "platform",
        id: "cap_visualize",
        label: "Create visuals",
        summary: "Create a visual artifact",
        resource: { kind: "capability", id: "cap_visualize", version: 1 },
        parameter_schema: { type: "object", properties: {}, additionalProperties: false },
        availability: "baseline",
        reason_code: null,
      }],
    };
    const agentCell = data.entry_support.find((item: any) => item.resource_kind === "agent");
    Object.assign(agentCell, { adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null });
    const capabilityCell = data.entry_support.find((item: any) => item.resource_kind === "capability");
    Object.assign(capabilityCell, { adapter_id: "initial-capability", adapter_version: 1, supported: true, reason_code: null });
    return data;
  }

  it("reads an exact platform Capability and detached parameter schema", () => {
    const data = ready();
    const parsed = parseReferenceCatalog(data, slash);
    const item = parsed.groups[1].items[0];
    expect(item).toMatchObject({
      kind: "capability",
      source_kind: "platform",
      resource: { kind: "capability", id: "cap_visualize", version: 1 },
      availability: "baseline",
    });
    data.groups[1].items[0].parameter_schema.properties.changed = { type: "string" };
    if (item.kind !== "capability") throw new Error("expected Capability");
    expect(item.parameter_schema.properties).toEqual({});
  });

  it("reads the same exact Capability under the released steer adapter", () => {
    const data = ready();
    for (const cell of data.entry_support) cell.entry_mode = "steer";
    const parsed = parseReferenceCatalog(data, {
      ...slash,
      entryMode: "steer",
    });
    expect(parsed.groups[1].items[0]).toMatchObject({
      kind: "capability",
      resource: { kind: "capability", id: "cap_visualize", version: 1 },
      availability: "baseline",
    });
  });

  it.each(["adapter", "schema", "source", "duplicate", "unsupported_skill"])("rejects an invalid %s binding", (change) => {
    const data = ready();
    const request: ReferenceCatalogSelection = { ...slash, selectedRefs: [] };
    if (change === "adapter") Object.assign(data.entry_support.find((item: any) => item.resource_kind === "capability"), { adapter_id: "unavailable", adapter_version: 0, supported: false, reason_code: "unsupported_entry" });
    if (change === "schema") data.groups[1].items[0].parameter_schema = { type: "object", additionalProperties: true };
    if (change === "source") data.groups[1].items[0].source_kind = "team";
    if (change === "duplicate") request.selectedRefs = [data.groups[1].items[0].resource];
    if (change === "unsupported_skill") request.selectedRefs = [data.groups[0].items[0].resource];
    expect(() => parseReferenceCatalog(data, request)).toThrow(ApiError);
  });

  it("accepts Skill and Capability choices with or without an explicit Agent", () => {
    const capabilityData = ready();
    for (const cell of capabilityData.entry_support) {
      if (["agent", "skill"].includes(cell.resource_kind)) {
        Object.assign(cell, { adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null });
      }
    }
    const skill = capabilityData.groups[0].items[0].resource;
    const agent = sample().groups[0].items[0].resource;
    expect(parseReferenceCatalog(capabilityData, { ...slash, selectedRefs: [skill] }).groups[1].items[0].availability).toBe("baseline");
    expect(parseReferenceCatalog(capabilityData, { ...slash, selectedRefs: [agent, skill] }).groups[1].items[0].availability).toBe("baseline");

    const skillData = structuredClone(capabilityData);
    skillData.groups[0].items[0].availability = "readonly_extension";
    skillData.groups[0].items[0].reason_code = null;
    skillData.groups[1].items[0].availability = "unavailable";
    skillData.groups[1].items[0].reason_code = "unsupported_entry";
    const capability = capabilityData.groups[1].items[0].resource;
    expect(parseReferenceCatalog(skillData, { ...slash, selectedRefs: [capability] }).groups[0].items[0].availability).toBe("readonly_extension");
    expect(parseReferenceCatalog(skillData, { ...slash, selectedRefs: [agent, capability] }).groups[0].items[0].availability).toBe("readonly_extension");
  });
});
