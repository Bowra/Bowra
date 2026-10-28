/**
 * Copy contract for the Agent version terms (docs/design/agent-release-beginner-experience.md, Item 1).
 *
 * A user sees three stages, Draft → Candidate → Released, and only released versions carry a number.
 * The Agent pages once mixed 线上 / Live / 当前发布版本 / 工作副本 / 下一份草稿 / 待发布版本 for the same
 * three objects. This guard keeps those words out of `agents_marketplace`; message keys and API fields
 * keep their names.
 */

import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

type Catalog = Record<string, unknown>;

const ZH_BANNED = /线上|Live|当前发布版本|工作副本|下一份草稿|待发布版本|working copy/;
const EN_BANNED = /\bLive\b|\blive version\b|\blive release\b|working copy|next draft|pending version/i;

/**
 * Keys outside this guard: webhook event names and Automation-tab copy are removed with the Automation
 * tab (Item 9), and their event names are external compatibility values until then.
 */
const ALLOWED = (key: string) => key.startsWith("version_webhook_") || key.startsWith("version_automation_");

/** ICU arguments such as `{live}` are not user-visible words. */
const stripArguments = (text: string) => text.replace(/\{[a-z_]+\}/g, "");

function offenders(catalog: Catalog, banned: RegExp): string[] {
  return Object.entries(catalog)
    .filter(([key, value]) => typeof value === "string" && !ALLOWED(key) && banned.test(stripArguments(value)))
    .map(([key]) => key);
}

describe("agents_marketplace version terms", () => {
  it("zh-CN uses only 草稿 / 候选 / 已发布", () => {
    expect(offenders((zh as unknown as { agents_marketplace: Catalog }).agents_marketplace, ZH_BANNED)).toEqual([]);
  });

  it("en-US uses only Draft / Candidate / Released", () => {
    expect(offenders((en as unknown as { agents_marketplace: Catalog }).agents_marketplace, EN_BANNED)).toEqual([]);
  });

  it("the guard catches the old wording", () => {
    expect(offenders({ a: "当前发布版本 v{version}", b: "保存工作副本" }, ZH_BANNED)).toEqual(["a", "b"]);
    expect(offenders({ a: "Live version v{version}", b: "Compared with {live}" }, EN_BANNED)).toEqual(["a"]);
  });

  it("only released versions carry a number in the Release card", () => {
    const catalog = (zh as unknown as { agents_marketplace: Record<string, string> }).agents_marketplace;
    // The release action names the number it creates; the candidate itself is never numbered.
    expect(catalog.version_release_action).toBe("发布为 {candidate}");
    expect(catalog.version_change_title).not.toContain("{candidate}");
  });

  it("the Owner's test-and-release flow never says 候选 / candidate (Item 3)", () => {
    for (const [bundle, word] of [[zh, "候选"], [en, "candidate"]] as const) {
      const catalog = (bundle as unknown as { agents_marketplace: Record<string, string> }).agents_marketplace;
      const owner = Object.entries(catalog).filter(([key]) => key.startsWith("version_test_") || key.startsWith("version_rail_"));
      expect(owner.length).toBeGreaterThan(20);
      expect(owner.filter(([, value]) => stripArguments(value).toLowerCase().includes(word)).map(([key]) => key)).toEqual([]);
    }
  });
});
