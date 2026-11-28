// @vitest-environment node
import { describe, expect, it } from "vitest";

import type { AgentArchivedItem, AgentCatalogItem } from "./agents-api";
import {
  archivedCreatedCount,
  createdAgentRows,
  createdQueryActive,
  DEFAULT_CREATED_QUERY,
  rowException,
  splitHiredRows,
} from "./agent-management-list";

function item(agentId: string, name: string, overrides: Partial<AgentCatalogItem> = {}): AgentCatalogItem {
  return {
    kind: "agent_catalog_item",
    schema_version: 1,
    agent_id: agentId,
    display_name: name,
    summary: `${name} summary`,
    lifecycle_state: "active",
    source: { kind: "team", owner_label: "Avery", owner_user_id: "usr_1" },
    facets: { domains: [], jobs: [], outputs: [] },
    capabilities: [],
    risk: { level: "low", reasons: [] },
    safety: { status: "pass", checked_subject: "legacy_row", reasons: [] },
    assignment: { status: "active", source: "user_hired", row_version: 1 },
    team_default: { enabled: false, policy_version: null, row_version: null },
    availability: { discoverable: true, hireable: false, runnable: true, reasons: [], public_reason: null },
    actions: { view_detail: true, hire: false, enable: false, disable: true, unhire: true, manage: true, manage_team_default: false, create: true },
    action_reasons: {},
    relationship: "owned",
    updated_label: { kind: "saved_at", at: "2026-11-01T00:00:00Z" },
    manager_url: `/agents/manage/?agent_id=${agentId}`,
    ...overrides,
  };
}

const ARCHIVED_ROW: AgentArchivedItem = {
  kind: "agent_archived_management_item", schema_version: 1, agent_id: "osa_old", name: "old", display_name: "Old",
  summary: "", status: "disabled", lifecycle_state: "archived", aggregate_row_version: 1, live_release_id: "arl_1",
  release_sequence: 1, has_pending_candidate: false, archived_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z",
  actions: { restore: true, view_history: true },
};

const text = { name: (value: AgentCatalogItem) => value.display_name, summary: (value: AgentCatalogItem) => value.summary, locale: "zh-CN" };

describe("rowException", () => {
  it.each([
    ["a working row", item("osa_1", "A"), "created", null],
    ["a row the user paused", item("osa_1", "A", { assignment: { status: "personal_disabled", source: "user_hired", row_version: 1 }, availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "personally_disabled" } }), "created", "disabled"],
    ["a broken row", item("osa_1", "A", { availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "dependency_broken" } }), "created", "unavailable"],
    ["an own Agent not yet hired", item("osa_1", "A", { assignment: { status: "not_hired", source: null, row_version: null }, availability: { discoverable: true, hireable: true, runnable: false, reasons: [], public_reason: "not_hired" } }), "created", null],
    ["an archived hired row", item("osa_1", "A", { lifecycle_state: "archived", availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "agent_retired" } }), "hired", "archived_by_owner"],
  ] as const)("names %s", (_label, row, view, expected) => {
    expect(rowException(row, view)).toBe(expected);
  });
});

describe("createdAgentRows", () => {
  const paused = item("osa_b", "Beta 周报", {
    summary: "门店周报",
    assignment: { status: "personal_disabled", source: "user_hired", row_version: 1 },
    availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "personally_disabled" },
    updated_label: { kind: "saved_at", at: "2026-11-20T00:00:00Z" },
  });
  const broken = item("osa_c", "Gamma", {
    availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "dependency_broken" },
    updated_label: { kind: "saved_at", at: "2026-11-10T00:00:00Z" },
  });
  const plain = item("osa_a", "Alpha", { updated_label: { kind: "saved_at", at: null } });
  const archived = item("osa_d", "Delta", { lifecycle_state: "archived" });
  const rows = [plain, paused, broken, archived, ARCHIVED_ROW];

  it("leaves archived Agents out, newest first, and counts them for the pointer line", () => {
    expect(createdAgentRows(rows, DEFAULT_CREATED_QUERY, text).map((row) => row.agent_id)).toEqual(["osa_b", "osa_c", "osa_a"]);
    expect(archivedCreatedCount(rows)).toBe(1);
  });

  it("searches name and summary, filters by the row's exception and sorts by name", () => {
    expect(createdAgentRows(rows, { ...DEFAULT_CREATED_QUERY, query: "周报" }, text).map((row) => row.agent_id)).toEqual(["osa_b"]);
    expect(createdAgentRows(rows, { ...DEFAULT_CREATED_QUERY, query: "ALPHA" }, text).map((row) => row.agent_id)).toEqual(["osa_a"]);
    expect(createdAgentRows(rows, { ...DEFAULT_CREATED_QUERY, filter: "disabled" }, text).map((row) => row.agent_id)).toEqual(["osa_b"]);
    expect(createdAgentRows(rows, { ...DEFAULT_CREATED_QUERY, filter: "unavailable" }, text).map((row) => row.agent_id)).toEqual(["osa_c"]);
    expect(createdAgentRows(rows, { ...DEFAULT_CREATED_QUERY, sort: "name" }, text).map((row) => row.display_name)).toEqual(["Alpha", "Beta 周报", "Gamma"]);
  });

  it("treats only a changed query, filter or sort as active", () => {
    expect(createdQueryActive(DEFAULT_CREATED_QUERY)).toBe(false);
    expect(createdQueryActive({ ...DEFAULT_CREATED_QUERY, query: "  " })).toBe(false);
    expect(createdQueryActive({ ...DEFAULT_CREATED_QUERY, query: "a" })).toBe(true);
    expect(createdQueryActive({ ...DEFAULT_CREATED_QUERY, sort: "name" })).toBe(true);
  });
});

it("puts runnable hired rows first and the rest in the unavailable group", () => {
  const off = item("osa_off", "Off", { availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "personally_disabled" } });
  const on = item("osa_on", "On");
  expect(splitHiredRows([off, on])).toEqual({ active: [on], unavailable: [off] });
});
