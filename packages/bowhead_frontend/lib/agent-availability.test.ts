import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

import { availabilityReason, limitedAvailability, PUBLIC_REASONS } from "./agent-availability";
import type { AgentCatalogItem } from "./agents-api";

function item(
  publicReason: string | null,
  extra: Partial<AgentCatalogItem["availability"]> = {},
  runnable = false,
  relationship = "team",
): AgentCatalogItem {
  return {
    availability: { discoverable: true, hireable: false, runnable, reasons: [], public_reason: publicReason, ...extra },
    source: { kind: "team", owner_label: "liuyixin", owner_user_id: "u1" },
    relationship,
  } as unknown as AgentCatalogItem;
}

describe("availabilityReason", () => {
  it("says nothing when the Agent can run, or when the reason is not one of the public codes", () => {
    expect(availabilityReason(item("not_hired", {}, true), "成员")).toBeNull();
    expect(availabilityReason(item("listing_safety_blocked"), "成员")).toBeNull();
    expect(availabilityReason(item(null), "成员")).toBeNull();
  });

  it("never offers a request where an administrator has said no, and says 'for now' only when it can change", () => {
    for (const code of PUBLIC_REASONS) {
      const view = availabilityReason(item(code), "成员")!;
      if (code === "blocked_by_admin" || code === "dependency_blocked_for_you") expect(view.action).toBeNull();
      expect(view.chipKey).toBe(code === "temporarily_unavailable" ? "reason_chip_checking" : "reason_chip_unavailable");
    }
    expect(availabilityReason(item("not_hired"), "成员")?.action).toBe("hire");
    expect(availabilityReason(item("personally_disabled"), "成员")?.action).toBe("enable");
    expect(availabilityReason(item("temporarily_unavailable"), "成员")?.action).toBe("retry");
  });

  it("does not tell someone who manages the Agent to go find its owner", () => {
    expect(availabilityReason(item("agent_needs_owner_fix"), "成员")?.noteKey).toBe("reason_agent_needs_owner_fix_note");
    expect(availabilityReason(item("agent_needs_owner_fix", {}, false, "owned"), "成员")?.noteKey).toBe(
      "reason_agent_needs_owner_fix_note_manager",
    );
    expect(availabilityReason(item("agent_retired", {}, false, "owned"), "成员")?.noteKey).toBe("reason_agent_retired_note_manager");
    // An administrator's "no" has nothing a manager of the Agent can change.
    expect(availabilityReason(item("blocked_by_admin", {}, false, "owned"), "成员")?.noteKey).toBe("reason_blocked_by_admin_note");
  });

  it("marks a running Agent with a missing dependency as limited, never as unavailable", () => {
    const limited = { name: "图表插图", name_en: "Chart image", more: 1 };
    expect(limitedAvailability(item(null, { limited }, true), "zh-CN")).toMatchObject({ shortKey: "limited_short", values: { x: "图表插图", n: 1 } });
    expect(limitedAvailability(item(null, { limited }, true), "en-US")?.values.x).toBe("Chart image");
    // The German UI uses the English name; fall back to Chinese only if no English name exists.
    expect(limitedAvailability(item(null, { limited }, true), "de-DE")?.values.x).toBe("Chart image");
    expect(limitedAvailability(item(null, { limited: { ...limited, name_en: null } }, true), "de-DE")?.values.x).toBe("图表插图");
    expect(limitedAvailability(item(null, { limited: { name: null, name_en: null, more: 0 } }, true), "zh-CN")?.shortKey).toBe("limited_short_unnamed");
    expect(limitedAvailability(item(null, {}, true), "zh-CN")).toBeNull();
    expect(availabilityReason(item(null, { limited }, true), "成员")).toBeNull();
  });

  it.each([
    ["zh-CN", zh],
    ["en-US", en],
  ] as const)("has every key it can produce in %s, and only reason 11 says 'for now' there", (locale, messages) => {
    const table = messages.agents_marketplace as Record<string, string>;
    for (const code of PUBLIC_REASONS) {
      for (const relationship of ["team", "owned"]) {
        const view = availabilityReason(item(code, {}, false, relationship), "x")!;
        const keys = [view.chipKey, view.shortKey, view.fullKey, view.noteKey].filter(Boolean) as string[];
        for (const key of keys) expect(table[key], `${locale} ${key}`).toBeTruthy();
        if (locale === "zh-CN" && code !== "temporarily_unavailable") {
          expect(keys.map((key) => table[key]).join("")).not.toMatch(/暂时|稍后|暂不/);
        }
      }
    }
    // Shown when the server gives no reason at all; it must not promise that waiting helps either.
    if (locale === "zh-CN") expect(table.assignment_active_unavailable).not.toMatch(/暂/);
    for (const key of [
      "reason_panel_title", "reason_agent_retired_note_manager", "reason_agent_needs_owner_fix_note_manager",
      "limited_chip", "limited_short", "limited_short_unnamed", "limited_full", "limited_full_unnamed",
    ]) {
      expect(table[key], `${locale} ${key}`).toBeTruthy();
    }
  });
});
