/**
 * Interface appearance page-by-page acceptance A · personal group: account, security, appearance, memory, export, access
 * requests, My Connections, Feishu account binding.
 *
 * jsdom does not compute cascades; lock source-determinable facts here; real-browser screenshots validate pixels.
 *   1. Mapping: every selector starts with :root:root, no literal colors or --c-line, later rules do not lose to earlier ones;
 *   2. Call sites: original inline styles are removed.
 *
 * During development, mapping is in styles/ui-refresh-pages-settings.css and legacy appearance in
 * styles/ui-controls-legacy.css; after merging, they respectively enter
 * styles/ui-refresh-pages-settings.css and the styles/ui-controls-legacy.css “逐页验收 A” section; this file accepts both locations.
 */
import { existsSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  cascadeProblems,
  decls,
  FRONTEND,
  LITERAL_COLOR,
  norm,
  parseRules,
  PREFIX,
  read,
  readSection,
  splitSelectors,
  stripComments,
} from "./css-contract";

const pick = (shard: string, merged: string) => (existsSync(join(FRONTEND, shard)) ? shard : merged);
const MAP_FILE = pick("styles/ui-refresh-pages-settings.css", "styles/ui-refresh-pages-settings.css");
const LEGACY_FILE = pick("styles/ui-controls-legacy.css", "styles/ui-controls-legacy.css");
const RULES = parseRules(stripComments(readSection(MAP_FILE, "accept-a:personal")));
const LEGACY = parseRules(stripComments(readSection(LEGACY_FILE, "accept-a:personal")));

/** Merge declarations for rules containing exactly `${PREFIX} ${sel}` in their selector lists (constrained by @media, outside @media by default). */
function exact(sel: string, media: RegExp | null = null): Record<string, string> {
  const found = RULES.filter(
    (r) => (media ? media.test(r.media ?? "") : r.media === null) && splitSelectors(r.selector).includes(`${PREFIX} ${norm(sel)}`),
  );
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}


const personalRules = RULES.filter((r) =>
  /legacy-profile|personal-security|pa-|cs-member|legacy-export|export-template|legacy-acr|legacy-capability|personal-mcp|fb-|memory-v2/.test(r.selector),
);

describe("personal group mappings start with the :root:root prefix", () => {
  it("解析出个人组的规则", () => {
    expect(personalRules.length).toBeGreaterThan(60);
  });

  it("every selector starts with :root:root (inside @media too)", () => {
    const offenders = personalRules.flatMap((rule) => splitSelectors(rule.selector)).filter((sel) => !sel.startsWith(PREFIX));
    expect(offenders).toEqual([]);
  });

  it("不写字面色值、不用 --c-line", () => {
    const literal = personalRules.filter((r) =>
      stripComments(r.body)
        .split(";")
        .some((d) => LITERAL_COLOR.test(d)),
    );
    expect(literal.map((r) => norm(r.selector))).toEqual([]);
    expect(personalRules.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => norm(r.selector))).toEqual([]);
  });

  it("后写的覆盖不输给先写的规则", () => {
    expect(cascadeProblems(personalRules)).toEqual([]);
  });

  it("base-layer classes carry no hook prefix", () => {
    const personalLegacy = LEGACY.filter((r) => /legacy-(profile|export|acr|capability)/.test(r.selector));
    expect(personalLegacy.length).toBeGreaterThan(20);
    expect(personalLegacy.filter((r) => r.selector.startsWith(":root"))).toEqual([]);
  });
});

