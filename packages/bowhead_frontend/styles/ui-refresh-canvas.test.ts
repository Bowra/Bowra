/**
 * Chat-workbench/report-canvas batch 3 (canvas appearance) style-source contracts.
 *
 * jsdom does not cascade; enforce source facts here; real-browser screenshots and element-wise main/branch comparisons validate pixels.
 *   1. Interface styles (styles/ui-refresh-canvas.css) start with the :root:root prefix, no literal colors/z-index, --font-control for controls;
 *      blur only floating functional layers (selection toolbar, source popover, chapter menu) via --ui-material-filter; authenticated layout only,
 *      after every non-refresh stylesheet—export screenshots under (public) never load it;
 *   2. Report Canvas Chrome Spec: toolbar 44 plus scroll padding, selection toolbar 34, rewrite bar 36, chapter buttons 28/drag handle 20×28,
 *      source popover 320, both drawers min(420, 100vw − 48), canvas/chat badges share appearance (restricted outline +
 * strikethrough), hairline chart frames;
 *   3. No shared-target/property conflicts with batch 2 workbench styles (subject-class intersection + same property);
 * only refresh styles read `data-source-denied`
 *      (plugin emits it); all backdrop-filter declarations include -webkit- prefix (WebKit requires it).
 */
import { describe, expect, it } from "vitest";

import { readdirSync } from "node:fs";
import { join } from "node:path";

import {
  cascadeProblems,
  cmpSpec,
  decls,
  FRONTEND,
  isTokenRoot,
  LITERAL_COLOR,
  norm,
  parseRules,
  PREFIX,
  read,
  type Rule,
  specificity,
  splitSelectors,
  stripComments,
} from "./css-contract";

const CSS = stripComments(read("styles/ui-refresh-canvas.css"));
const RULES = parseRules(CSS);
const WORKBENCH = parseRules(stripComments(read("styles/ui-refresh-workbench.css")));

/** Combined prefixed-selector declarations (possibly inside :is()); undefined media includes only rules outside @media. */
function rule(sel: string, media?: RegExp): Record<string, string> {
  const found = RULES.filter((r) => {
    if (media ? !media.test(r.media ?? "") : r.media !== null) return false;
    return splitSelectors(r.selector).some((full) => {
      const rest = norm(full.slice(PREFIX.length));
      if (rest === norm(sel)) return true;
      const m = /^:is\(([\s\S]*)\)$/.exec(rest);
      return m ? splitSelectors(m[1]).map(norm).includes(norm(sel)) : false;
    });
  });
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

/** Classes of the selector subject (last compound selector), after removing the :root:root prefix and expanding top-level :is(). */
function subjectClasses(selector: string): string[] {
  const rest = norm(selector.startsWith(PREFIX) ? selector.slice(PREFIX.length) : selector);
  const alternatives = /^:is\(([\s\S]*)\)$/.test(rest) ? splitSelectors(rest.slice(4, -1)) : [rest];
  return alternatives.flatMap((alt) => {
    let depth = 0;
    let start = 0;
    for (let i = 0; i < alt.length; i += 1) {
      const ch = alt[i];
      if (ch === "(") depth += 1;
      else if (ch === ")") depth -= 1;
      else if (depth === 0 && /[\s>+~]/.test(ch)) start = i + 1;
    }
    const compound = alt.slice(start).replace(/:(not|has)\([^)]*\)/g, "");
    return Array.from(compound.matchAll(/\.([\w-]+)/g), (m) => m[1]).filter((c) => !/^is-/.test(c));
  });
}

/** Rule pairs across stylesheets guaranteed to target the same element and write the same property: one subject's class set contains all classes of the other
 *  (elements matching the larger set necessarily match the smaller). Distinct families sharing only modifiers like .ghost / .on / .pos do not count.
 *  Higher specificity, or later order when tied, overrides the other—each batch should own its targets, so either direction is a conflict. */
