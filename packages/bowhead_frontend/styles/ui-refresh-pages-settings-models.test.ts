/**
 * Interface appearance, page-by-page acceptance A · source contract for team Models settings (/team/settings/models and its section pages).
 *
 *   1. Mappings (MAP): every selector starts with :root:root, without literal colors or --c-line;
 *      cascade not defeated by earlier rules; remove group-heading numbers, put row lists into group cards, remove
 * header dividers, replace draft dock with save bar.
 *   2. Base classes (LEGACY) carry no prefix; call sites use them and no longer write these inline styles.
 *
 * Both development CSS files are styles/_accept-a/ shards; change only the two paths below when merging into
 * ui-refresh-pages-settings.css / ui-controls-legacy.css.
 */
import { describe, expect, it } from "vitest";

import {
  cascadeProblems,
  decls,
  LITERAL_COLOR,
  norm,
  parseRules,
  PREFIX,
  read,
  readSection,
  splitSelectors,
  stripComments,
} from "./css-contract";

const MAP_FILE = "styles/ui-refresh-pages-settings.css";
const LEGACY_FILE = "styles/ui-controls-legacy.css";

const MAP = parseRules(stripComments(readSection(MAP_FILE, "accept-a:models")));
const LEGACY = parseRules(stripComments(readSection(LEGACY_FILE, "accept-a:models")));

/** Merge declarations for rules outside @media blocks whose selector list contains exactly `${PREFIX} ${sel}`. */
function on(sel: string): Record<string, string> {
  const found = MAP.filter((r) => r.media === null && splitSelectors(r.selector).includes(`${PREFIX} ${norm(sel)}`));
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe(`${MAP_FILE}: every rule starts with the :root:root prefix`, () => {
  it("解析出规则", () => {
    expect(MAP.length).toBeGreaterThan(30);
  });
  it("every selector starts with :root:root", () => {
    expect(MAP.flatMap((r) => splitSelectors(r.selector)).filter((s) => !s.startsWith(PREFIX))).toEqual([]);
  });
  it("不写字面色值、不用 --c-line、不定位不设层级", () => {
    expect(MAP.filter((r) => LITERAL_COLOR.test(stripComments(r.body))).map((r) => norm(r.selector))).toEqual([]);
    expect(MAP.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => norm(r.selector))).toEqual([]);
    expect(MAP.filter((r) => "position" in decls(r.body) || "z-index" in decls(r.body)).map((r) => norm(r.selector))).toEqual([]);
  });
  it("级联：没有被先写规则压住的覆盖", () => {
    expect(cascadeProblems(MAP)).toEqual([]);
  });
});

