/**
 * Memory source line: rendered from the structured `source_label_key` in the interface language, never the
 * Gateway's Chinese `source_label` (116 dogfood 1.5.25: en-US showed 「你在对话中告诉 Bowhead」).
 */
import { describe, expect, it } from "vitest";

import enMessages from "@/messages/en-US.json";
import deMessages from "@/messages/de-DE.json";
import frMessages from "@/messages/fr-FR.json";
import zhMessages from "@/messages/zh-CN.json";

import { memorySourceKeysMatching, memorySourceLabel } from "./memory-v2-content";

const KEYS = [
  "legacy_migration",
  "auto_captured",
  "confirmed_suggestion",
  "told_in_chat",
  "system_maintained",
  "unavailable",
];

function translator(messages: { memory_page: { v2: Record<string, unknown> } }) {
  return (key: string) => {
    const value = messages.memory_page.v2[key];
    if (typeof value !== "string") throw new Error(`missing memory_page.v2.${key}`);
    return value;
  };
}

describe("memorySourceLabel", () => {
  it("renders the English copy for a known key", () => {
    expect(
      memorySourceLabel(translator(enMessages), { source_label: "你在对话中告诉 Bowhead", source_label_key: "told_in_chat" }),
    ).toBe("You asked to remember this in a conversation");
  });

  it("has copy for every key in all four locales, with no Chinese outside zh-CN", () => {
    for (const messages of [enMessages, deMessages, frMessages, zhMessages]) {
      const t = translator(messages);
      for (const key of KEYS) expect(t(`source_${key}`)).toBeTruthy();
    }
    for (const messages of [enMessages, deMessages, frMessages]) {
      const t = translator(messages);
      for (const key of KEYS) expect(t(`source_${key}`)).not.toMatch(/[一-鿿]/);
    }
  });

  it("falls back to the Gateway label for a missing or unknown key", () => {
    const t = translator(enMessages);
    expect(memorySourceLabel(t, { source_label: "由系统维护" })).toBe("由系统维护");
    expect(memorySourceLabel(t, { source_label: "x", source_label_key: "future_kind" })).toBe("x");
  });
});

describe("memorySourceKeysMatching", () => {
  it("matches the source line the user reads in the interface language, case-insensitively", () => {
    const en = translator(enMessages);
    expect(memorySourceKeysMatching(en, "  CONVERSATION ")).toContain("told_in_chat");
    expect(memorySourceKeysMatching(en, "告诉")).toEqual([]);
    // zh-CN copy also differs from the Gateway's Chinese `source_label` (「你在对话中告诉 Bowhead」): search follows
    // the displayed 「你在对话中要求记住」, not the hidden server text.
    const zh = translator(zhMessages);
    expect(memorySourceKeysMatching(zh, "要求记住")).toEqual(["told_in_chat"]);
    expect(memorySourceKeysMatching(zh, "告诉")).toEqual([]);
  });

  it("returns no keys for blank or unrelated search text", () => {
    const en = translator(enMessages);
    expect(memorySourceKeysMatching(en, "   ")).toEqual([]);
    expect(memorySourceKeysMatching(en, "zzqx")).toEqual([]);
  });
});
