/**
 * Page acceptance B style-source contracts for main functional pages: Reports, Library/Capability Center entry,
 * Knowledge Base, personal Capability Center,
 * and Agent Market / Management / Details / Version Release.
 *
 * jsdom does not cascade; enforce source-determinable facts here, with pixel effects checked by real-browser screenshots.
 *   1. Interface rules (styles/ui-refresh-pages-main.css) start with :root:root, use no literal colors, hairline dividers, and import last;
 *   2. Page Layout Spec headers: title 22/700, description 13 --c-ink-3, eyebrow 12 --c-ink-4; old serif page/section headings use system font;
 *   3. Call sites use the classes migrated from inline styles (styles/ui-controls-legacy.css “逐页验收 B：主要功能页”);
 *      callers no longer restore the old inline styles.
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

const RULES = parseRules(stripComments(read("styles/ui-refresh-pages-main.css")));
const LEGACY_RULES = parseRules(stripComments(read("styles/ui-controls-legacy.css")));

/** Combined declarations for a selector under the flag (possibly in an :is() list). */
function rule(sel: string, media?: RegExp): Record<string, string> {
  const found = RULES.filter((r) => {
    if (media ? !media.test(r.media ?? "") : r.media !== null) return false;
    return splitSelectors(r.selector).some((full) => {
      const rest = norm(full.slice(PREFIX.length));
      if (rest === norm(sel)) return true;
      const m = /^:is\(([\s\S]*)\)$/.exec(rest);
      return m ? splitSelectors(m[1]).includes(norm(sel)) : false;
    });
  });
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("styles/ui-refresh-pages-main.css: every rule starts with the :root:root prefix", () => {
  it("every selector starts with :root:root (inside @media too)", () => {
    expect(RULES.length).toBeGreaterThan(10);
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("不写字面色值；分隔线用发丝线（不用 --c-line）；不写 z-index", () => {
    const literal = RULES.filter((r) => !splitSelectors(r.selector).every(isTokenRoot)).flatMap((r) =>
      stripComments(r.body).split(";").map((x) => x.trim()).filter((x) => LITERAL_COLOR.test(x)),
    );
    expect(literal).toEqual([]);
    expect(RULES.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => r.selector)).toEqual([]);
    expect(RULES.filter((r) => "z-index" in decls(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("没有被先写规则压住的覆盖", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });

  it("keeps the ordered authenticated styles starting with ui-refresh-pages-main.css", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    const expectedImports = ["../../styles/ui-refresh-pages-main.css", "../../styles/ui-refresh-canvas.css", "../../styles/ui-refresh-workbench.css", "../../styles/ui-refresh-materials.css"];
    const start = imports.indexOf(expectedImports[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(imports.slice(start, start + expectedImports.length)).toEqual(expectedImports);
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-pages-main.css");
  });
});

describe("页头（Page Layout Spec「页头固定四件」）", () => {
  it("uses the shared PageHeader (title and description classes) in reports, capability and Agent catalogs", () => {
    const header = read("components/shell/page-frame.tsx");
    expect(header).toContain('<h1 className="ui-page-title">{heading}</h1>');
    expect(header).toContain('<p className="ui-page-desc">{description}</p>');
    for (const file of [
      "app/(app)/reports/page.tsx",
      "components/capabilities/capability-center.tsx",
      // Marketplace, My agents and Top Charts share AgentPageHeader (polish spec D1), now a PageHeader.
      "components/agents/agent-page-frame.tsx",
      "app/(app)/knowledge/page.tsx",
      "components/settings/skills-settings-content.tsx",
    ]) {
      expect(read(file), file).toContain("<PageHeader");
    }
  });

  it("智能体管理、详情、版本发布与审阅的页标题 22/700 系统字体，说明 13 --c-ink-3", () => {
    for (const sel of [".am-detail-copy h1", ".am-release-hero h1", ".am-review-hero h1"]) {
      expect(rule(sel), sel).toMatchObject({ "font-family": "var(--font-control)", "font-size": "var(--fs-page-title)", "font-weight": "700", "letter-spacing": "-0.01em" });
    }
    expect(rule(".am-detail-copy > p")).toMatchObject({ color: "var(--c-ink-3)", "font-size": "var(--fs-md)", "max-width": "560px" });
  });

  it("小标 12 --c-ink-4 普通字（报告库、入口页、智能体各页）", () => {
    for (const sel of [".am-section-heading span", ".am-release-hero p"]) {
      expect(rule(sel), sel).toMatchObject({ color: "var(--c-ink-4)", "font-size": "var(--fs-sm)", "font-weight": "400", "text-transform": "none", "letter-spacing": "normal" });
    }
  });

  it("分区标题与分组标题同值（16/650），卡片内小标题 15/650，全部系统字体", () => {
    const group = decls(parseRules(stripComments(read("styles/ui-refresh-pages-settings.css"))).find((r) => r.selector === `${PREFIX} .ui-group-title`)!.body);
    for (const sel of [".am-section-heading h2", ".am-detail-section-heading h2", ".am-release-inbox h2", ".am-empty h3"]) {
      const got = rule(sel);
      for (const prop of ["font-family", "font-size", "font-weight", "line-height"]) expect(got[prop], `${sel} ${prop}`).toBe(group[prop]);
    }
    for (const sel of [".am-localization-source h3", ".am-taxonomy-grid legend"]) {
      expect(rule(sel), sel).toMatchObject({ "font-family": "var(--font-control)", "font-size": "0.9375rem", "font-weight": "650" });
    }
  });

  it("Agent sibling pages: the header and tabs come from PageFrame; the filled 28px toolbar spans the column", () => {
    // Header and tabs: styles/ui-refresh-page-frame.css (styles/ui-refresh-page-frame.test.ts).
    expect(RULES.some((r) => /\.am-page-(header|tabs)\b/.test(r.selector))).toBe(false);
    expect(rule(".am-toolbar")).toMatchObject({ width: "100%", margin: "0 0 12px" });
    expect(rule(".am-toolbar .am-search")).toMatchObject({ "min-height": "28px", background: "var(--c-fill-2)", "border-radius": "var(--r-2)" });
  });

  it("智能体原样式表里的衬线页标题 / 分区标题都有覆盖（新加一个没接上会红）", () => {
    const own = parseRules(stripComments(read("styles/agent-marketplace.css")))
      .filter((r) => /var\(--font-v2-serif\)/.test(r.body) && /\b(h1|h2)\b/.test(r.selector))
      .flatMap((r) => splitSelectors(r.selector))
      // Dialogs/bottom sheets are covered by batch five; top-bar wordmark is branding; Market home title uses .ui-page-title (above).
      .filter((sel) => !/am-dialog|am-fork|am-filter-sheet|am-brand|am-hero/.test(sel));
    const missing = own.filter((sel) => {
      try {
        rule(sel);
        return false;
      } catch {
        return true;
      }
    });
    expect(missing).toEqual([]);
  });
});

describe("landing pages and report library", () => {
  it("the report library header is PageHeader: no own hero, eyebrow or home link remain", () => {
    expect(RULES.some((r) => /reports-library-(hero|kicker)|reports-home-link/.test(r.selector))).toBe(false);
    expect(read("app/(app)/reports/page.tsx")).toContain('<PageHeader description={t("description")} />');
  });
});

describe("acceptance B: main pages use classes instead of inline styles", () => {
  it("call sites use the base-layer classes and the inline styles stay removed", () => {
    const expectations: [string, string[], RegExp[]][] = [
      // Share icon <ShIcon style> is a component parameter (shared palette constant), not a page inline style.
      ["app/(app)/reports/page.tsx", ["legacy-reports-shared-row", "legacy-reports-delete-session", "legacy-reports-download"], [/^(?!.*<ShIcon).*style=\{\{/m]],
      ["app/(app)/knowledge/page.tsx", ["legacy-kb-page-loading", "legacy-kb-space-access", "legacy-kb-space-select"], [/style=\{\{/]],
      ["components/capabilities/personal-capability-page.tsx", ["PageFrame"], [/style=\{\{/]],
      ["components/agents/agent-management.tsx", ["legacy-am-muted-block"], [/style=\{\{/]],
      ["components/agents/agent-management-route.tsx", ["legacy-am-legacy-editor-intro"], [/style=\{\{/]],
    ];
    for (const [file, present, absent] of expectations) {
      const src = read(file);
      for (const p of present) expect(src, `${file}: ${p}`).toContain(p);
      for (const a of absent) expect(a.test(src), `${file}: ${a}`).toBe(false);
    }
  });
});

describe("智能体市场的两处对比度缺陷（两种外观都生效）", () => {
  const own = parseRules(stripComments(read("styles/agent-marketplace.css")));
  it("精选卡片的能力小标只作用于文字区，不再命中插图里的领域字", () => {
    const sels = own.flatMap((r) => splitSelectors(r.selector));
    expect(sels).toContain(".am-featured-card > div:last-child > span");
    expect(sels).not.toContain(".am-featured-card > div > span");
    // Illustration text retains its original pale color (not overridden by the capability-eyebrow accent).
    expect(decls(own.find((r) => r.selector === ".am-artwork span")!.body).color).toBe("color-mix(in srgb, var(--c-surface) 94%, transparent)");
  });

  it("搜索框 ⌘K 提示用 --c-ink-3（≥ 4.5:1）", () => {
    expect(decls(own.find((r) => r.selector === ".am-search kbd" && r.media === null)!.body).color).toBe("var(--c-ink-3)");
  });
});

