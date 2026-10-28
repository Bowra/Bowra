/**
 * kb-matrix i18n key parity test (B8 lesson: missing keys crash at runtime; added coverage in tech-design §9).
 *
 * The kbMatrix namespace in en-US.json and zh-CN.json must define exactly the same key set
 * (existing M1-M4 keys + new M5 team template-library keys).
 */

import { describe, expect, it } from "vitest";

import enUS from "../../messages/en-US.json";
import zhCN from "../../messages/zh-CN.json";

type Bundle = Record<string, Record<string, unknown>>;

describe("kbMatrix i18n parity", () => {
  it("en-US 与 zh-CN 的 kbMatrix key 集完全一致", () => {
    const enKeys = Object.keys((enUS as Bundle).kbMatrix).sort();
    const zhKeys = Object.keys((zhCN as Bundle).kbMatrix).sort();
    expect(enKeys).toEqual(zhKeys);
  });

  it("kbMatrix namespace 键数量 sanity（≥ 300，覆盖 M1-M5）", () => {
    expect(Object.keys((enUS as Bundle).kbMatrix).length).toBeGreaterThanOrEqual(300);
  });

  it("M5 团队模板库关键键在两 locale 均存在", () => {
    const required = [
      "tpl_tab_mine",
      "tpl_tab_team",
      "tpl_publish",
      "tpl_published_badge",
      "tpl_unpublish",
      "tpl_republish",
      "tpl_drift_flag",
      "tpl_publish_confirm_title",
      "tpl_publish_travels_2",
      "tpl_publish_notravels_1",
      "tpl_toast_published",
      "otpl_sub",
      "otpl_copy",
      "otpl_copied_badge",
      "otpl_own_badge",
      "otpl_toast_copied",
      "otpl_copy_conflict_title",
      "err_not_publishable",
      "err_empty_template",
      "err_copy_failed",
      "otpl_gone",
    ];
    for (const k of required) {
      expect((enUS as Bundle).kbMatrix[k], `en missing ${k}`).toBeTruthy();
      expect((zhCN as Bundle).kbMatrix[k], `zh missing ${k}`).toBeTruthy();
    }
  });
});
