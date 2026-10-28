import { describe, expect, it } from "vitest";

import { staticTranslator } from "./locale-messages";
import { BLOCKED_REASON_CODES, blockedItemLines, dependencyIssueItems, parseBlockedItems } from "./agent-blocked-items";

const ITEM = { kind: "skill", name: "Sales report", identity: "team:sk_1", reason_codes: ["skill_pending_reapproval"] };

describe("parseBlockedItems", () => {
  it("reads the Agents shape at detail.error.details.blocked_items", () => {
    const body = { detail: { error: { code: "agent_dependency_blocked", details: { reason_codes: [], blocked_items: [ITEM] } } } };
    expect(parseBlockedItems(body)).toEqual([ITEM]);
  });

  it("reads the team Subagents shape at detail.blocked_items", () => {
    const body = { detail: { code: "agent_dependency_blocked", blocked_items: [ITEM] } };
    expect(parseBlockedItems(body)).toEqual([ITEM]);
  });

  it("reads already extracted details", () => {
    expect(parseBlockedItems({ blocked_items: [ITEM] })).toEqual([ITEM]);
  });

  it("drops malformed entries and falls back to the identity for a missing name", () => {
    const items = parseBlockedItems({ blocked_items: [
      null,
      { kind: "plugin", name: "x", reason_codes: [] },
      { kind: "tool", identity: "teammcp.crm.get", reason_codes: ["tool_unavailable", 3] },
    ] });
    expect(items).toEqual([{ kind: "tool", name: "teammcp.crm.get", identity: "teammcp.crm.get", reason_codes: ["tool_unavailable"] }]);
  });

  it("returns nothing when the server sent no items", () => {
    expect(parseBlockedItems({ detail: { code: "agent_dependency_blocked", reason_codes: ["skill_disabled"] } })).toEqual([]);
    expect(parseBlockedItems("not an object")).toEqual([]);
  });
});

describe("blockedItemLines", () => {
  it("writes one localized line per item with every known reason", () => {
    const t = staticTranslator("agents_marketplace", "en-US");
    const lines = blockedItemLines([
      { kind: "skill", name: "Sales report", identity: "team:sk_1", reason_codes: ["skill_pending_reapproval"] },
      { kind: "skill", name: "Forecast", identity: "team:sk_2", reason_codes: ["skill_risk_report_invalid"] },
      { kind: "tool", name: "CRM", identity: "teammcp.crm.get", reason_codes: ["tool_unavailable"] },
    ], t);
    expect(lines).toEqual([
      "Skill “Sales report”: Edited and waiting for a team admin to re-approve it. Ask a team admin to review the new version, or remove this Skill.",
      "Skill “Forecast”: Its safety check record is damaged. Re-upload the Skill.",
      "Tool “CRM”: It isn’t available right now. Remove it, or ask a team admin to turn it back on.",
    ]);
  });

  it("falls back to the generic text for the item kind when a code is unknown", () => {
    const t = staticTranslator("agents_marketplace", "zh-CN");
    expect(blockedItemLines([
      { kind: "skill", name: "Sales report", identity: "team:sk_1", reason_codes: ["something_new"] },
      { kind: "definition", name: "", identity: "", reason_codes: [] },
    ], t)).toEqual([
      "技能「Sales report」：此智能体当前无法使用它。请移除该技能，或到技能目录查看其状态。",
      "智能体配置：保存的配置需要处理。请刷新页面后重新保存。",
    ]);
  });

  it("says what is wrong with the definition itself and how to fix it, unlike the Tool-level advice", () => {
    const t = staticTranslator("agents_marketplace", "en-US");
    const [definition, tool] = blockedItemLines([
      { kind: "definition", name: "skill_grants", identity: "skill_grants", reason_codes: ["precise_skill_scope_required"] },
      { kind: "tool", name: "python_sandbox", identity: "python_sandbox", reason_codes: ["precise_skill_scope_required"] },
    ], t);
    expect(definition).toBe(
      "Agent configuration: It still names Skill Tools (read, run or list Skills) without a list of granted Skills, which only an older save produces. Open it in the editor and save it once; saving adds the list from the Skills you selected.",
    );
    expect(tool).toBe("Tool “python_sandbox”: It needs an exact grant on its Skill. Add the Skill it uses, or remove this Tool.");
  });

  it("has copy for every mapped reason code in all four locales", () => {
    const keys = [
      "blocked_reason_definition_precise_skill_scope_required",
      ...[...BLOCKED_REASON_CODES].map((code) => `blocked_reason_${code}`),
      "blocked_reason_generic_skill", "blocked_reason_generic_tool", "blocked_reason_generic_definition",
      "blocked_item_skill", "blocked_item_tool", "blocked_item_definition",
    ];
    for (const locale of ["en-US", "zh-CN", "de-DE", "fr-FR"] as const) {
      const t = staticTranslator("agents_marketplace", locale);
      for (const key of keys) expect(t.hasMessage(key), `${locale}:${key}`).toBe(true);
    }
  });
});

describe("dependencyIssueItems", () => {
  const t = staticTranslator("agents_marketplace", "en-US");

  it("names the Tool a visibility check rejected, with the picker's label", () => {
    const items = dependencyIssueItems({ reason: "plugin_dependencies_unavailable", rule: "tool_unavailable", tool_refs: ["python_sandbox"] });
    const lines = blockedItemLines(items, t, (item) => (item.identity === "python_sandbox" ? "Run code" : undefined));
    expect(lines).toEqual([`Tool “Run code”: ${t("blocked_reason_tool_unavailable")}`]);
  });

  it("names the Skill a visibility check rejected", () => {
    const [item] = dependencyIssueItems({ rule: "skill_audience_not_allowed", skill_name: "weekly-report" });
    expect(item).toEqual({ kind: "skill", name: "weekly-report", identity: "weekly-report", reason_codes: ["skill_reference_audience_not_covered"] });
  });

  it("prefers the compiler's blocked items and returns nothing for an unnamed failure", () => {
    const blocked = { blocked_items: [{ kind: "tool", name: "python_sandbox", identity: "python_sandbox", reason_codes: ["precise_skill_scope_required"] }] };
    expect(dependencyIssueItems(blocked)).toHaveLength(1);
    expect(dependencyIssueItems({ reason: "plugin_dependencies_changed" })).toEqual([]);
  });
});