function sharedTargetConflicts(earlier: Rule[], later: Rule[]): string[] {
  const overlaps = (x: string, y: string) => x === y || x.startsWith(`${y}-`) || y.startsWith(`${x}-`);
  const out: string[] = [];
  for (const e of earlier) {
    const eDecls = Object.keys(decls(e.body));
    for (const esel of splitSelectors(e.selector)) {
      const eClasses = new Set(subjectClasses(esel));
      if (!eClasses.size) continue;
      const subset = (a: Set<string>, b: Set<string>) => a.size > 0 && [...a].every((c) => b.has(c));
      for (const l of later) {
        if (l.media !== e.media) continue;
        const shared = Object.keys(decls(l.body)).filter((p) => eDecls.some((q) => overlaps(p, q)));
        if (!shared.length) continue;
        for (const lsel of splitSelectors(l.selector)) {
          const lClasses = new Set(subjectClasses(lsel));
          if (!subset(lClasses, eClasses) && !subset(eClasses, lClasses)) continue;
          const common = [...lClasses].filter((c) => eClasses.has(c));
          const winner = cmpSpec(specificity(lsel), specificity(esel)) >= 0 ? "后者压住前者" : "前者压住后者";
          out.push(`${esel}  ×  ${lsel}（.${common.join(" .")}：${shared.join(", ")}；${winner}）`);
        }
      }
    }
  }
  return out;
}

/** Floating functional layers: only these may use materials (blur). */
const FLOATING = /^:root:root \.(rc-sel-toolbar|rc-prov|rc-menu-pop)$/;

