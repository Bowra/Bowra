/**
 * Interface appearance page-acceptance finish: Files, Skills, turn replay, miscellaneous states (zero-space guidance, route error boundaries,
 * admin/settings permission denial), Help Center, and Notice / Field gaps from page acceptance A.
 *
 * jsdom does not cascade; enforce source facts here; real-browser screenshots validate pixels.
 *   1. Call sites use the classes migrated from inline styles (styles/ui-controls-legacy.css “tail:*” sections); no restored inline styles;
 *   2. Interface rules (styles/ui-refresh-pages-main.css “tail:files / tail:states / tail:help”, ui-refresh-pages-admin.css
 *      “tail:turns”) start every selector with :root:root, no literal colors/--c-line; later rules never lose to earlier ones;
 *   3. CSS Module pages (error boundaries, Help Center) expose stable hook classes; new appearance selects hooks only.
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

const LEGACY_FILE = "styles/ui-controls-legacy.css";
const MAIN_FILE = "styles/ui-refresh-pages-main.css";
const ADMIN_FILE = "styles/ui-refresh-pages-admin.css";

const LEGACY = ["tail:files", "tail:states", "tail:settings"].flatMap((name) =>
  parseRules(stripComments(readSection(LEGACY_FILE, name))),
);
const SECTIONS: Array<[string, string]> = [
  [MAIN_FILE, "tail:files"],
  [MAIN_FILE, "tail:states"],
  [MAIN_FILE, "tail:help"],
  [MAIN_FILE, "tail:settings"],
  [ADMIN_FILE, "tail:turns"],
];
const RULES = SECTIONS.flatMap(([file, name]) => parseRules(stripComments(readSection(file, name))));

/** Merge declarations with exact `${PREFIX} ${sel}` in selector list (or :is()). */
function on(sel: string, media: RegExp | null = null): Record<string, string> {
  const found = RULES.filter((r) => {
    if (media ? !media.test(r.media ?? "") : r.media !== null) return false;
    return splitSelectors(r.selector).some((full) => {
      if (!full.startsWith(PREFIX)) return false;
      const rest = norm(full.slice(PREFIX.length));
      if (rest === norm(sel)) return true;
      const m = /^:is\(([\s\S]*)\)$/.exec(rest);
      return m ? splitSelectors(m[1]).includes(norm(sel)) : false;
    });
  });
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("base classes moved from inline styles", () => {
  it("待上传行的「⋯」常显压过 .f-acts 的 opacity: 0（复合选择器 0,2,0，原来靠内联样式）", () => {
    expect(read("app/globals.css")).toMatch(/\.f-acts \{[^}]*opacity: 0;/);
    expect(LEGACY.some((r) => r.selector === ".f-acts.legacy-files-acts--pinned")).toBe(true);
  });
});

describe("调用点：原内联样式已移走，只剩按数据变化的宽度", () => {
  const styleProps = (src: string) => src.split("\n").filter((line) => line.includes("style={{")).map((line) => norm(line.slice(line.indexOf("style={{"))));

  it("文件页只剩骨架中段与上传进度两处按数据算的宽度", () => {
    expect(styleProps(read("app/(app)/files/page.tsx"))).toEqual(["style={{ width: `${w}%` }} />", "style={{ width: `${p.pct || 8}%` }} /></span>"]);
    expect(read("app/(app)/files/page.tsx")).toContain('<div className="f-acts legacy-files-acts--pinned">');
    expect(styleProps(read("components/files/file-dialogs.tsx"))).toEqual([]);
  });

  it("技能页只剩存储用量条的宽度", () => {
    const src = read("components/settings/skills-settings-content.tsx");
    expect(src).toContain('<div className="sgroup-head legacy-skills-group-head--static">');
    expect(src).not.toContain('style={{ cursor: "default" }}');
  });

  it("零空间引导页、设置页无权限、团队设置外壳的权限状态不再写内联样式", () => {
    expect(read("app/(app)/empty-workspace/page.tsx")).not.toContain("style=");
    expect(read("components/settings/access-boundary.tsx")).not.toContain("style=");
    const shell = read("components/settings/settings-shell.tsx");
    expect(shell).not.toContain('style={{ color: "var(--c-ink-2)" }}');
    expect(shell.match(/legacy-settings-gate ui-settings-gate/g)).toHaveLength(3);
  });

  it("Notice / Field 接收 className；导出、邮件服务、空间访问的提示条与字段不再写内联样式", () => {
    expect(read("components/ui/notice.tsx")).toContain("className?: string;");
    expect(read("components/ui/field.tsx")).toContain("className?: string;");
    expect(read("components/settings/export.tsx")).not.toMatch(/<Notice[^>]*style=/);
    expect(read("components/settings/email-config.tsx")).not.toMatch(/<Notice[^>]*style=/);
    expect(read("components/settings/members.tsx")).not.toMatch(/<Field[^>]*style=/);
  });

  it("CSS Module pages carry stable hook classes", () => {
    expect(read("components/boundary-error.tsx")).toContain('const styles = withUiHooks(moduleStyles, "ui-boundary");');
    expect(read("components/help/help-center-page.tsx")).toContain('const styles = withUiHooks(moduleStyles, "ui-help");');
  });
});

describe("interface rules start with the :root:root prefix", () => {
  it("每段都解析出规则，每个选择器都带开关前缀（含 @media 内）", () => {
    for (const [file, name] of SECTIONS) {
      expect(parseRules(stripComments(readSection(file, name))).length, `${file} ${name}`).toBeGreaterThan(2);
    }
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("不写字面色值；分隔线用发丝线（不用 --c-line）；不写 z-index", () => {
    const literal = RULES.filter((r) => !splitSelectors(r.selector).every(isTokenRoot)).flatMap((r) =>
      r.body.split(";").map((x) => x.trim()).filter((x) => LITERAL_COLOR.test(x)),
    );
    expect(literal).toEqual([]);
    expect(RULES.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => r.selector)).toEqual([]);
    expect(RULES.filter((r) => "z-index" in decls(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("没有被先写规则压住的覆盖", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });

  it("base-layer classes carry no hook prefix", () => {
    expect(LEGACY.flatMap((r) => splitSelectors(r.selector)).filter((sel) => sel.startsWith(":root"))).toEqual([]);
  });
});

describe("页面级状态（Page Layout Spec 第 4 节）", () => {
  it("零空间引导页：组标题 16/650、卡片发丝线、没有邀请时一行说明也在卡片里", () => {
    expect(on(".ui-ews-group-title")).toMatchObject({ "font-size": "var(--fs-lg)", "font-weight": "650" });
    expect(on(".ui-ews-card")).toMatchObject({ "box-shadow": "0 0 0 var(--hairline) var(--c-separator)", "border-radius": "var(--r-4)" });
    expect(on(".ui-ews-note")).toMatchObject({ background: "var(--c-surface)", color: "var(--c-ink-3)" });
  });

  it("错误边界：去掉红色左条与装饰渐变，小标不再大写等宽，标题 22/700，错误不整块染红", () => {
    expect(on(".ui-boundary-rail")).toEqual({ display: "none" });
    expect(on(".ui-boundary-shell")).toMatchObject({ background: "var(--c-bg)" });
    // Eyebrows use --c-ink-3 (--c-ink-4 falls below 4.5:1 on card background).
    expect(on(".ui-boundary-recovery-label")).toMatchObject({ "text-transform": "none", "font-family": "var(--font-control)", color: "var(--c-ink-3)" });
    expect(on(".ui-boundary-title")).toMatchObject({ "font-size": "var(--fs-page-title)", "font-weight": "700" });
    expect(on(".ui-boundary-primary-action")).toMatchObject({ background: "var(--c-accent)", color: "var(--c-accent-ink)" });
  });

  it("无权限不是错误：管理控制台无权限图标用中性底；设置页无权限说明是中性提示", () => {
    expect(on(".admin-route-state.is-forbidden .admin-route-state__mark")).toEqual({ background: "var(--c-fill-2)", color: "var(--c-ink-3)" });
    // Read failure retains error color: base rule (0,3,0) overrides globals.css error color; is-error must restore original values.
    expect(on(".admin-route-state.is-error .admin-route-state__mark")).toEqual({ background: "var(--c-err-soft)", color: "var(--c-err)" });
    expect(on(".admin-route-state h1")).toMatchObject({ "font-family": "var(--font-control)" });
    expect(on(".ui-access-state .ui-access-state-body")).toMatchObject({ background: "var(--c-fill-1)", color: "var(--c-ink-2)", border: "0" });
    expect(on(".ui-settings-gate:not(.ui-settings-gate--checking)")).toMatchObject({ "text-align": "center", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
  });

  it("文件页状态：标题 14/600、说明 12.5；技能页分组计数改系统字", () => {
    expect(on(".f-state p b")).toMatchObject({ "font-size": "0.875rem" });
    expect(on(".f-state p")).toMatchObject({ "font-size": "0.78125rem" });
    expect(on(".sset .sgroup-head .gcount")).toMatchObject({ "font-family": "var(--font-control)" });
  });
});

describe("帮助中心：控件层统一、文章正文不动", () => {
  it("分组标题 11/600 不大写；当前项填充底（不是强调色）；首页引导去渐变与投影", () => {
    expect(on(".ui-help-nav-group h2")).toMatchObject({ "text-transform": "none", "letter-spacing": "normal", "font-weight": "600" });
    expect(on(":is(.ui-help-home-link, .ui-help-nav-item).ui-help-nav-item-active")).toMatchObject({ background: "var(--c-fill-2)", color: "var(--c-ink)" });
    expect(on(".ui-help-home-hero")).toMatchObject({ background: "var(--c-surface)", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
    expect(on(".ui-help-hero-action")).toMatchObject({ background: "var(--c-accent)", "min-height": "var(--control-h-md)" });
  });

  it("只改文章标题与导语（页头），不碰 Markdown 正文的 h2 / p / 表格 / 代码", () => {
    const selectors = RULES.flatMap((r) => splitSelectors(r.selector)).map((s) => norm(s.slice(PREFIX.length)));
    const articleRules = selectors.filter((s) => s.startsWith(".ui-help-article"));
    expect(articleRules.sort()).toEqual([".ui-help-article > h1", ".ui-help-article > h1 + p", ".ui-help-article-meta"].sort());
  });
});

describe("逐页验收 A 未覆盖项", () => {
  it("OpenAPI：装密钥列表的外层卡片不画外框，每把密钥一张发丝线卡片（不再卡片套卡片）", () => {
    expect(on(".team-openapi-card:has(> .team-openapi-key-grid)")).toEqual({ padding: "0", background: "transparent", "box-shadow": "none" });
    expect(on(".team-openapi-key-card")).toMatchObject({ border: "0", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
  });
});

describe("回合重放（Page Layout Spec 列表页）", () => {
  it("三块内容都是内容面发丝线卡片；选中的轮次填充底加粗；表头 32；轮次 ID 与事件数据仍是等宽", () => {
    expect(on(".legacy-turns-events")).toMatchObject({ border: "0", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)" });
    expect(on(".legacy-turns-id.is-selected")).toEqual({ background: "var(--c-fill-2)", color: "var(--c-ink)", "font-weight": "600" });
    expect(on(".legacy-turns-th")).toMatchObject({ height: "max(var(--table-head-h), 2.4em)" });
    expect(on(".legacy-turns-id")).toMatchObject({ "font-family": "var(--font-mono)" });
    expect(on(".legacy-turns-payload")).toMatchObject({ "font-family": "var(--font-mono)" });
  });

  it("不重设窄屏上下排列段已定的布局（grid 列与最大高度）", () => {
    const base = RULES.filter((r) => r.media === null && /legacy-turns-(layout|ids)\b/.test(r.selector));
    for (const r of base) {
      expect(decls(r.body)).not.toHaveProperty("grid-template-columns");
      expect(decls(r.body)).not.toHaveProperty("max-height");
    }
  });
});
