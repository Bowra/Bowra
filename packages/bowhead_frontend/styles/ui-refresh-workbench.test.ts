/**
 * Chat-workbench/report-canvas batch 2 (workbench appearance) style-source contracts.
 *
 * jsdom does not cascade; enforce source facts here; real-browser screenshots and element-by-element main/branch comparisons validate pixels.
 *   1. Interface styles (styles/ui-refresh-workbench.css) start with the :root:root prefix, no literal colors/z-index/blur,
 *      hairline dividers, --font-control for controls, imported last by authenticated layout;
 *   2. Workbench Spec values: shared state frame for three components, running band retains three-row structure with
 * appearance-only changes (PRD A3), disclosure 40, step row 30, agent head 32, message actions 28,
 *      restricted badges use outline + strikethrough, card radius 12 + pending-warning outline, neutral Stop background,
 * divider width 9, recovery bar 40.
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

const CSS = stripComments(read("styles/ui-refresh-workbench.css"));
const RULES = parseRules(CSS);

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

describe("styles/ui-refresh-workbench.css: every rule starts with the :root:root prefix", () => {
  it("every selector starts with :root:root (inside @media too)", () => {
    expect(RULES.length).toBeGreaterThan(50);
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("不写字面色值；不用 --c-line；不写 z-index；没有模糊（模糊只给浮动的功能层）", () => {
    const literal = RULES.filter((r) => !splitSelectors(r.selector).every(isTokenRoot)).flatMap((r) =>
      r.body.split(";").map((x) => x.trim()).filter((x) => LITERAL_COLOR.test(x)),
    );
    expect(literal).toEqual([]);
    expect(RULES.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => r.selector)).toEqual([]);
    expect(RULES.filter((r) => "z-index" in decls(r.body)).map((r) => r.selector)).toEqual([]);
    expect(RULES.filter((r) => /backdrop-filter|blur\(/.test(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("控件层只用系统控件字体（个人外观的「字体」只作用于内容层）：写了 font-family 的都是 --font-control", () => {
    const others = RULES.flatMap((r) => {
      const family = decls(r.body)["font-family"];
      return family && family !== "var(--font-control)" ? [`${r.selector} → ${family}`] : [];
    });
    expect(others).toEqual([]);
  });

  it("焦点环都带强调色回退：var(--c-focus, var(--c-accent))", () => {
    const bare = RULES.flatMap((r) => (r.body.match(/var\(--c-focus[^,)]*\)/g) ?? []).map((m) => `${r.selector} → ${m}`));
    expect(bare).toEqual([]);
    expect(RULES.some((r) => r.body.includes("var(--c-focus, var(--c-accent))"))).toBe(true);
  });

  it("没有被先写规则压住的覆盖", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });

  it("keeps the ordered authenticated styles starting with ui-refresh-workbench.css", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    const expectedImports = ["../../styles/ui-refresh-workbench.css", "../../styles/ui-refresh-materials.css"];
    const start = imports.indexOf(expectedImports[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(imports.slice(start, start + expectedImports.length)).toEqual(expectedImports);
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-workbench.css");
  });
});

describe("运行状态带外框（对话卡片规格 D1：与对话卡片同一外壳；完成摘要与失败卡是 ChatCard）", () => {
  it("圆角 r4、内容面、发丝描边、无边框", () => {
    expect(rule(".bh-band")).toMatchObject({
      border: "0",
      "border-radius": "var(--r-4)",
      background: "var(--c-surface)",
      "box-shadow": "0 0 0 var(--hairline) var(--c-separator)",
    });
  });

  it("运行带披露条 40 高、文字 13/500、元信息 12 次要色系统字；展开箭头 28 见方", () => {
    expect(rule(".bh-band__bar")).toMatchObject({ "min-height": "40px", padding: "0 6px 0 12px", background: "transparent", "border-bottom": "var(--hairline) solid var(--c-separator)" });
    expect(rule(".bh-band__bar.legacy-band-bar--expanded")).toEqual({ "border-bottom": "0" });
    expect(rule(".bh-band__txt")).toMatchObject({ "font-size": "var(--fs-md)", "font-weight": "500", color: "var(--c-ink)" });
    expect(rule(".bh-band__meta")).toMatchObject({ "font-size": "var(--fs-sm)", color: "var(--c-ink-3)", "font-family": "var(--font-control)" });
    expect(rule(".bh-band__chev")).toMatchObject({ width: "28px", height: "28px" });
  });

  it("三行结构只换外观：历史行与当前行系统字 12、完成勾次要色，重试 / 失败 / 进行中仍是语义色；不写行高与行数", () => {
    expect(rule(".bh-band__hist")).toMatchObject({ "font-family": "var(--font-control)" });
    expect(rule(".bh-band__glyph")).toEqual({ color: "var(--c-ink-3)" });
    expect(rule(".bh-band__row.is-warn .bh-band__glyph")).toEqual({ color: "var(--c-warn-ink)" });
    expect(rule(".bh-band__row.is-fail .bh-band__glyph")).toEqual({ color: "var(--c-err)" });
    expect(rule(".bh-band__foot")).toMatchObject({ "font-family": "var(--font-control)", "font-size": "var(--fs-sm)" });
    // Constant height relies on three rows/original line height: this sheet neither changes .bh-band__row min-height nor hides any row.
    expect(rule(".bh-band__row")).toEqual({ "font-size": "var(--fs-sm)" });
    expect(RULES.filter((r) => /bh-band/.test(r.selector) && /display:\s*none/.test(r.body))).toEqual([]);
  });

  it("减少动态效果（系统与应用内）时转圈换成静止的强调色圆点", () => {
    expect(rule(".bh-band__spin", /prefers-reduced-motion: reduce/)).toMatchObject({ border: "0", background: "var(--c-accent)" });
    const inApp = RULES.find((r) => norm(r.selector) === `${PREFIX}[data-theme-motion="reduced"] .bh-band__spin`);
    expect(inApp && decls(inApp.body)).toMatchObject({ border: "0", background: "var(--c-accent)" });
  });

  it("步骤里的失败只是一句话：正文色 13、提示 12 次要色，不是卡片", () => {
    expect(rule(".bh-step-failure__msg")).toMatchObject({ color: "var(--c-ink)" });
    expect(rule(".bh-step-failure__hint")).toMatchObject({ color: "var(--c-ink-3)" });
    expect(RULES.some((r) => /bh-step-failure/.test(r.selector) && /border|box-shadow|background/.test(r.body))).toBe(false);
  });
});

describe("步骤列表（一种行、一列图标、最多一层缩进）", () => {
  it("工具步骤行 30 高、图标列 16；标签 13、摘要与用时 12 次要色系统字", () => {
    expect(rule(".bh-step-list .ag-trace .stp-head")).toMatchObject({ "min-height": "30px", "align-items": "center" });
    expect(rule(".bh-step-list .ag-trace .g")).toMatchObject({ width: "16px", flex: "0 0 16px" });
    expect(rule(".bh-step-list .ag-trace .stp-tool")).toMatchObject({ "font-size": "var(--fs-md)" });
    for (const sel of [".bh-step-list .ag-trace .stp-label", ".bh-step-list .ag-trace .stp-sum", ".bh-step-list .ag-trace .stp-time"]) {
      expect(rule(sel), sel).toMatchObject({ color: "var(--c-ink-3)", "font-size": "var(--fs-sm)", "font-family": "var(--font-control)" });
    }
  });

  it("完成的勾用次要色、进行中强调色、未开始 ink-4", () => {
    expect(rule(".bh-step-list .ag-trace .g-done")).toEqual({ color: "var(--c-ink-3)" });
    expect(rule(".bh-step-list .ag-trace .g-run")).toEqual({ color: "var(--c-accent)" });
    expect(rule(".bh-step-list .ag-trace .g-pend")).toEqual({ color: "var(--c-ink-4)" });
  });

  it("智能体头 32 高单行；块体不再嵌第二层框，步骤缩进 30", () => {
    expect(rule(".bh-step-list .ag-trace .agb-head")).toMatchObject({ "min-height": "32px", "grid-template-rows": "auto" });
    expect(rule(".bh-step-list .ag-trace .agb-body")).toEqual({ padding: "0 6px 6px 30px", "border-top": "0", background: "transparent" });
  });

  it("工具类别小标不低于 11px、不大写", () => {
    expect(rule(".kchip")).toMatchObject({ "font-size": "var(--fs-xs)", "text-transform": "none" });
  });
});

describe("消息操作与引用角标", () => {
  it("消息操作是 28 见方图标按钮（触屏 44），文字只给读屏与 Tooltip", () => {
    expect(rule(".bh-msg-actions__btn")).toMatchObject({ width: "28px", height: "28px", padding: "0" });
    expect(rule(".bh-msg-actions__btn", /pointer: coarse/)).toEqual({ width: "44px", height: "44px" });
    expect(rule(".bh-msg-actions__btn > span")).toMatchObject({ position: "absolute", width: "1px", height: "1px", clip: "rect(0 0 0 0)" });
    expect(rule('.bh-msg-actions__btn[data-state="copied"] > span')).toMatchObject({ position: "static", clip: "auto" });
  });

  it("最新一条回答的操作条在指针设备上常显", () => {
    expect(rule('.bh-msg-actions[data-latest="1"]', /hover: hover/)).toEqual({ opacity: "1", "pointer-events": "auto" });
  });

  it("角标系统字等宽数字；受限 / 失效不再靠透明度，改为透明底 + 发丝描边 + 删除线", () => {
    expect(rule(".legacy-cite-chip")).toMatchObject({ "font-family": "var(--font-control)", "font-variant-numeric": "tabular-nums", background: "var(--c-fill-2)" });
    expect(rule(".legacy-cite-chip.is-denied")).toEqual({
      opacity: "1",
      background: "transparent",
      "box-shadow": "inset 0 0 0 var(--hairline) var(--c-control-line)",
      color: "var(--c-ink-3)",
      "text-decoration": "line-through",
    });
  });
});

describe("计划卡外壳（对话卡片规格 D1）", () => {
  it("内容面、r4、发丝结构线、无投影；待批准 / 受阻换 1px 警示色描边", () => {
    expect(rule(".pcard")).toMatchObject({
      border: "var(--card-edge)",
      "border-radius": "var(--card-radius)",
      background: "var(--c-surface)",
      "box-shadow": "none",
    });
    expect(rule(":is(.pcard.is-proposed, .pcard.is-blocked)")).toEqual({ border: "var(--card-edge-input)" });
    expect(rule(".pbtn.primary")).toMatchObject({ color: "var(--c-accent-ink)" });
  });

  it("计划卡头部不再按状态整块染色；头行 12/600 语义色", () => {
    expect(rule(".pcard.is-proposed .pc-head")).toEqual({ background: "transparent" });
    expect(rule(".pc-eyebrow")).toMatchObject({ "font-size": "var(--fs-sm)", "font-weight": "600", color: "var(--c-warn-ink)" });
    expect(rule(".pcard.is-running .pc-eyebrow")).toEqual({ color: "var(--c-accent-text)" });
  });
});

describe("输入框停止键与成果区", () => {
  it("停止键中性填充底 + 正文色（不用强调色，避免与发送混淆）", () => {
    expect(rule(".composer-v2 .v2-send-btn--cancel")).toMatchObject({ background: "var(--c-fill-2)", color: "var(--c-ink)" });
  });

  it("分栏线 1px 结构线、9 宽可拖区；拖动中 2px 强调色", () => {
    expect(rule(".v2-split-divider")).toEqual({ width: "9px", "margin-left": "2.5px" });
    expect(rule(".v2-split-divider-line")).toMatchObject({ width: "1px", background: "var(--c-separator-strong)" });
    expect(rule(".v2-split-divider.is-active .v2-split-divider-line")).toEqual({ background: "var(--c-ink-3)" });
    expect(rule(".v2-split-divider.is-active:active .v2-split-divider-line")).toMatchObject({ width: "2px", background: "var(--c-accent)" });
  });

  it("手机「对话 / 成果」分段 32 高（外框 36），触屏每段 44", () => {
    expect(rule(".v2-ws-mobile-tabs .v2-ws-mobile-tabs-pill .v2-ws-mobile-tab", /^@media \(max-width: 1023\.98px\)$/)).toMatchObject({ "min-height": "32px", "font-size": "0.875rem" });
    const coarse = RULES.find((r) => /pointer: coarse/.test(r.media ?? "") && /v2-ws-mobile-tab$/.test(norm(r.selector)));
    expect(coarse && decls(coarse.body)).toEqual({ "min-height": "44px" });
  });

  it("恢复条 40 宽、只留图标（名称视觉隐藏，按钮自带读屏名称）", () => {
    expect(rule(".v2-ws-edge")).toMatchObject({ flex: "0 0 40px" });
    expect(rule(".v2-ws-edge-label")).toMatchObject({ position: "absolute", width: "1px", clip: "rect(0 0 0 0)" });
  });

  it("结构线 token 深浅色各一档", () => {
    const light = RULES.find((r) => r.selector === PREFIX);
    const dark = RULES.find((r) => r.selector === `${PREFIX}[data-theme="dark"]`);
    expect(decls(light!.body)["--c-separator-strong"]).toBe("color-mix(in srgb, var(--c-ink) 20%, transparent)");
    expect(decls(dark!.body)["--c-separator-strong"]).toBe("color-mix(in srgb, var(--c-ink) 24%, transparent)");
  });
});

describe("首页控件层", () => {
  it("页标题 22/700 系统字；提议卡是内容面发丝线卡片，不再有彩色图标块与右侧箭头列", () => {
    expect(rule(".home-work-header h1")).toMatchObject({ "font-family": "var(--font-control)", "font-size": "var(--fs-page-title)", "font-weight": "700" });
    expect(rule(".home-work-proposal")).toMatchObject({ background: "var(--c-surface)", "box-shadow": "0 0 0 var(--hairline) var(--c-separator)", "grid-template-columns": "minmax(0, 1fr)" });
    expect(rule(".home-work-proposal-icon")).toMatchObject({ background: "transparent", color: "var(--c-ink-2)" });
    expect(rule(".home-work-proposal-action")).toEqual({ display: "none" });
  });

  it("Centers the wider home composer and preserves the inline dock and content order", () => {
    expect(rule(".home-work-inner")).toMatchObject({ width: "min(100%, calc(800px + 2 * var(--page-gutter)))" });
    expect(rule(".home-work-header")).toMatchObject({ "grid-template-columns": "minmax(0, 1fr)", "text-align": "center" });
    expect(rule(".home-work-layout")).toMatchObject({ "grid-template-columns": "minmax(0, 1fr)", "align-content": "start" });
    expect(rule(".home-work-proposal-list")).toMatchObject({ "grid-template-columns": "repeat(auto-fit, minmax(min(200px, 100%), 1fr))" });
    // Specificity 0,4,0 overrides narrow-screen globals.css .home-work-composer-dock padding-bottom (including --bh-kb-inset).
    expect(rule(".home-work-composer-dock.home-work-composer-dock--inline")).toEqual({ padding: "0", "border-top": "0", background: "none" });
    // Section headings are screen-reader-only, not display: none.
    expect(rule(".home-work-layout .home-work-section-heading")).toMatchObject({ position: "absolute", clip: "rect(0 0 0 0)" });
  });

  it("single-column DOM: the page puts the inline composer dock into the surface slot after the heading", () => {
    const page = read("app/(app)/page.tsx");
    expect(page).toContain('className="home-work-composer-dock home-work-composer-dock--inline"');
    expect(page).toContain("composerSlot={showHomeComposer ? homeComposerDock : undefined}");
    const surface = read("components/home/HomeWorkSurface.tsx");
    expect(surface).toContain("{[proposalsSection, briefSection]}");
  });
});

describe("P2 batch 2: stroke of small icons upscaled by CSS", () => {
  it("uses 1.5 for the rendered size, overriding the thin inline stroke derived from the attributes", () => {
    for (const sel of [
      ".home-work-clear > svg.bh-icon",
      '.ui-menu [role="menuitem"] > svg.bh-icon:first-child',
      '.ui-menu [role="option"] > svg.bh-icon:first-child',
      ".home-work-proposal-action svg.bh-icon",
    ]) {
      expect(rule(sel)["stroke-width"], sel).toBe("1.5px !important");
    }
  });
});
