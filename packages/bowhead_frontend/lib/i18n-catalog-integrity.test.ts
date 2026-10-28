import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";
import de from "@/messages/de-DE.json";
import fr from "@/messages/fr-FR.json";
import zh from "@/messages/zh-CN.json";

type CatalogValue =
  | string
  | number
  | boolean
  | null
  | CatalogValue[]
  | { [key: string]: CatalogValue };

function flatten(
  value: CatalogValue,
  prefix = "",
  result: Record<string, string> = {},
): Record<string, string> {
  if (value === null || typeof value !== "object") {
    result[prefix] = String(value);
    return result;
  }
  for (const [key, child] of Object.entries(
    value as CatalogValue[] | Record<string, CatalogValue>,
  )) {
    flatten(child, prefix ? `${prefix}.${key}` : key, result);
  }
  return result;
}

const zhFlat = flatten(zh as unknown as CatalogValue);
const enFlat = flatten(en as unknown as CatalogValue);
const deFlat = flatten(de as unknown as CatalogValue);
const frFlat = flatten(fr as unknown as CatalogValue);

const CRITICAL_LOCALIZED_KEYS = [
  "auth.access_required.title",
  "agents_marketplace.version_agent_archived",
  "invite.landing.space_title",
  "invite.profile.title",
  "invite.verify.title",
  "invite.ready.space_title",
  // Formerly settings.mcp.title. After b0e153662 removed the old personal MCP subsystem, this surface moved to the personalMcp namespace;
  // move the sentinel with it instead of deleting this coverage.
  "personalMcp.settingsTitle",
  "settings.members.active_members_title",
  "memory_page.title",
  "canvas.tab_report",
  "admin.credits.page_title",
] as const;

describe("i18n catalog integrity", () => {
  it("keeps the Chinese and English key sets identical", () => {
    expect(Object.keys(enFlat).sort()).toEqual(Object.keys(zhFlat).sort());
  });

  it("keeps the German catalog on the same key set", () => {
    expect(Object.keys(deFlat).sort()).toEqual(Object.keys(enFlat).sort());
  });

  it("keeps the French catalog on the same key set", () => {
    expect(Object.keys(frFlat).sort()).toEqual(Object.keys(enFlat).sort());
  });

  it("keeps all four catalogs on one key set", () => {
    const keySets = [zhFlat, enFlat, deFlat, frFlat].map((flat) => Object.keys(flat).sort());
    for (const keys of keySets) expect(keys).toEqual(keySets[0]);
  });

  it("does not contain Chinese characters in the French catalog", () => {
    const leaked = Object.entries(frFlat).filter(([, value]) =>
      /\p{Script=Han}/u.test(value),
    );
    expect(leaked).toEqual([]);
  });

  it("does not contain Chinese characters in the German catalog", () => {
    const leaked = Object.entries(deFlat).filter(([, value]) =>
      /\p{Script=Han}/u.test(value),
    );
    expect(leaked).toEqual([]);
  });

  it("does not contain Chinese characters in the English catalog", () => {
    const leaked = Object.entries(enFlat).filter(([, value]) =>
      /\p{Script=Han}/u.test(value),
    );
    expect(leaked).toEqual([]);
  });

  it.each(CRITICAL_LOCALIZED_KEYS)("%s has distinct Chinese and English copy", (key) => {
    expect(zhFlat[key]).toBeTruthy();
    expect(enFlat[key]).toBeTruthy();
    expect(zhFlat[key]).not.toBe(enFlat[key]);
    expect(deFlat[key]).toBeTruthy();
    expect(deFlat[key]).not.toBe(zhFlat[key]);
    expect(deFlat[key]).not.toBe(enFlat[key]);
    // Critical French UI copy must be translated, including newly added surfaces.
    expect(frFlat[key]).toBeTruthy();
    expect(frFlat[key]).not.toBe(zhFlat[key]);
    expect(frFlat[key]).not.toBe(enFlat[key]);
  });
});
