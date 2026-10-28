/**
 * Page acceptance B administration style source contracts: access/approvals/replay/audit/credits.
 *
 * jsdom does no cascading; enforce source facts; real-browser screenshots validate pixels (on/off).
 *   1. Callers use the classes from styles/ui-controls-legacy.css “逐页验收 B” and never restore the original inline styles.
 *   2. Interface rules (styles/ui-refresh-pages-admin.css) start with :root:root, no literal colors, imported last;
 *      mobile list rows follow Narrow Spec (min height 56, left padding 12, trailing ⋯ 44×44).
 */
import { describe, expect, it } from "vitest";

import {
  cascadeProblems,
  decls,
  isTokenRoot,
  LITERAL_COLOR,
  norm,
  parseRules,
  PREFIX,
  read,
  splitSelectors,
  stripComments,
} from "./css-contract";

const RULES = parseRules(stripComments(read("styles/ui-refresh-pages-admin.css")));

function rule(sel: string, media?: RegExp): Record<string, string> {
  const found = RULES.filter(
    (r) => (media ? media.test(r.media ?? "") : r.media === null) && splitSelectors(r.selector).includes(`${PREFIX} ${norm(sel)}`),
  );
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("acceptance B: call sites use classes instead of inline styles", () => {
  it("call sites use the base-layer classes and the inline styles stay removed", () => {
    const expectations: [string, string[], RegExp[]][] = [
      ["app/(app)/admin/access/page.tsx", ["ui-input legacy-access-input", "legacy-access-btn"], [/const (compactInput|actionButton)|style=\{(compactInput|actionButton)\}/]],
      ["app/(app)/admin/approvals/page.tsx", ["legacy-approvals-toolbar", "legacy-approval-card--pending", "legacy-risk-badge"], [/style=\{\{/, /palette/]],
      ["app/(app)/admin/turns/page.tsx", ["legacy-turns-layout", "legacy-turns-table", "legacy-turns-content"], [/style=\{\{/]],
      ["app/(app)/admin/credits/page.tsx", ["legacy-credits-head-btn legacy-credits-head-btn--dim-disabled"], [/style=\{\{/]],
      ["app/(app)/admin/credits/users/page.tsx", ["legacy-credits-user-head", "legacy-credits-back"], [/style=\{\{/, /const btn:/]],
      ["components/admin/credits/csv-download-button.tsx", ["legacy-credits-csv-menu", "legacy-credits-csv-item"], [/boxShadow/]],
      ["components/admin/credits/tier-defaults.tsx", ["legacy-credits-card", "legacy-credits-tier-row"], [/cardStyle/]],
      ["components/admin/credits/summary-card.tsx", ["legacy-credits-stat-value", "legacy-credits-severity"], [/cardStyle|poolColor/]],
      ["components/admin/credits/ledger-table.tsx", ["legacy-credits-ledger-type ${typeTone(type)}", "legacy-credits-th legacy-credits-th--${align}", "ui-pager-btn legacy-credits-pagebtn"], [/style=\{/, /const (cardStyle|tdStyle)|function typeColor/]],
      ["components/admin/credits/users-table.tsx", ["legacy-credits-toolbar", "legacy-credits-balance-track", "legacy-credits-attn-go"], [/cardStyle|sectionHeader|errorBannerStyle|attentionSeverityColor/]],
    ];
    for (const [file, present, absent] of expectations) {
      const src = read(file);
      for (const p of present) expect(src, `${file}: ${p}`).toContain(p);
      for (const a of absent) expect(a.test(src), `${file}: ${a}`).toBe(false);
    }
  });
});

describe("styles/ui-refresh-pages-admin.css: every rule starts with the :root:root prefix", () => {
  it("every selector starts with :root:root (inside @media too)", () => {
    expect(RULES.length).toBeGreaterThan(20);
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("不写字面色值；分隔线用发丝线 --c-separator", () => {
    const literal = RULES.filter((r) => !splitSelectors(r.selector).every(isTokenRoot)).flatMap((r) =>
      stripComments(r.body).split(";").map((x) => x.trim()).filter((x) => LITERAL_COLOR.test(x)),
    );
    expect(literal).toEqual([]);
    expect(RULES.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("没有被先写规则压住的覆盖", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });

  it("keeps the ordered authenticated styles starting with ui-refresh-pages-settings.css", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    const expectedImports = ["../../styles/ui-refresh-pages-settings.css", "../../styles/ui-refresh-pages-admin.css", "../../styles/ui-refresh-pages-main.css", "../../styles/ui-refresh-canvas.css", "../../styles/ui-refresh-workbench.css", "../../styles/ui-refresh-materials.css"];
    const start = imports.indexOf(expectedImports[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(imports.slice(start, start + expectedImports.length)).toEqual(expectedImports);
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-pages-admin.css");
  });

  it("in-page buttons .ui-btn / .ui-btn--primary join the batch 1 button family", () => {
    const refresh = stripComments(read("styles/ui-refresh.css"));
    expect(refresh.match(/\.ui-btn,/g)?.length ?? 0).toBeGreaterThanOrEqual(6);
    expect(refresh).toMatch(/\.credits-btn--primary,\s*\.ui-btn--primary,/);
    const offMode = [read("app/globals.css"), read("styles/ui-controls-legacy.css")].map(stripComments).join("\n");
    expect(offMode).not.toMatch(/\.ui-btn\b/);
  });
});

describe("手机列表行（取舍 12；窄屏规格「管理类表格」）", () => {
  it("行最小 56 高、左边距 12；行间发丝线", () => {
    expect(rule(".ui-list-row-main")).toMatchObject({ "min-height": "56px", padding: "8px 4px 8px 12px" });
    expect(rule(".ui-list-row")).toMatchObject({ "border-bottom": "var(--hairline) solid var(--c-separator)" });
  });

  it("行尾 ⋯ 44×44 常显", () => {
    expect(rule(".ui-list-row-actions :is(.ui-row-more, .legacy-credits-more)")).toMatchObject({ width: "44px", height: "44px", opacity: "1" });
  });

  it("点整行打开：标题按钮的点击区铺满整行，状态与 ⋯ 叠在它之上", () => {
    expect(rule(".ui-list-row-open::after")).toMatchObject({ position: "absolute", inset: "0" });
    expect(rule(".ui-list-row-status, .ui-list-row-actions".split(", ")[0])).toMatchObject({ position: "relative", "z-index": "1" });
  });

  it("第三行最多两行（可换行），标题按钮铺满文字列以便省略", () => {
    expect(rule(".ui-list-row-text > .ui-list-row-meta")).toMatchObject({ "-webkit-line-clamp": "2", "white-space": "normal" });
    expect(rule(".ui-list-row-open")).toMatchObject({ display: "block", width: "100%" });
  });

  it("额度用户列表行：第二行的套餐与标记不收缩、显示名省略；手机上卡片内边距收窄、列表铺到卡片边", () => {
    expect(rule(".credits-user-listrow-sub > :is(.legacy-credits-tag, .legacy-credits-tier)")).toEqual({ flex: "none" });
    expect(rule(".credits-user-listrow-name")).toMatchObject({ "min-width": "0", "text-overflow": "ellipsis", "white-space": "nowrap" });
    expect(rule(".legacy-credits-card", /max-width: 639\.98px/)).toEqual({ padding: "16px 12px" });
  });

  it("回放页窄屏上下排列", () => {
    expect(rule(".legacy-turns-layout", /max-width: 639px/)).toEqual({ "grid-template-columns": "minmax(0, 1fr)" });
  });
});
