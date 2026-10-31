/**
 * Interface appearance batch 5: source contracts for surfaces earlier four batches marked
 * as visually distinctive or inline-styled and uncovered (styles/ui-refresh-surfaces.css).
 *
 * jsdom lacks cascade; guard source-determinable facts here, real-browser screenshots verify pixels.
 *   1. Every selector in every rule starts with :root:root (including @media);
 *      no literal colors except tokens on the :root:root token block; no z-index (appearance only, no layer changes).
 *   2. Imported by authenticated (app) layout only, after ui-refresh.css; never public layout.
 *   3. Families match specification: dialogs/panels, backdrops, titles, button variants, navigation drawer, “@”
 * completion, business badges, matrix chips, worksheet tabs.
 *   4. Base classes moved from inline styles (styles/ui-controls-legacy.css batch 5 section):
 *      every TSX-used class defined, call sites no longer restore original inline styles.
 */
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { allStylesheets } from "./css-contract";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(HERE, "..");
const PREFIX = ":root:root";

function read(rel: string): string {
  return readFileSync(join(FRONTEND, rel), "utf8");
}

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

type Rule = { selector: string; body: string; media: string | null };

/** Parse ordinary stylesheet rules;@media / @supports recurse (record outer conditions),@keyframes skip. */
function parseRules(css: string, media: string | null = null): Rule[] {
  const rules: Rule[] = [];
  let i = 0;
  while (i < css.length) {
    const open = css.indexOf("{", i);
    if (open === -1) break;
    const prelude = css.slice(i, open).trim();
    let depth = 0;
    let close = open;
    for (; close < css.length; close += 1) {
      if (css[close] === "{") depth += 1;
      else if (css[close] === "}") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    const body = css.slice(open + 1, close);
    if (/^@(media|supports)\b/.test(prelude)) rules.push(...parseRules(body, prelude));
    else if (!prelude.startsWith("@")) rules.push({ selector: prelude, body, media });
    i = close + 1;
  }
  return rules;
}

/** Split selector list on top-level commas (ignore commas in :is() / :where() parentheses). */
function splitSelectors(selector: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < selector.length; i += 1) {
    const ch = selector[i];
    if (ch === "(") depth += 1;
    else if (ch === ")") depth -= 1;
    else if (ch === "," && depth === 0) {
      parts.push(selector.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(selector.slice(start));
  return parts.map((part) => part.replace(/\s+/g, " ").trim()).filter(Boolean);
}

const norm = (text: string) => text.replace(/\s+/g, " ").trim();

/** CSS specificity [id, class, type]: :is() / :not() / :has() take highest argument, :where() zero, pseudo-elements type. */
function specificity(selector: string): [number, number, number] {
  const max = (list: string): [number, number, number] =>
    splitSelectors(list).map(specificity).reduce((a, b) => (cmpSpec(a, b) >= 0 ? a : b), [0, 0, 0] as [number, number, number]);
  let a = 0;
  let b = 0;
  let c = 0;
  let i = 0;
  const s = selector;
  const readIdent = () => {
    const m = /^[\w-]+/.exec(s.slice(i));
    i += m ? m[0].length : 0;
    return m ? m[0] : "";
  };
  const readParens = () => {
    let depth = 0;
    const start = i;
    for (; i < s.length; i += 1) {
      if (s[i] === "(") depth += 1;
      else if (s[i] === ")") {
        depth -= 1;
        if (depth === 0) {
          i += 1;
          return s.slice(start + 1, i - 1);
        }
      }
    }
    return s.slice(start + 1);
  };
  while (i < s.length) {
    const ch = s[i];
    if (ch === "#") {
      i += 1;
      readIdent();
      a += 1;
    } else if (ch === ".") {
      i += 1;
      readIdent();
      b += 1;
    } else if (ch === "[") {
      const end = s.indexOf("]", i);
      i = end === -1 ? s.length : end + 1;
      b += 1;
    } else if (ch === ":" && s[i + 1] === ":") {
      i += 2;
      readIdent();
      if (s[i] === "(") readParens();
      c += 1;
    } else if (ch === ":") {
      i += 1;
      const name = readIdent();
      if (s[i] === "(") {
        const args = readParens();
        if (name === "where") continue;
        if (["is", "not", "has", "matches"].includes(name)) {
          const [x, y, z] = max(args);
          a += x;
          b += y;
          c += z;
          continue;
        }
      }
      b += 1;
    } else if (/[\w-]/.test(ch)) {
      readIdent();
      c += 1;
    } else if (ch === "*") {
      i += 1;
    } else {
      i += 1;
    }
  }
  return [a, b, c];
}
function cmpSpec(x: [number, number, number], y: [number, number, number]): number {
  return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
}


/** Declaration map: property → value (normalize value whitespace). */
function decls(body: string): Record<string, string> {
  return Object.fromEntries(
    stripComments(body)
      .split(";")
      .map((d) => d.trim())
      .filter(Boolean)
      .map((d) => {
        const i = d.indexOf(":");
        return [d.slice(0, i).trim(), norm(d.slice(i + 1))];
      }),
  );
}

const CSS = read("styles/ui-refresh-surfaces.css");
const RULES = parseRules(stripComments(CSS));
const isTokenRoot = (sel: string) => /^:root:root(\[[^\]]+\])*$/.test(sel);
/** Rules whose selector list contains exactly `${PREFIX} ${sel}` (excluding @media); merge declarations. */
function exact(sel: string): Record<string, string> {
  const found = RULES.filter((r) => r.media === null && splitSelectors(r.selector).includes(`${PREFIX} ${norm(sel)}`));
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

/** Find rules whose selector list contains needle (substring after whitespace normalization), optionally constrain @media. */
function rulesWith(needle: string, media: RegExp | null = null): Rule[] {
  return RULES.filter((r) => norm(r.selector).includes(norm(needle)) && (media ? media.test(r.media ?? "") : r.media === null));
}
/** As above, merge static rules only (without state in needle, skip :hover / :focus / :active / :disabled state rules). */
function ruleWith(needle: string, media: RegExp | null = null): Record<string, string> {
  const stateful = /:(hover|focus|focus-visible|active|disabled)\b|\):is\(/;
  const found = rulesWith(needle, media).filter((r) => stateful.test(needle) || !stateful.test(r.selector));
  expect(found.length, needle).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("styles/ui-refresh-surfaces.css: every rule starts with the :root:root prefix", () => {
  it("解析出规则", () => {
    expect(RULES.length).toBeGreaterThan(60);
  });

  it("every selector starts with :root:root (inside @media too)", () => {
    const offenders = RULES.flatMap((rule) => splitSelectors(rule.selector)).filter((sel) => !sel.startsWith(PREFIX));
    expect(offenders).toEqual([]);
  });

  it("no literal colours outside the :root:root token blocks", () => {
    const literal = /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?)\(|(?<![\w-])(?:white|black)(?![\w-])/i;
    const offenders = RULES.filter((rule) => !splitSelectors(rule.selector).every(isTokenRoot)).flatMap((rule) =>
      stripComments(rule.body)
        .split(";")
        .map((d) => d.trim())
        .filter((d) => literal.test(d))
        .map((d) => `${splitSelectors(rule.selector)[0]} { ${d} }`),
    );
    expect(offenders).toEqual([]);
  });

  it("只改外观：不写 z-index、position（层级与挂载方式不动）", () => {
    const offenders = RULES.filter((r) => /(^|;)\s*(z-index|position)\s*:/.test(stripComments(r.body))).map((r) => norm(r.selector));
    expect(offenders).toEqual([]);
  });
});

describe("引入位置", () => {
  it("keeps the ordered authenticated styles starting with ui-refresh.css", () => {
    const layout = read("app/(app)/layout.tsx");
    const imports = Array.from(layout.matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    const expectedImports = ["../../styles/ui-refresh.css", "../../styles/ui-refresh-surfaces.css", "../../styles/ui-refresh-pages-settings.css", "../../styles/ui-refresh-pages-admin.css", "../../styles/ui-refresh-pages-main.css", "../../styles/ui-refresh-canvas.css", "../../styles/ui-refresh-workbench.css", "../../styles/ui-refresh-materials.css"];
    const start = imports.indexOf(expectedImports[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(imports.slice(start, start + expectedImports.length)).toEqual(expectedImports);
  });

  it("公共布局不引入", () => {
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-surfaces.css");
  });
});

describe("级联：后写的覆盖规则不能输给先写的家族规则", () => {
  /** Rule subject set: remove the :root:root prefix; entire :is(a, b) splits into a/b, otherwise entire selector. */
  const subjects = (sel: string): string[] => {
    const rest = norm(sel.slice(PREFIX.length));
    if (!rest.startsWith(":is(")) return [rest];
    let depth = 0;
    for (let k = 3; k < rest.length; k += 1) {
      if (rest[k] === "(") depth += 1;
      else if (rest[k] === ")" && --depth === 0) return k === rest.length - 1 ? splitSelectors(rest.slice(4, -1)).map(norm) : [rest];
    }
    return [rest];
  };
  /** State rules (hover/press/focus/disabled/checked/highlight) apply only in corresponding state, excluded from family-default override comparison. */
  const STATEFUL = /:(hover|active|focus|focus-visible|focus-within|disabled|checked)\b|aria-(disabled|checked|selected|current)|data-active|\.is-(active|on|selected)\b|\.selected\b/;
  /** Properties overlap if identical or one shorthand of other (margin vs margin-top, border vs border-top-color). */
  const overlaps = (x: string, y: string) => x === y || x.startsWith(`${y}-`) || y.startsWith(`${x}-`);
  /** Same subject/property: later rule specificity (:is() takes highest) must ≥ earlier, otherwise override ineffective. */
  function cascadeProblems(rules: Rule[]): string[] {
    const problems: string[] = [];
    rules.forEach((later, j) => {
      const laterProps = Object.keys(decls(later.body));
      for (const lsel of splitSelectors(later.selector)) {
        if (STATEFUL.test(lsel)) continue;
        const lsub = subjects(lsel);
        const lspec = specificity(lsel);
        rules.slice(0, j).forEach((earlier) => {
          const shared = Object.keys(decls(earlier.body)).filter((prop) => laterProps.some((lp) => overlaps(lp, prop)));
          if (!shared.length) return;
          for (const esel of splitSelectors(earlier.selector)) {
            if (STATEFUL.test(esel)) continue;
            if (!subjects(esel).some((x) => lsub.includes(x))) continue;
            const espec = specificity(esel);
            if (cmpSpec(lspec, espec) < 0) {
              problems.push(`${lsel} (${lspec.join(",")}) 覆盖不了 ${esel.slice(0, 80)}… (${espec.join(",")}) 的 ${shared.join(" / ")}`);
            }
          }
        });
      }
    });
    return problems;
  }

  it("权重计算：:is() 取最高项、:where() 为 0", () => {
    expect(specificity(":root:root .a")).toEqual([0, 3, 0]);
    expect(specificity(":root:root :is(.a, .b.c, .d .e)")).toEqual([0, 4, 0]);
    expect(specificity(":root:root :where(.a.b) span")).toEqual([0, 2, 1]);
    expect(specificity(".x button::before")).toEqual([0, 1, 2]);
  });

  it("本文件没有被先写规则压住的覆盖", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });

  it("能抓到「家族 :is() 里混进复合选择器、抬高权重压过窄屏覆盖」这一类", () => {
    const bad = parseRules(`${PREFIX} :is(.a, .b.c) { border-radius: 14px; }
@media (max-width: 640px) { ${PREFIX} .a { border-radius: 0; } }`);
    expect(cascadeProblems(bad)).toHaveLength(1);
    // Shorthand overriding longhand also counts (margin over margin-top).
    expect(cascadeProblems(parseRules(`${PREFIX} :is(.a, .b .c) { margin-top: 0; }
${PREFIX} .a { margin: 8px 0 4px; }`))).toHaveLength(1);
    const ok = parseRules(`${PREFIX} :is(.a, .b) { border-radius: 14px; }
${PREFIX} .b.c { border-radius: 14px; }
@media (max-width: 640px) { ${PREFIX} .a { border-radius: 0; } }`);
    expect(cascadeProblems(ok)).toEqual([]);
  });
});

describe("弹窗与面板（已定取舍 2；Component Spec「弹窗 · 确认框」）", () => {
  const PANELS = [
    ".am-dialog",
    ".am-fork-dialog",
    ".am-filter-sheet",
    ".rc-inspect-panel",
    ".discovery-picker",
    ".verification-selector",
    ".reference-parameter-panel",
    ".legacy-credits-modal",
    ".legacy-credits-quota-modal",
    ".legacy-share-panel",
    ".legacy-share-sheet",
    ".legacy-share-revoke",
    ".legacy-datarc-modal",
  ];
  const PANEL = { border: "0", "border-radius": "var(--r-4)", background: "var(--c-surface)", "box-shadow": "var(--shadow-overlay)", "font-family": "var(--font-control)" };

  it("实底 surface、无边框、圆角 r4、浮层阴影、系统字体", () => {
    const panel = RULES.find((r) => r.media === null && PANELS.every((p) => norm(r.selector).includes(p)));
    expect(panel, "面板家族规则").toBeDefined();
    const d = decls(panel!.body);
    expect(d).toMatchObject(PANEL);
    // Solid background: material only for menus and Toast.
    expect(d.background).not.toContain("material");
    // Two compound-selector families separate (putting in :is() raises whole-family specificity, overriding narrow-screen rules).
    expect(exact(".kb-upload.md")).toMatchObject(PANEL);
    expect(exact(".legacy-tools-create-panel .legacy-ctool-card")).toMatchObject(PANEL);
    const inFamily = splitSelectors(panel!.selector.slice(panel!.selector.indexOf(":is(") + 4, panel!.selector.lastIndexOf(")")));
    expect(inFamily.filter((x) => !/^\.[\w-]+$/.test(x))).toEqual([]);
  });

  it("窄屏：原来的底部面板顶部圆角 16（Narrow Spec），原来全屏的保持直角", () => {
    expect(CSS).toMatch(/--sheet-radius:\s*16px/);
    expect(ruleWith(".am-fork-dialog, .kb-upload.md", /max-width: 640px/)["border-radius"]).toBe("var(--sheet-radius) var(--sheet-radius) 0 0");
    expect(ruleWith(".am-filter-sheet", /max-width: 640px/)["border-radius"]).toBe("0");
    expect(ruleWith(".legacy-share-sheet")["border-radius"]).toBe("var(--sheet-radius) var(--sheet-radius) 0 0");
  });

  it("额度弹窗原来写死的最小宽在手机上不超出视口", () => {
    for (const [cls, w] of [["--edit", 420], ["--grant", 380], ["--reset", 480], ["--tier", 360]] as const) {
      expect(ruleWith(`.legacy-credits-modal${cls}`)["min-width"], cls).toBe(`min(${w}px, calc(100vw - 32px))`);
    }
  });

  it("遮罩统一 --c-scrim、去掉模糊", () => {
    const d = ruleWith(".am-dialog-backdrop");
    expect(d).toMatchObject({ background: "var(--c-scrim)", "backdrop-filter": "none", "-webkit-backdrop-filter": "none" });
    const scrim = rulesWith(".am-dialog-backdrop")[0].selector;
    for (const s of [".am-filter-sheet-backdrop", ".am-fork-scrim", ".artifact-viewer-modal-scrim", ".legacy-credits-modal-backdrop", ".legacy-credits-quota-backdrop", ".legacy-share-backdrop", ".legacy-datarc-modal-backdrop", ".legacy-tools-create-backdrop", ".v2-mobile-drawer-scrim", ".unified-sidebar-scrim"]) {
      expect(scrim, s).toContain(s);
    }
  });

  it("标题 15/650 系统字体（原衬线 16–32px），眉题 12px 次要色普通字，说明 13 次要色", () => {
    const title = ruleWith(".legacy-credits-quota-title");
    expect(title).toMatchObject({ "font-family": "var(--font-control)", "font-size": "var(--fs-dialog-title)", "font-weight": "650", "letter-spacing": "normal" });
    const titleSel = rulesWith(".legacy-credits-quota-title")[0].selector;
    for (const s of [".am-dialog h2", ".am-fork-dialog h2", ".am-filter-sheet h2", ".rc-inspect-title", ".discovery-head h2", ".verification-selector-head h2", ".kb-upload .md-hd h3", ".reference-parameter-panel h2", ".legacy-share-title", ".legacy-datarc-modal-title", ".legacy-ctool-title"]) {
      expect(titleSel, s).toContain(s);
    }
    expect(ruleWith(".am-dialog > span")).toMatchObject({ "font-size": "0.75rem", "text-transform": "none", "letter-spacing": "normal", color: "var(--c-ink-3)" });
    expect(ruleWith(".am-dialog > p")).toMatchObject({ "font-size": "var(--fs-md)", color: "var(--c-ink-2)", "line-height": "1.55" });
  });

  it("头部与底栏分隔线是发丝线，不用 --c-line", () => {
    expect(ruleWith(".rc-inspect-head")["border-bottom"]).toBe("var(--hairline) solid var(--c-separator)");
    expect(ruleWith(".kb-upload .md-ft")["border-top"]).toBe("var(--hairline) solid var(--c-separator)");
    const offenders = RULES.filter((r) => /var\(--c-line(?:-2)?\)/.test(r.body)).map((r) => norm(r.selector));
    expect(offenders).toEqual([]);
  });
});

describe("弹窗里的自写按钮：第一批的变体", () => {
  const FAMILY = [".am-dialog footer button", ".am-filter-sheet > footer button", ".artifact-viewer-modal .artifact-viewer-actions button", ".rc-inspect-panel .v2-chart-action-btn", ".reference-parameter-panel footer button", ".legacy-credits-quota-btn", ".legacy-share-btn", ".legacy-ctool-btn"];

  it("外形：控件高、左右 12、r2、13px/500 系统字体、无描边；默认是次按钮（fill-2）", () => {
    const base = RULES.find((r) => r.media === null && FAMILY.every((f) => norm(r.selector).includes(f)) && !r.selector.includes(":hover") && !r.selector.includes("):is("));
    expect(base).toBeDefined();
    expect(decls(base!.body)).toMatchObject({
      "min-height": "var(--control-h-md)",
      padding: "0 12px",
      border: "0",
      "border-radius": "var(--r-2)",
      "font-family": "var(--font-control)",
      "--ui-btn-bg": "var(--c-fill-2)",
      background: "var(--ui-btn-bg)",
    });
  });

  it("主按钮强调色；两处确认动作（停用智能体、停止访问）是实心红（取舍第 23 条）；变体规则权重高于外形规则", () => {
    const primary = RULES.find((r) => r.selector.includes(".am-action-primary") && r.selector.includes("):is("));
    expect(primary, "主按钮变体写成 :is(家族):is(变体)").toBeDefined();
    expect(decls(primary!.body)["--ui-btn-bg"]).toBe("var(--c-accent)");
    for (const v of [".am-filter-apply", ".reference-parameter-panel__apply", ".legacy-credits-quota-btn--primary", ".legacy-share-btn--primary", ".legacy-ctool-btn--primary"]) {
      expect(primary!.selector, v).toContain(v);
    }
    const danger = RULES.find((r) => r.selector.includes(".am-danger-confirm"));
    expect(danger).toBeDefined();
    expect(danger!.selector).toContain("):is(");
    expect(danger!.selector).toContain(".legacy-share-btn--danger");
    expect(decls(danger!.body)).toMatchObject({ "--ui-btn-bg": "var(--c-err)", "--ui-btn-fg": "var(--c-err-on)" });
  });

  it("按钮变量只取 token", () => {
    for (const r of RULES) {
      for (const [prop, value] of Object.entries(decls(r.body))) {
        if (!/^--ui-btn-(bg|bg-hover|bg-press|fg)$/.test(prop)) continue;
        expect(value === "transparent" || /^var\(--c-[\w-]+\)$/.test(value), `${norm(r.selector)} ${prop}: ${value}`).toBe(true);
      }
    }
  });

  it("禁用 40% 不透明、焦点 2px 外描边；触屏 44 高（控件高随 pointer: coarse）", () => {
    expect(ruleWith(".legacy-ctool-btn ):is(:disabled")).toMatchObject({ opacity: "0.4" });
    expect(ruleWith(".legacy-ctool-btn ):focus-visible")).toMatchObject({ outline: "2px solid var(--c-focus, var(--c-accent))", "outline-offset": "2px" });
    expect(ruleWith(".legacy-ctool-btn", /pointer: coarse/)).toMatchObject({ "border-radius": "var(--r-3)", "font-size": "0.9375rem" });
  });

  it("关闭按钮是控件高见方的图标按钮", () => {
    const d = ruleWith(".legacy-share-close");
    expect(d).toMatchObject({ width: "var(--control-h-md)", height: "var(--control-h-md)", background: "transparent", color: "var(--c-ink-3)", border: "0" });
    for (const s of [".am-filter-close", ".am-fork-dialog > header > button", ".rc-inspect-close", ".kb-upload .kb-x", ".legacy-ctool-close", ".v2-mobile-drawer-close"]) {
      expect(rulesWith(".legacy-share-close")[0].selector, s).toContain(s);
    }
  });
});

describe("导航抽屉：只改面板材质、圆角、头部与行（侧栏本体与导航结构属于 P2）", () => {
  it("手机会话抽屉与侧栏浮层：实底 surface、打开一侧 r4、浮层阴影", () => {
    for (const s of [".v2-mobile-drawer", ".unified-sidebar.is-overlay .unified-sidebar-expanded"]) {
      const d = RULES.filter((r) => splitSelectors(r.selector).includes(`${PREFIX} ${s}`)).map((r) => decls(r.body));
      expect(d.length, s).toBeGreaterThan(0);
      expect(Object.assign({}, ...d), s).toMatchObject({ background: "var(--c-surface)", "border-radius": "0 var(--r-4) var(--r-4) 0", "box-shadow": "var(--shadow-overlay)" });
    }
  });

  it("行与分组标题的规则都限定在两个抽屉里，不碰侧栏本体", () => {
    const rowRules = RULES.filter((r) => /unified-(work|drawer|sidebar-(nav|subnav|section))/.test(r.selector));
    // Row metrics and selection moved to styles/ui-refresh-chrome.css in P2 batch 3 (same rules for the
    // persistent sidebar and both drawers); only the drawer-specific font and heading rules remain here.
    expect(rowRules.length).toBeGreaterThan(1);
    for (const r of rowRules) {
      for (const sel of splitSelectors(r.selector)) {
        expect(/\.v2-mobile-drawer|\.unified-sidebar\.is-overlay/.test(sel), sel).toBe(true);
      }
    }
    expect(ruleWith(".unified-work-group h3")).toMatchObject({ "font-size": "0.6875rem", "font-weight": "600", "text-transform": "none", color: "var(--c-ink-3)" });
  });
});

describe("「@」引用补全列表（Menus Spec 第 4 节）", () => {
  it("控件层材质、圆角 12、内边距 6、浮层阴影 + 材质描边", () => {
    expect(exact(".composer-reference-menu")).toMatchObject({
      padding: "6px",
      "border-radius": "12px",
      background: "var(--ui-material-bg)",
      "backdrop-filter": "var(--ui-material-filter)",
      "box-shadow": "var(--shadow-overlay), inset 0 0 0 1px var(--material-edge)",
    });
  });

  it("行 32（触屏 44，--menu-item-h）、双行项最小 44、行首标记位 18、分组标题 11/600", () => {
    expect(ruleWith(".composer-reference-menu .app-floating-menu-item")["min-height"]).toBe("var(--menu-item-h)");
    expect(ruleWith(".composer-reference-menu__option:has(")["min-height"]).toBe("44px");
    expect(ruleWith(".composer-reference-menu__icon")).toMatchObject({ width: "18px", flex: "0 0 18px" });
    expect(ruleWith(".composer-reference-menu__heading")).toMatchObject({ "font-size": "0.6875rem", "font-weight": "600", color: "var(--c-ink-3)" });
  });

  it("高亮 = 强调色整行、反白；不可用项停留时不用强调色；悬停不另画底色", () => {
    expect(exact('.composer-reference-menu__option[data-active="true"]')).toMatchObject({ background: "var(--c-accent)", color: "var(--c-accent-ink)" });
    // Fill overlays material: active unavailable item text needs --c-ink-2 (--c-ink-3 below 4.5 in dark mode).
    expect(exact('.composer-reference-menu__option[aria-disabled="true"][data-active="true"]')).toMatchObject({ background: "var(--c-fill-2)", color: "var(--c-ink-2)" });
    expect(ruleWith('.app-floating-menu-item:hover:not([data-active="true"])').background).toBe("transparent");
  });

  it("发现 / 验证模型：悬停与选中行内辅助字提一档（accent-tint、row-hover 底上 --c-ink-4 不到 4.5）", () => {
    const lift = RULES.find((r) => r.selector.includes(".verification-selector-row.is-selected") && /--c-ink-4:\s*var\(--c-ink-3\)/.test(r.body));
    expect(lift).toBeDefined();
    for (const s of [".discovery-row:hover", ".discovery-row.selected", ".verification-selector-row:hover"]) expect(lift!.selector, s).toContain(s);
    // On model page’s own dark ink and create-tool gray card, raise helper text one level across panel.
    const panel = RULES.find((r) => r.selector.includes(".legacy-ctool-card") && /--c-ink-4:\s*var\(--c-ink-3\)/.test(r.body) && !r.selector.includes(":hover"));
    expect(panel).toBeDefined();
    for (const s of [".discovery-picker", ".verification-selector", ".legacy-ctool-card"]) expect(panel!.selector, s).toContain(s);
  });

  it("材质上的次级文字最浅 --c-ink-3，不用 --c-ink-4 与纯强调色字", () => {
    const onMaterial = RULES.filter((r) => r.selector.includes("composer-reference-menu"));
    expect(onMaterial.filter((r) => /var\(--c-ink-4\)/.test(r.body)).map((r) => norm(r.selector))).toEqual([]);
    expect(onMaterial.filter((r) => /(^|[;\s])color:\s*var\(--c-accent\)/.test(r.body)).map((r) => norm(r.selector))).toEqual([]);
  });

  it("composer 里的引用 chip 只改文字色与字重（强调色文字用 --c-accent-text）", () => {
    expect(ruleWith(".composer-reference-preview__reference")).toMatchObject({ color: "var(--c-accent-text)", "font-weight": "500" });
  });
});

describe("业务徽标与 chip（第二批徽标外形）", () => {
  const FAMILY = [".v2-agent-pill", ".pbadge", ":where(.pcard) .vpill", ".rc-badge", ".intent-badge", ".legacy-badge-sched-run", ".legacy-ctool-source-kind", ".legacy-preset-pill"];

  it("20 高、全圆角、11.5px/600、系统字体、无描边；色调来自 --ui-badge-*", () => {
    const shape = RULES.find((r) => r.media === null && FAMILY.every((f) => norm(r.selector).includes(f)) && r.selector.includes(":is("));
    expect(shape).toBeDefined();
    expect(decls(shape!.body)).toMatchObject({
      height: "auto",
      "min-height": "var(--ui-badge-h, 20px)",
      "border-radius": "999px",
      border: "0",
      "font-size": "var(--ui-badge-fs, 0.71875rem)",
      "font-weight": "600",
      "font-family": "var(--font-control)",
      "text-transform": "none",
      background: "var(--ui-badge-bg, var(--c-fill-2))",
    });
  });

  it("色调变量只取 token（描边色调为 0.5px 控件描边）", () => {
    let count = 0;
    for (const r of RULES) {
      for (const [prop, value] of Object.entries(decls(r.body))) {
        if (!/^--ui-badge-(bg|fg|ring)$/.test(prop)) continue;
        count += 1;
        const ok = prop === "--ui-badge-ring" ? value === "none" || /^inset 0 0 0 0\.5px var\(--c-[\w-]+\)$/.test(value) : value === "transparent" || /^var\(--c-[\w-]+\)$/.test(value);
        expect(ok, `${norm(r.selector)} ${prop}: ${value}`).toBe(true);
      }
    }
    expect(count).toBeGreaterThan(12);
  });

  it("浅底色调的文字向正文色混过（--c-*-ink / --c-accent-text），不用纯强调色", () => {
    for (const r of RULES.filter((rule) => /--ui-badge-bg:\s*var\(--c-(accent|pos|warn|err)-soft\)/.test(rule.body))) {
      expect(decls(r.body)["--ui-badge-fg"], norm(r.selector)).toMatch(/^var\(--c-(accent-text|pos-ink|warn-ink|err-ink)\)$/);
    }
  });

  it("状态映射：运行记录（进行中强调、成功、失败危险、等你回答待核对、其余中性）、置信度、校验结果", () => {
    const toneOf = (needle: string) => decls(RULES.find((r) => r.selector.includes(":where(") && norm(r.selector).includes(needle) && /--ui-badge-bg/.test(r.body))!.body)["--ui-badge-bg"];
    expect(toneOf(".legacy-badge-sched-run.is-running")).toBe("var(--c-accent-soft)");
    expect(toneOf(".legacy-badge-sched-run.is-success")).toBe("var(--c-pos-soft)");
    expect(toneOf(".legacy-badge-sched-run.is-failed")).toBe("var(--c-err-soft)");
    expect(toneOf(".legacy-badge-sched-run.is-awaiting")).toBe("var(--c-warn-soft)");
    expect(toneOf(".legacy-badge-sched-run.is-muted")).toBe("var(--c-fill-2)");
    expect(toneOf(".rc-badge.conf-high")).toBe("var(--c-pos-soft)");
    expect(toneOf(".rc-badge.amber")).toBe("var(--c-warn-soft)");
    expect(toneOf(".pcard .vpill.unmet")).toBe("var(--c-err-soft)");
    expect(toneOf(".v2-agent-pill.running")).toBe("var(--c-accent-soft)");
    // Approval page reuses intent badges as status (pending approval → modeling, rejected → causal): map original color families.
    expect(toneOf(".intent-badge.causal")).toBe("var(--c-err-soft)");
    expect(toneOf(".intent-badge.modeling")).toBe("var(--c-warn-soft)");
    expect(toneOf(".intent-badge.report")).toBe("var(--c-pos-soft)");
    expect(ruleWith(".legacy-badge-sched-run-pip").display).toBe("none");
  });

  it("share roster avatars: neutral fill with secondary text, accent for self (decision 22); per-name colours only in the base layer", () => {
    expect(exact(".legacy-share-avatar")).toMatchObject({ background: "var(--c-fill-3)", color: "var(--c-ink-2)" });
    expect(exact(".legacy-share-avatar.is-self")).toMatchObject({ background: "var(--c-accent)", color: "var(--c-accent-ink)" });
    const ui = read("components/share/share-ui.tsx");
    const avatar = ui.slice(ui.indexOf("export function ShAvatar"), ui.indexOf("export function ShGroupAvatar"));
    expect(avatar).toContain('"--sh-avatar-bg": color');
    expect(avatar).not.toMatch(/background:\s*color|color:\s*"#fff"/);
    expect(read("components/share/share-dialog.tsx")).toMatch(/<ShAvatar name=\{t\("ro_you"\)\}[^>]*\bself\b/);
  });

  it("矩阵：值 chip 20 高全圆角、计数徽标 11px（最小字号）、失败筛选 chip 26 高 r2", () => {
    expect(ruleWith(".mx-chipval, .mx-more")).toMatchObject({ height: "20px", "border-radius": "999px", border: "0", background: "var(--c-fill-2)" });
    expect(exact(".mx-cbadge .num")["font-size"]).toBe("0.6875rem");
    expect(ruleWith(".mx-fchip")).toMatchObject({ height: "26px", "border-radius": "var(--r-2)", color: "var(--c-err-ink)" });
  });

  it("工作表标签按下划线标签：13px、选中正文色 600 + 2px 强调色下划线、列表底发丝线", () => {
    expect(exact(".artifact-sheet-tabs")["border-bottom"]).toBe("var(--hairline) solid var(--c-separator)");
    expect(ruleWith(".artifact-sheet-tabs button.active")).toMatchObject({ "border-bottom-color": "var(--c-accent)", color: "var(--c-ink)", "font-weight": "600" });
  });
});

describe("个人外观确认框（CSS Module）", () => {
  const css = stripComments(read("components/settings/personal-appearance-settings.module.css"));
  const rules = parseRules(css).filter((r) => r.selector.includes(":global(:root:root)"));

  it("follows the dialog spec: solid r4, overlay shadow, --c-scrim backdrop; every selector starts with :global(:root:root), no literal colors", () => {
    expect(rules.length).toBeGreaterThan(5);
    for (const r of rules) {
      for (const sel of splitSelectors(r.selector)) expect(sel.startsWith(":global(:root:root)"), sel).toBe(true);
      expect(/#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?)\(/i.test(r.body), r.selector).toBe(false);
    }
    const dialog = Object.assign({}, ...rules.filter((r) => /\.confirmDialog$/.test(norm(r.selector))).map((r) => decls(r.body)));
    expect(dialog).toMatchObject({ "border-radius": "var(--r-4)", "box-shadow": "var(--shadow-overlay)", border: "0" });
    const backdrop = Object.assign({}, ...rules.filter((r) => /\.confirmBackdrop$/.test(norm(r.selector))).map((r) => decls(r.body)));
    expect(backdrop.background).toBe("var(--c-scrim)");
  });
});

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "node_modules" ? [] : tsxFiles(path);
    return name.endsWith(".tsx") && !name.endsWith(".test.tsx") ? [path] : [];
  });
}

describe("batch 5 base classes moved from inline styles", () => {
  const LEGACY_CSS = stripComments(read("styles/ui-controls-legacy.css"));
  const LEGACY_RULES = parseRules(LEGACY_CSS);
  const legacy = (selector: string) => {
    const rule = LEGACY_RULES.find((r) => splitSelectors(r.selector).includes(selector));
    expect(rule, selector).toBeDefined();
    return decls(rule!.body);
  };

  it("each run-record state base-layer class matches the call-site state map", () => {
    const src = read("components/settings/scheduled.tsx");
    for (const tone of ["is-running", "is-success", "is-failed", "is-muted", "is-awaiting"]) {
      expect(src, tone).toContain(`"${tone}"`);
      expect(LEGACY_RULES.some((r) => splitSelectors(r.selector).includes(`.legacy-badge-sched-run.${tone}`)), tone).toBe(true);
    }
  });

  it("every legacy- class used in TSX (constants and template strings included) is defined in a stylesheet", () => {
    const defined = new Set(Array.from(allStylesheets().matchAll(/\.(legacy-[\w-]+)/g), (m) => m[1]));
    const used = new Set<string>();
    for (const file of [...tsxFiles(join(FRONTEND, "components")), ...tsxFiles(join(FRONTEND, "app"))]) {
      const src = readFileSync(file, "utf8");
      for (const lit of src.matchAll(/(["'`])((?:(?!\1)[^\\\n]|\\.)*?)\1/g)) {
        for (const m of lit[2].matchAll(/(?:^|\s)(legacy-[a-z0-9]+(?:-{1,2}[a-z0-9]+)*)(?=$|\s|\$)/g)) used.add(m[1]);
      }
    }
    // Non-class string: Results route type route.type === "legacy-data".
    used.delete("legacy-data");
    for (const name of ["legacy-share-btn", "legacy-ctool-btn--primary", "legacy-badge-sched-run", "legacy-credits-modal", "legacy-datarc-modal", "legacy-preset-pill"]) {
      expect(used.has(name), name).toBe(true);
    }
    expect([...used].filter((name) => !defined.has(name))).toEqual([]);
  });

  it("call sites use the base-layer classes and the inline styles stay removed", () => {
    const expectations: [string, string[], RegExp[]][] = [
      ["components/admin/credits/edit-user-modal.tsx", ["legacy-credits-modal legacy-credits-modal--edit", "legacy-credits-modal-backdrop"], [/modalBox|modalBackdrop|font-v2-serif/]],
      ["components/admin/credits/grant-credit-modal.tsx", ["legacy-credits-modal legacy-credits-modal--grant"], [/backdropStyle=|panelStyle=/]],
      ["components/admin/credits/period-reset-modal.tsx", ["legacy-credits-modal legacy-credits-modal--reset", "legacy-credits-mode-tab"], [/backdropStyle=|panelStyle=/]],
      ["components/admin/credits/team-quota-modal.tsx", ["legacy-credits-quota-modal", "legacy-credits-quota-btn legacy-credits-quota-btn--primary"], [/buttonStyle|backdropStyle=|panelStyle=/]],
      ["components/admin/credits/tier-defaults.tsx", ["legacy-credits-modal legacy-credits-modal--tier", "ui-input legacy-input-tier-default"], [/backdropStyle=|panelStyle=/]],
      ["components/share/share-dialog.tsx", ["legacy-share-panel", "legacy-share-sheet", "legacy-share-revoke"], [/function backdropStyle|borderRadius: 18,/]],
      ["components/share/share-ui.tsx", ["legacy-share-btn legacy-share-btn--", "legacy-share-seg-item"], [/const kinds|boxShadow:/]],
      ["components/settings/datarc-connections.tsx", ["legacy-datarc-modal", "ui-input legacy-input-datarc"], [/backdropStyle=|panelStyle=/]],
      ["components/settings/tools/tools-manager.tsx", ["legacy-tools-create-backdrop", "legacy-tools-create-panel"], [/panelStyle=\{\{ width: "100%", maxWidth: 560 \}\}/]],
      ["components/canvas/sections/create-tool-card.tsx", ["legacy-ctool-card", "legacy-ctool-btn legacy-ctool-btn--primary", "legacy-ctool-source-kind"], [/function btnPrimary|function btnGhost|const wrap:/]],
      ["components/canvas/sections/preset-directory.tsx", ["legacy-preset-pill legacy-preset-kind", "legacy-preset-pill legacy-preset-auth is-oauth"], [/borderRadius: 6, padding: "2px 7px 2px 5px"/]],
      ["components/settings/scheduled.tsx", ["legacy-badge-sched-run ${toneClass}", "legacy-badge-sched-run-pip"], [/S\.runBadge|S\.pip/]],
      ["components/knowledge/kb-upload-modal.tsx", ["ui-input legacy-input-kb-upload-template"], [/borderRadius: 6,\s*background: "var\(--c-surface\)"/]],
    ];
    for (const [file, present, absent] of expectations) {
      const src = read(file);
      for (const p of present) expect(src, `${file}: ${p}`).toContain(p);
      for (const a of absent) expect(a.test(src), `${file}: ${a}`).toBe(false);
    }
  });

  it("moved inputs carry .ui-input over their base-layer class", () => {
    for (const cls of ["legacy-input-tier-default", "legacy-input-datarc", "legacy-input-kb-upload-template"]) {
      const hits = [...tsxFiles(join(FRONTEND, "components"))].flatMap((f) => Array.from(readFileSync(f, "utf8").matchAll(new RegExp(`className="([^"]*\\b${cls}\\b[^"]*)"`, "g")), (m) => m[1]));
      expect(hits.length, cls).toBeGreaterThan(0);
      for (const h of hits) expect(h.split(" "), cls).toContain("ui-input");
    }
  });
});