describe("styles/ui-refresh-canvas.css: every rule starts with the :root:root prefix", () => {
  it("every selector starts with :root:root (inside @media too)", () => {
    expect(RULES.length).toBeGreaterThan(80);
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("不写字面色值；不用 --c-line；不写 z-index", () => {
    const literal = RULES.filter((r) => !splitSelectors(r.selector).every(isTokenRoot)).flatMap((r) =>
      r.body.split(";").map((x) => x.trim()).filter((x) => LITERAL_COLOR.test(x)),
    );
    expect(literal).toEqual([]);
    expect(RULES.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => r.selector)).toEqual([]);
    expect(RULES.filter((r) => "z-index" in decls(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("模糊只给浮动功能层，且只经 --ui-material-filter（减少透明度 / 高对比度 / 不支持时退回实底）", () => {
    const blurred = RULES.filter((r) => /backdrop-filter|blur\(/.test(r.body));
    expect(blurred.map((r) => norm(r.selector)).sort()).toEqual(
      [`${PREFIX} .rc-menu-pop`, `${PREFIX} .rc-prov`, `${PREFIX} .rc-sel-toolbar`].sort(),
    );
    for (const r of blurred) {
      expect(FLOATING.test(norm(r.selector)), r.selector).toBe(true);
      expect(decls(r.body)["backdrop-filter"]).toBe("var(--ui-material-filter)");
      expect(r.body).not.toMatch(/blur\(/);
    }
  });

  it("controls use system fonts while KPI labels and table counts inherit content fonts", () => {
    const others = RULES.flatMap((r) => {
      const family = decls(r.body)["font-family"];
      if (!family || family === "var(--font-control)") return [];
      if (family === "inherit" && /\.v2-kpi-cell |\.v2-chart-table-footer\b/.test(r.selector)) return [];
      // Preformatted source excerpts (raw stdout fallback) keep line alignment with the mono stack.
      if (family === "var(--font-v2-mono, ui-monospace, monospace)" && /\.rc-prov-excerpt\b/.test(r.selector)) return [];
      return [`${r.selector} → ${family}`];
    });
    expect(others).toEqual([]);
  });

  it("焦点环都带强调色回退：var(--c-focus, var(--c-accent))", () => {
    const bare = RULES.flatMap((r) => (r.body.match(/var\(--c-focus[^,)]*\)/g) ?? []).map((m) => `${r.selector} → ${m}`));
    expect(bare).toEqual([]);
    expect(RULES.filter((r) => /:focus-visible/.test(r.selector)).length).toBeGreaterThan(15);
  });

  it("没有被本文件先写规则压住的覆盖", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });

  it("与第 2 批工作台样式不争同一对象：一定命中同一元素（class 集合互相包含）且写同一属性的规则一条都没有（不看选择器是否逐字相同）", () => {
    // This sheet imports first, workbench later: intersection in either direction means competing batches on one element, decided by specificity/order.
    expect(sharedTargetConflicts(RULES, WORKBENCH)).toEqual([]);
  });

  it("写了 backdrop-filter 的规则都同时写 -webkit-backdrop-filter，且两者取值相同", () => {
    const missing = RULES.flatMap((r) => {
      const d = decls(r.body);
      if (!("backdrop-filter" in d)) return [];
      return d["-webkit-backdrop-filter"] === d["backdrop-filter"] ? [] : [r.selector];
    });
    expect(missing).toEqual([]);
  });

  it("only the refreshed stylesheets read data-source-denied (the base stylesheets also load on the export page)", () => {
    const sheets = [
      "app/globals.css",
      ...readdirSync(join(FRONTEND, "styles"))
        .filter((name) => name.endsWith(".css") && !name.startsWith("ui-refresh"))
        .map((name) => `styles/${name}`),
      ...readdirSync(join(FRONTEND, "components/canvas"))
        .filter((name) => name.endsWith(".css"))
        .map((name) => `components/canvas/${name}`),
    ];
    expect(sheets).toContain("styles/report-canvas.css");
    expect(sheets.filter((path) => stripComments(read(path)).includes("data-source-denied"))).toEqual([]);
    expect(RULES.some((r) => r.selector.includes("[data-source-denied]"))).toBe(true);
  });

  it("只由认证布局引入，排在全部非刷新样式表（含 report-canvas.css、ui-controls-legacy.css）之后；公共布局（导出截图页）不引入", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    const at = imports.indexOf("../../styles/ui-refresh-canvas.css");
    expect(at).toBeGreaterThan(-1);
    const lastPlain = Math.max(...imports.map((p, i) => (/ui-refresh/.test(p) ? -1 : i)));
    expect(at).toBeGreaterThan(lastPlain);
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-canvas.css");
  });
});

describe("吸顶工具条与成果区头部", () => {
  it("工具条 44 高、左 12 右 8、发丝底线；报告滚动区留白按 44 重算为 60", () => {
    expect(rule(".results-report-object-toolbar")).toMatchObject({
      "min-height": "44px",
      padding: "0 8px 0 12px",
      "border-bottom": "var(--hairline) solid var(--c-separator)",
    });
    expect(rule(".v2-ws-right-body.results-body--report")).toEqual({ "scroll-padding-top": "60px" });
  });

  it("三组之间各一条 0.5 × 18 的分隔（编辑组之后、导出与图标按钮之间）", () => {
    for (const sel of [
      ".results-report-object-toolbar .results-report-edit-controls:not(:empty)",
      ".results-report-object-toolbar .ws-menu-wrap:has(.ws-export) + .ws-menu-wrap",
    ]) {
      expect(rule(sel).background, sel).toContain("var(--hairline) 18px no-repeat");
    }
  });

  it("成果区头部 44；保存状态 12 次要色系统字", () => {
    expect(rule(".v2-ws-right-head")).toMatchObject({ height: "44px" });
    expect(rule(".legacy-edit-bar-status")).toMatchObject({ color: "var(--c-ink-3)", "font-size": "var(--fs-sm)" });
  });
});

describe("编辑与改写", () => {
  it("引导条 fill1 底；冲突条警示浅底、不再有左色条；两条上的按钮都是次按钮（不用红色）", () => {
    expect(rule(".rc-guide")).toMatchObject({ background: "var(--c-fill-1)", border: "0" });
    expect(rule(".rc-conflict-bar")).toMatchObject({ background: "var(--c-warn-soft)", border: "0" });
    expect(rule(".rc-conflict-bar .rc-pill-btn")).toMatchObject({ background: "var(--c-fill-2)", color: "var(--c-ink)" });
  });

  it("选区工具栏 34 高（按钮 28）、圆角 10；只有「改写 / 发送」是强调色文字，开关类选中用中性底", () => {
    expect(rule(".rc-sel-toolbar")).toMatchObject({ height: "34px", "border-radius": "var(--r-3)" });
    expect(rule(".rc-sel-btn")).toMatchObject({ height: "28px", "min-width": "28px" });
    expect(rule(".rc-sel-btn.active")).toMatchObject({ background: "var(--c-fill-2)" });
    expect(rule(".rc-sel-btn.active:not([aria-pressed]):not([aria-haspopup])")).toMatchObject({ color: "var(--c-accent-text)" });
  });

  it("Stop on the rewriting bar and Retry / Close on the failed bar are 28 (over the base-layer legacy-rc-pill--compact 24; no inline height)", () => {
    const legacy = parseRules(stripComments(read("styles/report-canvas.css"))).find((r) => norm(r.selector) === ".rc-pill-btn.legacy-rc-pill--compact");
    expect(legacy && decls(legacy.body)).toEqual({ height: "24px" });
    const mine = RULES.find((r) => norm(r.selector) === `${PREFIX} :is(.rc-result-bar, .rc-regen-chip) .rc-pill-btn`);
    expect(decls(mine!.body).height).toBe("28px");
    expect(cmpSpec(specificity(mine!.selector), specificity(legacy!.selector))).toBeGreaterThan(0);
    const bar = read("components/canvas/ai-rewrite-diff-bar.tsx");
    expect(bar).not.toMatch(/height:\s*24/);
    // Batch 4a failed-state Retry/Close are both .rc-pill-btn in .rc-result-bar, both 28.
    expect(bar).toMatch(/className="rc-pill-btn ghost" data-testid="ai-rewrite-retry"/);
  });

  it("改写输入条与结果条 36 起、圆角 10；「接受」唯一实心主操作，其余次按钮，都不再全圆角", () => {
    expect(rule(".rc-ai-bar")).toMatchObject({ "min-height": "36px", "border-radius": "var(--r-3)" });
    expect(rule(".rc-result-bar")).toMatchObject({ "min-height": "36px", "border-radius": "var(--r-3)" });
    expect(rule(".rc-ai-send")).toMatchObject({ width: "28px", height: "28px", "border-radius": "999px" });
    expect(rule(":is(.rc-result-bar, .rc-regen-chip) .rc-pill-btn")).toMatchObject({ height: "28px", "border-radius": "var(--r-2)" });
    expect(rule(".rc-result-bar .rc-pill-btn.accept")).toMatchObject({ background: "var(--c-accent)", color: "var(--c-accent-ink)" });
    expect(rule(":is(.rc-result-bar, .rc-regen-chip) .rc-pill-btn.ghost")).toMatchObject({ background: "var(--c-fill-2)" });
  });

  it("章节操作条：按钮 28、实底 + 浮层阴影（不用材质）、上沿 −14；拖拽柄 20×28 在左 −30；悬停章节 fill1", () => {
    expect(rule(".rc-actions")).toMatchObject({ top: "-14px", background: "var(--c-surface)", "box-shadow": "var(--shadow-overlay)" });
    expect(rule(".rc-actions")).not.toHaveProperty("backdrop-filter");
    expect(rule(".rc-action-btn")).toMatchObject({ height: "28px", "min-width": "28px" });
    expect(rule(".rc-handle")).toMatchObject({ left: "-30px", width: "20px", height: "28px" });
    const hover = RULES.find((r) => /data-chrome-kind="narrative"/.test(r.selector));
    expect(hover && decls(hover.body)).toEqual({ background: "var(--c-fill-1)", "box-shadow": "none" });
  });

  it("章节菜单按菜单规格外观：宽 ≥ 220、圆角 12、行 32、悬停中性填充（不用强调浅底）", () => {
    expect(rule(".rc-menu-pop")).toMatchObject({ "min-width": "220px", "border-radius": "12px", padding: "6px" });
    expect(rule(".rc-mi")).toMatchObject({ height: "32px" });
    expect(rule(".rc-mi:hover")).toMatchObject({ background: "var(--c-fill-1)" });
  });
});

describe("角标与来源", () => {
  it("画布角标与对话角标同一套外观：系统字等宽数字、填充底；受限 / 失效改透明底 + 发丝描边 + 删除线", () => {
    const chat = WORKBENCH.find((r) => norm(r.selector) === `${PREFIX} .legacy-cite-chip.is-denied`);
    const chatDenied = decls(chat!.body);
    const chip = rule(".canvas-report-article .citation-num--numbered");
    expect(chip).toMatchObject({ "font-family": "var(--font-control)", "font-variant-numeric": "tabular-nums", background: "var(--c-fill-2)" });
    const denied = rule(".canvas-report-article .citation-num--numbered[data-source-denied]");
    expect(denied.background).toBe(chatDenied.background);
    expect(denied["box-shadow"]).toBe(chatDenied["box-shadow"]);
    expect(denied["text-decoration"]).toBe(chatDenied["text-decoration"]);
    expect(denied.color).toBe(`${chatDenied.color} !important`);
  });

  it("the source popover is 320 wide with radius 12; the excerpt is a fill-1 block without a left bar, 12.5/1.55", () => {
    expect(rule(".rc-prov")).toMatchObject({ width: "320px", "border-radius": "12px", background: "var(--ui-material-bg)" });
    expect(rule(".rc-prov-quote")).toMatchObject({ "border-left": "0", background: "var(--c-fill-1)", "font-size": "0.78125rem", "line-height": "1.55" });
  });

});

describe("图表、表格与 KPI", () => {
  it("图表 / 洞察卡 / 表格外框换发丝线（只在 .canvas-report-article 里）；洞察卡里的图表不描第二层边", () => {
    expect(rule(".canvas-report-article :is(.v2-chart-block, .v2-insight-card, .v2-dt-wrap)")).toMatchObject({
      border: "0",
      "box-shadow": "0 0 0 var(--hairline) var(--c-separator)",
    });
    expect(rule(".canvas-report-article .v2-insight-card :is(.v2-chart-block, .v2-dt-wrap)")).toEqual({ "box-shadow": "none" });
  });

  it("图表头按钮系统字 28 高，不描边；「⋯」28 见方（触屏 44）——Chart Embed Spec 起不再限于报告，规则搬到 globals.css", () => {
    // Moved unscoped so chat, embeds and reports share one treatment; a report-only copy here would drift from it.
    expect(RULES.filter((r) => /v2-chart-action-btn/.test(r.selector))).toEqual([]);
    const globals = parseRules(stripComments(read("app/globals.css")));
    const base = (sel: string, media: RegExp | null = null) => Object.assign(
      {},
      ...globals
        .filter((r) => (media ? media.test(r.media ?? "") : r.media === null) && splitSelectors(r.selector).map(norm).includes(norm(sel)))
        .map((r) => decls(r.body)),
    );
    expect(base(".v2-chart-action-btn")).toMatchObject({ "min-height": "28px", border: "0", "font-weight": "500" });
    expect(base(".v2-chart-action-btn")["font-family"]).toContain("var(--font-control");
    expect(base(".v2-chart-action-btn")["font-family"]).not.toContain("mono");
    expect(base(".v2-chart-actions.is-quiet > .v2-chart-action-btn.is-more")).toMatchObject({ width: "28px" });
    expect(base(".v2-chart-actions.is-quiet > .v2-chart-action-btn.is-more", /pointer: coarse/)).toMatchObject({ width: "44px" });
    expect(RULES.filter((r) => /v2-chart-(canvas|kind|title|subtitle|summary)|v2-chart-actions-pop/.test(r.selector))).toEqual([]);
  });

  it("KPI 数值层级不动（不写 .val）；标签与变化行 12、不大写；上升用语义正色", () => {
    expect(RULES.filter((r) => /\.v2-kpi-cell \.val/.test(r.selector))).toEqual([]);
    expect(rule(".canvas-report-article .v2-kpi-cell .lbl")).toMatchObject({ "font-size": "var(--fs-sm)", "text-transform": "none" });
    expect(rule(".canvas-report-article .v2-kpi-cell .sub.pos")).toEqual({ color: "var(--c-pos-ink)" });
  });
});

describe("第 4b 批：画布行为", () => {
  it("工具条状态组占住中间；「停止」次按钮 28；减少动态效果时转圈换成静止圆点（系统与应用内两种）", () => {
    expect(rule(".results-report-object-toolbar > .results-report-status-group")).toMatchObject({ flex: "1 1 auto", "min-width": "0" });
    expect(rule(".rc-toolbar-stream-stop")).toMatchObject({ "min-height": "28px", background: "var(--c-fill-2)" });
    expect(rule(".rc-toolbar-stream-spinner", /prefers-reduced-motion: reduce/)).toMatchObject({ animation: "none", background: "var(--c-accent)" });
    expect(RULES.some((r) => r.selector.includes('[data-theme-motion="reduced"] .rc-toolbar-stream-spinner'))).toBe(true);
  });

  it("写作中已写完的正文不变暗（只不接收指针）；AI 改写中仍 0.65；正在写的章节 2px 强调色左内描边", () => {
    const streaming = rule('.canvas-report-article[data-streaming="on"]');
    expect(streaming).toEqual({ "pointer-events": "none" });
    expect(rule('.canvas-report-article[data-ai-rewriting="on"]')).toEqual({ opacity: "0.65", "pointer-events": "none" });
    expect(rule(".sp-block--writing")).toMatchObject({ "box-shadow": "inset 2px 0 0 var(--c-accent)" });
    expect(rule(".sp-skeleton > span")).toMatchObject({ height: "12px", background: "var(--c-fill-2)" });
  });

  it("窄屏来源卡是底部面板时不再固定 320 宽", () => {
    expect(rule(".rc-prov.rc-prov--sheet")).toMatchObject({ width: "auto" });
  });
});

describe("第 4b 批余项", () => {
  it("写作暂停用警示色、静止的暂停标记（没有动画）；中断说明用错误色，「刷新报告」次按钮 28", () => {
    expect(rule(".rc-toolbar-stream.is-paused")).toEqual({ color: "var(--c-warn-ink)" });
    const paused = rule(".rc-toolbar-stream-paused");
    expect(paused.animation).toBeUndefined();
    expect(paused.background).toContain("currentColor");
    expect(rule(".rc-toolbar-outcome.is-error")).toEqual({ color: "var(--c-err)" });
    expect(rule(".rc-toolbar-outcome-action")).toMatchObject({ "min-height": "28px", background: "var(--c-fill-2)" });
  });

  it("写作中导出不可用：原因是工具条第二行（占满一行、12 次要色），导出按钮 40%", () => {
    expect(rule(".results-report-object-toolbar:has(> .results-report-writing-hint)")).toMatchObject({ "flex-wrap": "wrap" });
    expect(rule(".results-report-writing-hint")).toMatchObject({ flex: "1 0 100%", color: "var(--c-ink-3)", "font-size": "var(--fs-sm)" });
    expect(rule(".results-report-object-toolbar .ws-export:disabled")).toMatchObject({ opacity: "0.4" });
  });

  it("手机阅读合并工具条：藏对象类型小标与成果区头部；返回 44；标题占剩余宽度、单行省略（原手机头部藏标题，这里显示）", () => {
    expect(rule('.results-report-object-toolbar[data-phone-reading="true"] > span:first-child')).toEqual({ display: "none" });
    expect(rule('.v2-ws-right[data-phone-reading-bar="true"] > .v2-ws-right-head')).toEqual({ display: "none" });
    expect(rule(".results-report-phone-back")).toMatchObject({ "min-width": "44px", "min-height": "44px" });
    expect(rule(".results-report-object-toolbar .results-report-phone-title")).toMatchObject({
      display: "block",
      "min-width": "0",
      "white-space": "nowrap",
      "text-overflow": "ellipsis",
    });
    expect(rule('.results-report-object-toolbar[data-phone-reading="true"] > .results-report-status-group:has(.rc-toolbar-outcome)')).toMatchObject({ flex: "1 0 100%" });
  });

  it("手机合并条在触屏下分享、更多与状态徽标的可点区域不小于 44", () => {
    expect(
      rule('.results-report-object-toolbar[data-phone-reading="true"] :is(.results-report-share, .ws-menu-wrap > .v2-icon-btn, .rc-status-trigger)', /pointer: coarse/),
    ).toMatchObject({ "min-width": "44px", "min-height": "44px" });
  });

  it("「已停止 / 连接中断」说明整句可见：换行而不截断", () => {
    const text = rule(".rc-toolbar-outcome-text");
    expect(text["white-space"]).toBe("normal");
    expect(text["text-overflow"]).toBeUndefined();
  });

});
