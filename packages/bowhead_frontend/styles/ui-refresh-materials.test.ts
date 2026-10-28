/**
 * P2 batch 1 style-source contracts: three material tiers, three divider tiers, accent text on materials, opaque persistent sidebar.
 *
 * jsdom does not cascade; enforce source facts here; real browsers validate pixels, contrast and layers (technical design “P2 第 1 批结果”).
 *   1. Every rule starts with the :root:root prefix, no literal colors, tokens derive from ink / accent / bg / surface;
 *      authenticated layout imports last; public layout (export screenshots) does not import it;
 *   2. Spec: bar material background 94%, hairline ink 12% / 14%, structural line 20% / 24%, sidebar half surface/half background;
 *      reduced transparency, increased contrast or unsupported blur falls back to opaque background; increased contrast
 * replaces hairlines with structural lines;
 *   3. Specified blur sites: top bar transparent at rest, bar material after scroll; sticky toolbar, back bar, Save bar,
 * draft command bar, mobile composer dock, image download use bar material;
 *      results-navigation/quick-switch popovers use overlay material; remove HTML fullscreen-backdrop blur; blur only
 * via --ui-material-filter with -webkit- prefix;
 *   4. useScrollEdge writes data-scrolled for top-bar scroll edges; no legacy rule reads it.
 */
import { readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  decls,
  FRONTEND,
  isTokenRoot,
  LITERAL_COLOR,
  norm,
  parseRules,
  PREFIX,
  read,
  splitSelectors,
  stripComments,
} from "./css-contract";

const CSS = stripComments(read("styles/ui-refresh-materials.css"));
const RULES = parseRules(CSS);

