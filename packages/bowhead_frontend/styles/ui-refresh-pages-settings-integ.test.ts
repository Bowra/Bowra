/**
 * Interface appearance page-by-page acceptance A · integration/security (overview, Feishu, Slack, Teams, email service, public entries, OIDC)
 * source contract. Development slices: styles/ui-refresh-pages-settings.css (mappings) and ui-controls-legacy.css (legacy appearance).
 *
 * jsdom does not compute cascades; lock source-determinable facts here, with real-browser screenshots validating pixels.
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
  readSection,
  splitSelectors,
  stripComments,
} from "./css-contract";
import { withUiHooks } from "../components/settings/ui-hook-classes";

const RULES = parseRules(stripComments(readSection("styles/ui-refresh-pages-settings.css", "accept-a:integ")));
const LEGACY = parseRules(stripComments(readSection("styles/ui-controls-legacy.css", "accept-a:integ")));

/** Merge declarations for rules whose selector lists (including original :is(...) before expansion) contain class `sel`, constrained by @media. */
function rulesFor(sel: string, media: RegExp | null = null): Record<string, string> {
  const pattern = new RegExp(`${sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\w-])`);
  const found = RULES.filter(
    (r) =>
      (media ? media.test(r.media ?? "") : r.media === null) &&
      splitSelectors(r.selector).some((s) => {
        // Classes inside :not(...) are exclusions and do not count as matches.
        const bare = s.replace(/:not\([^)]*\)/g, "");
        return pattern.test(bare) && !/:(hover|focus)/.test(bare);
      }),
  );
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("styles/ui-refresh-pages-settings.css: every rule starts with the :root:root prefix", () => {
  it("解析出规则", () => {
    expect(RULES.length).toBeGreaterThan(60);
  });

  it("every selector starts with :root:root (inside @media too)", () => {
    const offenders = RULES.flatMap((rule) => splitSelectors(rule.selector)).filter((sel) => !sel.startsWith(PREFIX));
    expect(offenders).toEqual([]);
  });

  it("不写字面色值、不用 --c-line、不改层级与定位（保存栏由共用组件负责）", () => {
    const literal = RULES.filter((rule) => !splitSelectors(rule.selector).every(isTokenRoot)).flatMap((rule) =>
      stripComments(rule.body)
        .split(";")
        .map((d) => d.trim())
        .filter((d) => LITERAL_COLOR.test(d)),
    );
    expect(literal).toEqual([]);
    expect(RULES.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => norm(r.selector))).toEqual([]);
    expect(RULES.filter((r) => "z-index" in decls(r.body)).map((r) => norm(r.selector))).toEqual([]);
    const positioned = RULES.filter((r) => "position" in decls(r.body));
    for (const r of positioned) expect(decls(r.body).position, r.selector).toBe("static");
  });

  it("级联：后写的覆盖规则不输给先写的规则", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });
});

describe("CSS Module 钩子类（components/settings/ui-hook-classes.ts）", () => {
  it("每个类名保留原值并附上 `${prefix}-kebab` 全局钩子类", () => {
    const hooked = withUiHooks({ eyebrow: "m_eyebrow_1", stageSummaryCopy: "m_ssc_2", linkageTableFrame: "m_ltf" }, "ui-integ");
    expect(hooked).toEqual({
      eyebrow: "m_eyebrow_1 ui-integ-eyebrow",
      stageSummaryCopy: "m_ssc_2 ui-integ-stage-summary-copy",
      linkageTableFrame: "m_ltf ui-integ-linkage-table-frame",
    });
  });

  it.each([
    ["components/settings/integrations.tsx", "integration-pages.module.css", "ui-integ"],
    ["components/settings/feishu-integration-detail.tsx", "integration-pages.module.css", "ui-integ"],
    ["components/settings/slack-integration-detail.tsx", "integration-pages.module.css", "ui-integ"],
    ["components/settings/teams-integration-detail.tsx", "integration-pages.module.css", "ui-integ"],
    ["components/settings/email-config.tsx", "email-service.module.css", "ui-email"],
    ["components/settings/email-service-settings.tsx", "email-service.module.css", "ui-email"],
    ["components/settings/public-entries.tsx", "public-entries.module.css", "ui-pe"],
  ] as const)("%s 经钩子包装后再用（%s → %s-*）", (file, module, prefix) => {
    const src = read(file);
    expect(src).toContain(`import rawStyles from "./${module}";`);
    expect(src).toContain(`const styles = withUiHooks(rawStyles, "${prefix}");`);
    expect(src).not.toMatch(/import styles from "\.\/[\w-]+\.module\.css"/);
  });
});

