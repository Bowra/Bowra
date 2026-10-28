/**
 * V3 PRD-B · i18n key parity test (B8 lesson — missing keys break runtime).
 *
 * Both zh-CN.json and en-US.json must define the same set of memory_page keys.
 */

import { describe, expect, it } from "vitest";

import zhCN from "../messages/zh-CN.json";
import enUS from "../messages/en-US.json";

type Bundle = Record<string, Record<string, unknown>>;

describe("memory_page i18n parity", () => {
  it("zh-CN and en-US have identical memory_page key sets", () => {
    const zhKeys = Object.keys((zhCN as Bundle).memory_page).sort();
    const enKeys = Object.keys((enUS as Bundle).memory_page).sort();
    expect(enKeys).toEqual(zhKeys);
  });

  it("V3.5 PRD-C keys present in both locales", () => {
    const required = [
      "propose_card_title", "propose_card_accept", "propose_card_reject",
      "target_dropdown_structured", "target_dropdown_notes",
    ];
    const zhMem = (zhCN as Bundle).memory_page;
    const enMem = (enUS as Bundle).memory_page;
    for (const k of required) {
      expect(zhMem[k], `zh-CN missing V3.5 key: ${k}`).toBeTypeOf("string");
      expect(enMem[k], `en-US missing V3.5 key: ${k}`).toBeTypeOf("string");
    }
  });

  it("required keys for toolbar / table / modals are present", () => {
    const required = [
      "title", "lead", "loading", "load_error", "stats_active", "stats_trash",
      "stats_pending", "empty_title", "empty_hint", "reset_confirm_title",
      "reset_confirm_action", "delete_confirm", "delete_done", "delete_failed",
      "restore_done", "restore_failed", "trash_empty", "candidates_title",
      "candidates_lead", "candidates_empty", "candidates_approve",
      "candidates_reject",
    ];
    const zhMem = (zhCN as Bundle).memory_page;
    const enMem = (enUS as Bundle).memory_page;
    for (const k of required) {
      expect(zhMem[k], `zh-CN missing key: ${k}`).toBeTypeOf("string");
      expect(enMem[k], `en-US missing key: ${k}`).toBeTypeOf("string");
    }
  });
});