describe("模型页映射（Page Layout Spec：页头、分组卡片、垂直节奏）", () => {
  it("页头去掉分隔线；页头 → 第一组 24、组与组 32；草稿坞留白不再需要", () => {
    expect(on(".mset .ph")).toMatchObject({ "padding-bottom": "0", "border-bottom": "0" });
    expect(on(".mset")).toMatchObject({ gap: "24px" });
    expect(on(".mset :is(.model-overview, .model-editor)")).toMatchObject({ gap: "var(--group-gap)" });
    expect(on(".mset.v4")).toMatchObject({ "padding-bottom": "0" });
  });

  it("分组标题不编号（01/02/03、①②③ 隐藏），16/650 系统字；说明 12.5 ink3、标题下 2", () => {
    expect(on(".mset :is(.overview-section-index, .overview-kicker, .block-n)")).toEqual({ display: "none" });
    expect(on(".mset :is(.overview-section-head h2, .overview-readiness h2, .runtime-head h2, .block-title)")).toMatchObject({
      "font-family": "var(--font-control)",
      "font-size": "var(--fs-lg)",
      "font-weight": "650",
    });
    expect(on(".mset .block-desc")).toMatchObject({ margin: "-10px 0 0", color: "var(--c-ink-3)", "font-size": "0.78125rem" });
    expect(on(".mset .block")).toMatchObject({ gap: "12px" });
  });

  it("行列表与编辑页卡片：内容面、--r-4、0.5px 发丝线外框，没有描边与左侧色条", () => {
    const card = MAP.find((r) => /\.overview-provider-rows,[\s\S]*\.external-capability-model/.test(r.selector));
    expect(card, "card rule").toBeDefined();
    for (const cls of [".overview-ledger", ".overview-external-rows", ".pcard", ".connect", ".default-sel", ".kb-sel"]) {
      expect(card!.selector).toContain(cls);
    }
    expect(decls(card!.body)).toMatchObject({
      border: "0",
      "border-radius": "var(--r-4)",
      background: "var(--c-surface)",
      "box-shadow": "0 0 0 var(--hairline) var(--c-separator)",
    });
    expect(on(".mset .pcard::before")).toEqual({ display: "none" });
  });

  it("行：最小 44、内边距 10 / 16、行间发丝线（不用原来的底边线）", () => {
    expect(on(".mset :is(.overview-provider-row, .overview-external-rows > div, .overview-ledger-row)")).toEqual({
      "min-height": "var(--group-row-min-h)",
      padding: "10px 16px",
      "border-bottom": "0",
    });
    expect(on(".mset :is( .overview-provider-row + .overview-provider-row, .overview-external-rows > div + div, .overview-ledger-row, .overview-ledger-more )")).toEqual({
      "box-shadow": "inset 0 var(--hairline) 0 var(--c-separator)",
    });
    expect(on(".mset .overview-ledger-header")).toMatchObject({ "min-height": "32px", background: "var(--c-surface-2)", "font-size": "0.71875rem", "font-weight": "600" });
  });

  it("等宽大写小字改为系统字普通字（字段名、步骤、来源标记）", () => {
    expect(on(".mset :is(.pcard-field .fl, .connect-head .step)")).toMatchObject({
      "font-family": "var(--font-control)",
      "text-transform": "none",
      "letter-spacing": "normal",
    });
    expect(on(".mset :is(.cat-src, .kb-row .kbl .req, .kb-row .kbl .opt)")).toMatchObject({ "text-transform": "none" });
  });

  it("录入型连接表单：上下排列，标签 12/600 ink2、提示 11.5", () => {
    expect(on(".mset .fld label")).toEqual({ color: "var(--c-ink-2)", "font-size": "var(--fs-sm)", "font-weight": "600" });
    expect(on(".mset .fld .hint")).toMatchObject({ "font-size": "0.71875rem" });
  });
});

describe("模型页调用点", () => {
  const src = read("components/settings/models.tsx");
  it("a dirty draft shows the save bar (Review goes through the review flow, Discard drops the draft, the page keeps its leave guard)", () => {
    expect(src).toMatch(/\{LLM_SETTINGS_V4_ENABLED \? <SaveBar dirty=\{pageMode === "edit" && isDirty && !reviewOpen\} guard=\{false\}/);
    expect(src).toMatch(/saveLabel=\{t\("v4\.review"\)\} onDiscard=\{discardDraft\} onSave=\{\(\) => void reviewDraft\(\)\}/);
  });
  it("loading renders the skeleton", () => {
    expect(src).toContain('<LoadingSkeleton rows={3} label={t("loading")} />');
  });
  it("模型相关组件不再写内联样式", () => {
    expect(src).not.toMatch(/style=\{\{/);
    expect(read("components/settings/models/external-capability-editor.tsx")).not.toMatch(/style=\{\{/);
  });
});

describe(`${LEGACY_FILE}：原内联样式逐字搬来`, () => {
  it("base-layer classes carry no hook prefix", () => {
    expect(LEGACY.flatMap((r) => splitSelectors(r.selector)).filter((s) => s.startsWith(":root"))).toEqual([]);
  });

  it("调用点用上了这些 class", () => {
    const src = read("components/settings/models.tsx");
    for (const cls of ["legacy-mset-empty-cta", "legacy-mset-rot180", "legacy-mset-guard-tight", "legacy-mset-connect-fieldset"]) {
      expect(src).toContain(cls);
    }
    expect(src.match(/legacy-mset-proto-plain/g)).toHaveLength(2);
    expect(src.match(/legacy-mset-effect-indent/g)).toHaveLength(2);
    expect(src.match(/legacy-mset-unver-neutral/g)).toHaveLength(2);
  });
});