describe("集成页：页头、按钮、卡片、分组标题", () => {
  it("眉题 12px --c-ink-4 普通字（不再大写、不再强调色）", () => {
    expect(rulesFor(".ui-integ-eyebrow")).toMatchObject({
      color: "var(--c-ink-4)",
      "font-size": "var(--fs-sm)",
      "font-weight": "400",
      "text-transform": "none",
      "letter-spacing": "normal",
    });
  });

  it("模块自写按钮对齐共享按钮：控件高、--r-2、填充底；主按钮强调色；返回是强调色文字的幽灵按钮", () => {
    expect(rulesFor(".ui-integ-refresh-button")).toMatchObject({
      "min-height": "var(--control-h-md)",
      "border-radius": "var(--r-2)",
      background: "var(--c-fill-2)",
      "font-size": "var(--fs-md)",
      border: "0",
    });
    expect(rulesFor(".ui-integ-primary-button")).toMatchObject({ background: "var(--c-accent)", color: "var(--c-accent-ink)" });
    expect(rulesFor(".ui-integ-back-button")).toMatchObject({ background: "transparent", color: "var(--c-accent-text)", "justify-self": "start" });
  });

  it("卡片：内容面、--r-4、发丝线外框（去掉投影与强调色描边）", () => {
    for (const card of [".ui-integ-provider-card", ".ui-integ-teams-config-card", ".ui-integ-stage", ".ui-integ-side-card", ".ui-integ-action-bar"]) {
      expect(rulesFor(card), card).toMatchObject({
        border: "0",
        background: "var(--c-surface)",
        "box-shadow": "0 0 0 var(--hairline) var(--c-separator)",
      });
    }
    expect(rulesFor(".ui-integ-stage[open]")).toMatchObject({ "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
  });

  it("分组标题 16/650 普通字、去掉右侧横线；卡片与阶段标题改系统字体（原衬线）", () => {
    expect(rulesFor(".ui-integ-section-label")).toMatchObject({ "font-size": "var(--fs-lg)", "font-weight": "650", "text-transform": "none" });
    expect(rulesFor(".ui-integ-section-label::after")).toMatchObject({ display: "none" });
    expect(rulesFor(".ui-integ-provider-title")).toMatchObject({ "font-family": "var(--font-control)", "font-weight": "650" });
    expect(rulesFor(".ui-integ-stage-summary-copy strong")).toMatchObject({ "font-family": "var(--font-control)", "font-weight": "650" });
    expect(rulesFor(".ui-integ-teams-card-header h2")).toMatchObject({ "font-family": "var(--font-control)", "font-size": "var(--fs-lg)" });
  });

  it("小字提档：侧栏标题不再大写，说明与提示不小于 11.5px", () => {
    expect(rulesFor(".ui-integ-side-card")).toBeDefined();
    const sideHead = RULES.find((r) => splitSelectors(r.selector).some((s) => s.includes(":is(.ui-integ-side-card, .ui-integ-stage-nav) h2")));
    expect(decls(sideHead!.body)).toMatchObject({ "text-transform": "none", "font-size": "var(--fs-md)" });
    const sizes = RULES.flatMap((r) => {
      const size = decls(r.body)["font-size"];
      return size && /^\d+(\.\d+)?px$/.test(size) ? [Number.parseFloat(size)] : [];
    });
    expect(Math.min(...sizes)).toBeGreaterThanOrEqual(11);
  });

  it("人工步骤说明去掉左侧色条（取舍 4），改语义浅底", () => {
    expect(rulesFor(".ui-integ-manual-note")).toMatchObject({ "border-left": "0", background: "var(--c-warn-soft)" });
  });

  it("录入型表单：输入框控件高、--r-2、控件描边，焦点强调色光晕", () => {
    expect(rulesFor(".ui-integ-input")).toMatchObject({
      height: "var(--control-h-md)",
      "border-radius": "var(--r-2)",
      border: "0",
    });
    expect(rulesFor(".ui-integ-teams-field input")).toMatchObject({ height: "var(--control-h-md)" });
    expect(rulesFor(".ui-integ-field label")).toMatchObject({ "font-size": "var(--fs-sm)", "font-weight": "600", color: "var(--c-ink-2)" });
  });
});

describe("飞书：主开关与接入方式规则（取舍 12：窄屏改列表行）", () => {
  it("≥ 640 表格、< 640 列表行（互斥显示）", () => {
    expect(rulesFor(".ui-integ-linkage-rows")).toMatchObject({ display: "none" });
    const narrow = /max-width:\s*639\.98px/;
    expect(rulesFor(".ui-integ-linkage-table", narrow)).toMatchObject({ display: "none" });
    expect(rulesFor(".ui-integ-linkage-rows", narrow)).toMatchObject({ display: "block" });
    expect(rulesFor(".ui-integ-linkage-rows-caption")).toMatchObject({ display: "none" });
    expect(rulesFor(".ui-integ-linkage-rows-caption", narrow)).toMatchObject({ display: "block" });
  });

  it("列表行：最小 56（窄屏触控行）、主标题 13 正文色、次要信息 12 --c-ink-3、行尾状态徽标、行间发丝线", () => {
    expect(rulesFor(".ui-integ-linkage-row")).toMatchObject({ "min-height": "56px", display: "grid" });
    expect(rulesFor(".ui-integ-linkage-row + .ui-integ-linkage-row")).toMatchObject({ "border-top": "var(--hairline) solid var(--c-separator)" });
    expect(rulesFor(".ui-integ-linkage-row__title")).toMatchObject({ "font-size": "var(--fs-md)", color: "var(--c-ink)" });
    expect(rulesFor(".ui-integ-linkage-row__meta")).toMatchObject({ "font-size": "var(--fs-sm)", color: "var(--c-ink-3)" });
    expect(rulesFor(".ui-integ-linkage-row__trail")).toMatchObject({ height: "20px", "border-radius": "999px" });
  });

  it("call site: table and list rows share the same data; the save bar saves", () => {
    const src = read("components/settings/feishu-integration-detail.tsx");
    expect(src).toContain("{linkageRows.map((row) => (\n                <tr key={row.key}>");
    expect(src).toContain('<p className="ui-integ-linkage-rows-caption" id="feishu-linkage-rows-caption">');
    expect(src).toContain('<ul className="ui-integ-linkage-rows" aria-labelledby="feishu-linkage-rows-caption">');
    expect(src).toMatch(/<SaveBar\s+dirty=\{dirty\}\s+saving=\{saving\}\s+guard=\{false\}/);
  });
});

describe("email service: form max width 560, group titles 16/650", () => {
  it("表单与分区", () => {
    expect(rulesFor(".ui-email-form")).toMatchObject({ "max-width": "560px" });
    expect(rulesFor(".ui-email-section-header h2")).toMatchObject({ "font-size": "var(--fs-lg)", "font-weight": "650" });
    expect(rulesFor(".ui-email-form-card")).toMatchObject({ border: "0", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
  });

  it("the base-layer section carries no hook prefix", () => {
    expect(LEGACY.flatMap((r) => splitSelectors(r.selector)).filter((s) => s.startsWith(":root"))).toEqual([]);
  });
});

describe("公开入口：卡片、徽标、当前请求", () => {
  it("卡片发丝线外框；当前入口去掉左侧强调色条", () => {
    expect(rulesFor(".ui-pe-card")).toMatchObject({ "box-shadow": "0 0 0 var(--hairline) var(--c-separator)", border: "0" });
    expect(rulesFor(".ui-pe-current-card")).toMatchObject({ "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
  });

  it("徽标 20 高、全圆角、11.5/600；徽标行垂直居中（窄屏不被行尾菜单撑高）", () => {
    expect(rulesFor(".ui-pe-badge")).toMatchObject({ height: "20px", "border-radius": "999px", "font-size": "0.71875rem" });
    expect(rulesFor(".ui-pe-current-badge")).toMatchObject({ background: "var(--c-accent-soft)", color: "var(--c-accent-text)" });
    expect(rulesFor(".ui-pe-badges")).toMatchObject({ "align-items": "center" });
  });
});