describe("账号（profile.tsx）：分组卡片 + 保存栏", () => {
  it("content width follows the shell (880), the label column is no longer 110, email and credits use the system font (tabular credits)", () => {
    expect(exact(".legacy-profile-page")).toMatchObject({ "max-width": "none" });
    expect(exact(".legacy-profile-row > .legacy-profile-label")).toMatchObject({ width: "auto" });
    expect(exact(".legacy-profile-mono")).toMatchObject({ "font-family": "var(--font-control)", "font-variant-numeric": "tabular-nums" });
  });

  it("call site: grouped cards, and the save bar saves", () => {
    const src = read("components/settings/profile.tsx");
    expect(src).toContain('<section className="ui-group profile-group">');
    expect(src).toContain('<div className="ui-group-card">{children}</div>');
    expect(src).toContain("<SaveBar dirty={dirty} saving={busy} onSave={() => { void saveProfile(); }} onDiscard={discard} />");
    expect(src).not.toMatch(/style=\{\{ (maxWidth: 640|fontFamily: "var\(--font-v2-mono\)"|color: "var\(--c-ink-3\)")/);
    expect(src).not.toContain("badgeStyle(");
  });
});

describe("安全（security.tsx）：纯 CSS 映射", () => {
  it("每段标题 16/650 在卡片外，内容进内容面卡片（发丝线、--r-4）", () => {
    expect(exact(".personal-security-card__heading h2")).toMatchObject({ "font-size": "var(--fs-lg)", "font-weight": "650", "font-family": "var(--font-control)" });
    expect(exact(".personal-security-card")).toMatchObject({ border: "0", background: "transparent", padding: "0" });
    expect(exact(".personal-security-card__action")).toMatchObject({
      background: "var(--c-surface)",
      "border-radius": "var(--r-4)",
      "box-shadow": "0 0 0 var(--hairline) var(--c-separator)",
      "min-height": "var(--group-row-min-h)",
    });
    expect(exact(".personal-security-session + .personal-security-session")).toMatchObject({ "box-shadow": "inset 0 var(--hairline) 0 var(--c-separator)" });
  });

  it("call site: the loading skeleton replaces the content and keeps the header", () => {
    const src = read("components/settings/security.tsx");
    expect(src).toMatch(/if \(!me && !error\) \{\s+return <div className="personal-security-page">\{header\}<LoadingSkeleton variant="form"/);
  });
});

describe("外观（personal-appearance-settings.tsx）：pa-* 钩子", () => {
  it("页头去掉范围图标；分区标题 16/650、眉题隐藏；分区内容一张卡片、行间发丝线", () => {
    expect(exact(".pa-heading .pa-scope-icon")).toMatchObject({ display: "none" });
    expect(exact(".pa-section-heading h2")).toMatchObject({ "font-size": "var(--fs-lg)", "font-weight": "650", "font-family": "var(--font-control)" });
    expect(exact(".pa-section-heading > span")).toMatchObject({ display: "none" });
    expect(exact(".pa-section-body")).toMatchObject({ background: "var(--c-surface)", "border-radius": "var(--r-4)" });
    expect(exact(".pa-section-body > * + .pa-setting-row")).toMatchObject({ "box-shadow": "inset 0 var(--hairline) 0 var(--c-separator)" });
    expect(exact(".pa-setting-row[data-compact=\"true\"]")).toMatchObject({ "grid-template-columns": "var(--group-label-w) minmax(0, 1fr)", "align-items": "start" });
    expect(exact(".pa-setting-row[data-compact=\"true\"]", /max-width:\s*767\.98px/)).toMatchObject({ display: "block" });
  });

  it("call sites: hook classes sit beside the module classes; while loading the header stays and the content shows a skeleton", () => {
    const src = read("components/settings/personal-appearance-settings.tsx");
    for (const hook of ["pa-page", "pa-heading", "pa-section", "pa-section-heading", "pa-section-body", "pa-setting-row", "pa-setting-head", "pa-inheritance", "pa-danger-zone", "pa-current-effect"]) {
      expect(src, hook).toContain(` ${hook}\``);
    }
    expect(src).toMatch(/if \(!errorCode\) \{[\s\S]{0,200}<LoadingSkeleton variant="form"/);
  });
});

describe("导出（export.tsx）", () => {
  it("uses the shared page heading and grouped export guidance", () => {
    const src = read("components/settings/export.tsx");
    expect(src).toContain('<h1 className="ui-page-title legacy-export-title">');
    expect(src).toContain('legacy-export-section ui-group');
    expect(src).not.toMatch(/style=\{\{(?! marginTop: 12 \}\})/);
  });
});

describe("访问申请（access-requests-content.tsx）", () => {
  it("requests sit in one card", () => {
    expect(exact(".legacy-acr-list")).toMatchObject({ gap: "0", background: "var(--c-surface)", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
    expect(exact(".legacy-acr-item + .legacy-acr-item")).toMatchObject({ "box-shadow": "inset 0 var(--hairline) 0 var(--c-separator)" });
  });

  it("call site: no inline styles; loading goes through LoadingSkeleton", () => {
    const src = read("components/settings/access-requests-content.tsx");
    expect(src).not.toContain("style={{");
    expect(src).toContain('<LoadingSkeleton rows={3} label={t("loading")} />');
  });
});

describe("我的连接（/connections）", () => {
  // Page-by-page acceptance B extracted and locked wrapper legacy .legacy-capability-scroll / .legacy-capability-main values (ui-refresh-pages-admin.test.ts).

  it("list page uses the PageFrame column; Saved connections is a group title", () => {
    // The 1120 column and breakpoint gutters: styles/ui-refresh-page-frame.css (styles/ui-refresh-page-frame.test.ts).
    expect(read("components/capabilities/personal-capability-page.tsx")).toContain("<PageFrame");
    expect(exact(".personal-mcp-list-heading h3")).toMatchObject({ "font-size": "var(--fs-lg)", "font-weight": "650" });
  });

  it("call sites: no inline frame styles; only My Connections declares a width tier; the page title is the route's h1", () => {
    const page = read("components/capabilities/personal-capability-page.tsx");
    expect(page).not.toContain("style={{");
    expect(page).toContain("<PageFrame title={mobileTitle} width={width}");
    expect(read("app/(app)/connections/page.tsx")).toContain('width="table"');
    // The H1 repeats the top-bar title (large-title pattern, PageHeader defaults to the frame title); Personal MCP is a section.
    expect(read("app/(app)/connections/page.tsx")).toContain("<PageHeader title={title} description=");
    expect(read("components/settings/personal-mcp-settings.tsx")).toContain(
      '<h2 className="personal-mcp-section-title">{t("settingsTitle")}</h2>',
    );
    expect(exact(".personal-mcp-page-header h2.personal-mcp-section-title")).toMatchObject({
      "font-size": "var(--fs-xl)",
      "font-weight": "650",
    });
  });
});

describe("记忆（memory-v2-content.tsx）：已选筛选 chip", () => {
  it("chip 行与缩进 50 的筛选行对齐，≤ 720 不缩进", () => {
    expect(exact(".memory-v2-filters + .ui-filter-chips")).toMatchObject({ "margin-left": "50px" });
    expect(exact(".memory-v2-filters + .ui-filter-chips", /max-width:\s*720px/)).toMatchObject({ "margin-left": "0" });
  });

  it("调用点：搜索词、类型、范围三个已生效条件都能单独去掉，「清除全部」复用原清空", () => {
    const src = read("components/memory/memory-v2-content.tsx");
    expect(src).toContain("<ActiveFilterChips");
    for (const key of ['key: "query"', 'key: "type"', 'key: "scope"']) expect(src).toContain(key);
    expect(src).toContain("onClearAll={clearFilters}");
  });
});

describe("绑定飞书账号（/me/integrations/feishu/bind）", () => {
  it("no decorative gradient, hairline cards, 22/700 system-font title, plain eyebrow", () => {
    expect(exact(".fb-page")).toMatchObject({ background: "none" });
    expect(exact(".fb-card")).toMatchObject({ border: "0", "border-radius": "var(--r-4)", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
    expect(exact(".fb-title")).toMatchObject({ "font-family": "var(--font-control)", "font-size": "var(--fs-page-title)", "font-weight": "700" });
    expect(exact(".fb-eyebrow")).toMatchObject({ "text-transform": "none", "letter-spacing": "normal" });
  });

  it("调用点：钩子 class 挂在模块 class 旁", () => {
    const src = read("app/(app)/me/integrations/feishu/bind/page.tsx");
    for (const hook of ["fb-page", "fb-card", "fb-title", "fb-eyebrow", "fb-provider", "fb-primary", "fb-secondary"]) {
      expect(src, hook).toContain(` ${hook}\``);
    }
  });
});