/** Combined prefixed-selector declarations (possibly inside :is()); undefined media includes only rules outside @media / @supports. */
function rule(sel: string, media?: RegExp): Record<string, string> {
  const found = RULES.filter((r) => {
    if (media ? !media.test(r.media ?? "") : r.media !== null) return false;
    return splitSelectors(r.selector).some((full) => {
      const rest = norm(full.slice(PREFIX.length));
      if (rest === norm(sel)) return true;
      const m = /^:is\(([\s\S]*)\)(.*)$/.exec(rest);
      return m ? splitSelectors(m[1]).map((x) => norm(x + m[2])).includes(norm(sel)) : false;
    });
  });
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("styles/ui-refresh-materials.css: every rule starts with the :root:root prefix", () => {
  it("every selector starts with :root:root (inside @media / @supports too)", () => {
    expect(RULES.length).toBeGreaterThan(10);
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("不写字面色值（token 根上也不写：全部从团队配色的 ink / accent / bg / surface 派生）；不写 z-index", () => {
    const literal = RULES.flatMap((r) => r.body.split(";").map((x) => x.trim()).filter((x) => LITERAL_COLOR.test(x)));
    expect(literal).toEqual([]);
    expect(RULES.filter((r) => "z-index" in decls(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("模糊只经 --ui-material-filter（或 none），写了 backdrop-filter 的都同时写 -webkit- 前缀且值相同", () => {
    const withFilter = RULES.filter((r) => "backdrop-filter" in decls(r.body) || "-webkit-backdrop-filter" in decls(r.body));
    expect(withFilter.length).toBeGreaterThan(4);
    for (const r of withFilter) {
      const d = decls(r.body);
      expect(d["-webkit-backdrop-filter"], r.selector).toBe(d["backdrop-filter"]);
      expect(d["backdrop-filter"].replace(/\s*!important$/, ""), r.selector).toMatch(/^(var\(--ui-material-filter\)|none)$/);
    }
    expect(RULES.filter((r) => /blur\(/.test(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("keeps the ordered authenticated styles starting with ui-refresh-materials.css", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    // P2 batch 3's ui-refresh-chrome.css, batch 6's ui-refresh-content.css and batch 5's ui-refresh-kb.css come after it
    const expectedImports = [
      "../../styles/ui-refresh-materials.css",
      "../../styles/ui-refresh-chrome.css",
      "../../styles/ui-refresh-content.css",
    ];
    const start = imports.indexOf(expectedImports[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(imports.slice(start, start + expectedImports.length)).toEqual(expectedImports);
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-materials.css");
  });
});

describe("token（P2 Materials Spec）", () => {
  const root = (media?: RegExp) =>
    Object.assign(
      {},
      ...RULES.filter((r) => (media ? media.test(r.media ?? "") : r.media === null) && splitSelectors(r.selector).includes(PREFIX)).map((r) => decls(r.body)),
    );
  const dark = () =>
    Object.assign({}, ...RULES.filter((r) => r.media === null && splitSelectors(r.selector).includes(`${PREFIX}[data-theme="dark"]`)).map((r) => decls(r.body)));

  it("栏材质 = background 94%；侧栏 = surface 与 background 各半（oklab）", () => {
    expect(root()).toMatchObject({
      "--ui-material-bar": "color-mix(in srgb, var(--c-bg) 94%, transparent)",
      "--c-sidebar": "color-mix(in oklab, var(--c-surface) 50%, var(--c-bg))",
    });
  });

  it("分隔线三档：发丝线 ink 12% / 深色 14%，结构线 20% / 24%（控件边框 --c-control-line / --c-line 不改）", () => {
    expect(root()).toMatchObject({
      "--c-separator": "color-mix(in srgb, var(--c-ink) 12%, transparent)",
      "--c-separator-strong": "color-mix(in srgb, var(--c-ink) 20%, transparent)",
    });
    expect(dark()).toMatchObject({
      "--c-separator": "color-mix(in srgb, var(--c-ink) 14%, transparent)",
      "--c-separator-strong": "color-mix(in srgb, var(--c-ink) 24%, transparent)",
    });
    expect(CSS).not.toMatch(/--c-(control-)?line\s*:/);
  });

  it("回退：减少透明度、增强对比度、不支持模糊时栏材质 = background 实底；增强对比度下发丝线换结构线", () => {
    expect(root(/prefers-reduced-transparency: reduce\), \(prefers-contrast: more/)).toMatchObject({ "--ui-material-bar": "var(--c-bg)" });
    expect(root(/^@supports not \(\(backdrop-filter: blur\(1px\)\) or \(-webkit-backdrop-filter: blur\(1px\)\)\)$/)).toMatchObject({ "--ui-material-bar": "var(--c-bg)" });
    // Increased contrast: bar-bottom hairlines/etc. become 1px structural lines.
    expect(root(/^@media \(prefers-contrast: more\)$/)).toMatchObject({ "--c-separator": "var(--c-separator-strong)", "--hairline": "1px" });
    // Overlay-material/filter fallbacks remain in batch three (ui-refresh.css); do not redefine here.
    expect(CSS).not.toMatch(/--ui-material-(bg|filter)\s*:/);
  });
});

describe("13 处模糊的去处", () => {
  it("顶栏（桌面与手机）：静止透明、无线、无模糊；data-scrolled 时栏材质 + 底部发丝线", () => {
    expect(rule(".v2-main-head")).toMatchObject({
      background: "transparent",
      "border-bottom-color": "transparent",
      "box-shadow": "none",
      "backdrop-filter": "none",
    });
    expect(rule(".v2-mobile-topbar")).toMatchObject({ background: "transparent", "backdrop-filter": "none" });
    expect(rule(".v2-main-head[data-scrolled]")).toMatchObject({
      background: "var(--ui-material-bar)",
      "backdrop-filter": "var(--ui-material-filter)",
      "box-shadow": "inset 0 calc(-1 * var(--hairline)) 0 var(--c-separator)",
    });
    expect(rule(".v2-mobile-topbar[data-scrolled]")).toMatchObject({ background: "var(--ui-material-bar)" });
  });

  it("栏材质：报告吸顶工具条、返回条、图片下载按钮；保存栏与草稿命令条就地改用栏材质", () => {
    for (const sel of [".results-report-object-toolbar", ".results-return-context"]) {
      expect(rule(sel), sel).toMatchObject({ background: "var(--ui-material-bar)", "backdrop-filter": "var(--ui-material-filter)" });
    }
    expect(rule(".ui-image-download")).toMatchObject({ background: "var(--ui-material-bar) !important" });
    const settings = parseRules(stripComments(read("styles/ui-refresh-pages-settings.css")));
    for (const sel of [".ui-savebar"]) {
      const d = Object.assign({}, ...settings.filter((r) => r.media === null && norm(r.selector) === `${PREFIX} ${sel}`).map((r) => decls(r.body)));
      expect(d.background, sel).toBe("var(--ui-material-bar)");
    }
  });

  it("single column (< 1024, P2 batch 4) composer dock: bar material + top hairline (the original rule is !important, so is this one)", () => {
    expect(rule(".v2-composer-wrap:not(.home-work-composer-inner *)", /max-width: 1023\.98px/)).toMatchObject({
      background: "var(--ui-material-bar) !important",
      "border-top-color": "transparent !important",
      "backdrop-filter": "var(--ui-material-filter)",
    });
  });

  it("首页的输入框（不吸底、外壳透明）不套栏材质：每条写到输入框外壳的规则都排除首页", () => {
    const wraps = RULES.filter((r) => /v2-composer-wrap/.test(r.selector));
    expect(wraps.length).toBeGreaterThan(0);
    for (const r of wraps) {
      for (const sel of splitSelectors(r.selector)) expect(sel, sel).toContain(":not(.home-work-composer-inner *)");
    }
    // globals.css resets home shell to transparent/not bottom-sticky; this batch must not override it.
    expect(stripComments(read("app/globals.css"))).toMatch(/\.home-work-composer-inner \.v2-composer-wrap \{[^}]*background: transparent !important/);
  });

  it("浮层材质：结果导航只有浮层形态（固定与手机形态是实底）、结果快速切换", () => {
    expect(rule(".results-navigator--overlay")).toMatchObject({ background: "var(--ui-material-bg)" });
    expect(rule(".results-quick")).toMatchObject({ background: "var(--ui-material-bg)" });
    expect(CSS).not.toMatch(/results-navigator--(pinned|mobile)/);
  });

  it("HTML 全屏遮罩：--c-scrim、去模糊", () => {
    expect(rule(".rc-embed-overlay")).toMatchObject({ background: "var(--c-scrim)", "backdrop-filter": "none" });
  });
});

describe("材质上的强调字与侧栏", () => {
  it("本文件里不出现材质上的纯强调色文字；报告工具条（栏材质）上的「已暂停」用 --c-warn-ink", () => {
    const canvas = parseRules(stripComments(read("styles/ui-refresh-canvas.css")));
    const paused = canvas.find((r) => norm(r.selector) === `${PREFIX} .rc-toolbar-stream.is-paused`);
    expect(paused && decls(paused.body).color).toBe("var(--c-warn-ink)");
    expect(RULES.filter((r) => decls(r.body).color === "var(--c-accent)" && !/unified-sidebar/.test(r.selector)).map((r) => r.selector)).toEqual([]);
  });

  it("深色浮层材质 96%（P2 第 1 批由 95% 提高，留对比度余量）", () => {
    const refresh = parseRules(stripComments(read("styles/ui-refresh.css")));
    const dark = refresh.find((r) => r.media === null && r.selector.trim() === `${PREFIX}[data-theme="dark"]` && r.body.includes("--ui-material-bg"));
    expect(dark && decls(dark.body)["--ui-material-bg"]).toBe("color-mix(in srgb, var(--c-surface) 96%, transparent)");
  });

  it("常驻侧栏实底 --c-sidebar、右缘 1px 结构线（浮层展开态不在内）；图标未选中 ink2、选中强调色", () => {
    const sidebar = RULES.find((r) => r.selector.includes(".unified-sidebar:not(.is-overlay)"));
    expect(sidebar && decls(sidebar.body)).toMatchObject({ background: "var(--c-sidebar)", "border-right": "1px solid var(--c-separator-strong)" });
    const icons = RULES.filter((r) => /> svg$/.test(r.selector.trim())).map((r) => decls(r.body).color);
    expect(icons).toEqual(["var(--c-ink-2)", "var(--c-accent)"]);
  });
});

describe("base stylesheets and call sites", () => {
  it("only the refreshed stylesheets read data-scrolled and ui-image-download", () => {
    const sheets = [
      "app/globals.css",
      ...readdirSync(join(FRONTEND, "styles"))
        .filter((name) => name.endsWith(".css") && !name.startsWith("ui-refresh"))
        .map((name) => `styles/${name}`),
    ];
    expect(sheets).toContain("styles/ui-controls-legacy.css");
    expect(sheets.filter((path) => /data-scrolled|ui-image-download/.test(stripComments(read(path))))).toEqual([]);
  });

  it("两处图片下载按钮保留原内联样式（导出截图页不加载刷新样式，按钮外观仍来自内联）", () => {
    for (const file of ["components/chat/step-list/ChatInlineImage.tsx", "components/canvas/sections/image.tsx"]) {
      const src = read(file);
      expect(src, file).toContain('className="ui-image-download"');
      expect(src, file).toContain('backdropFilter: "blur(4px)"');
    }
  });

  it("两个顶栏都挂 useScrollEdge", () => {
    expect(read("components/shell/Topbar.tsx")).toMatch(/useScrollEdge\(headRef\)[\s\S]*ref=\{headRef\} className="v2-main-head"/);
    expect(read("components/mobile-topbar.tsx")).toMatch(/useScrollEdge\(barRef\)[\s\S]*ref=\{barRef\} className="v2-mobile-topbar"/);
  });
});
