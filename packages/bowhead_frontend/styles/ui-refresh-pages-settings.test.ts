/**
 * Interface appearance, page acceptance A: personal/team/space settings
 * source contracts (styles/ui-refresh-pages-settings.css).
 *
 * jsdom does not cascade; enforce source facts here; real-browser screenshots validate pixels.
 *   1. Every selector starts with :root:root (including @media);
 *      no literal colors except the :root:root token block; dividers avoid --c-line; hierarchy only --z-sticky (Save bar), only
 * Save bar is bottom-positioned.
 *   2. Imported last only by authenticated (app) layout, never public layout.
 *   3. Shared spec structures: grouped cards/left labels-right controls (tradeoffs 15, 16), Save bar (17), active-filter
 * chips (20), loading skeletons.
 *   4. Cascade: later overrides must not lose to earlier rules.
 *   5. Enforce every page mapping; call sites keep the classes migrated from inline styles (styles/ui-controls-legacy.css “逐页验收 A”).
 */
import { describe, expect, it } from "vitest";

import {
  PREFIX,
  LITERAL_COLOR,
  cascadeProblems,
  decls,
  isTokenRoot,
  norm,
  parseRules,
  read,
  specificity,
  splitSelectors,
  stripComments,
  type Rule,
} from "./css-contract";

const CSS = read("styles/ui-refresh-pages-settings.css");
const RULES = parseRules(stripComments(CSS));
/** Shared-structure section: before first `@section accept-a:<group>` (later group mappings have their own contracts). */
const SHARED = parseRules(stripComments(CSS.slice(0, CSS.indexOf("/* @section "))));

/** Merge declarations for exact `${PREFIX} ${sel}` in selector list (restrict @media; default outside @media). */
function exact(sel: string, media: RegExp | null = null): Record<string, string> {
  const found = RULES.filter(
    (r) => (media ? media.test(r.media ?? "") : r.media === null) && splitSelectors(r.selector).includes(`${PREFIX} ${norm(sel)}`),
  );
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("styles/ui-refresh-pages-settings.css: every rule starts with the :root:root prefix", () => {
  it("解析出规则", () => {
    expect(RULES.length).toBeGreaterThan(40);
  });

  it("every selector starts with :root:root (inside @media too)", () => {
    const offenders = RULES.flatMap((rule) => splitSelectors(rule.selector)).filter((sel) => !sel.startsWith(PREFIX));
    expect(offenders).toEqual([]);
  });

  it("除开关根上的 token 定义外不写字面色值", () => {
    const offenders = RULES.filter((rule) => !splitSelectors(rule.selector).every(isTokenRoot)).flatMap((rule) =>
      stripComments(rule.body)
        .split(";")
        .map((d) => d.trim())
        .filter((d) => LITERAL_COLOR.test(d))
        .map((d) => `${splitSelectors(rule.selector)[0]} { ${d} }`),
    );
    expect(offenders).toEqual([]);
  });

  it("分隔线与描边不用 --c-line（发丝线 --c-separator / 控件描边 --c-control-line）", () => {
    const offenders = RULES.filter((r) => /var\(--c-line\)/.test(r.body)).map((r) => norm(r.selector));
    expect(offenders).toEqual([]);
  });

  it("层级只用 --z-sticky、定位只有保存栏吸底（不改浮层层级与挂载方式）", () => {
    const zOffenders = RULES.filter((r) => /(^|;)\s*z-index\s*:/.test(stripComments(r.body)))
      .filter((r) => decls(r.body)["z-index"] !== "var(--z-sticky)")
      .map((r) => norm(r.selector));
    expect(zOffenders).toEqual([]);
    // Each group mapping has its own contract (ui-refresh-pages-settings-<group>.test.ts); only shared structures here.
    const positioned = SHARED.filter((r) => "position" in decls(r.body));
    for (const r of positioned) {
      expect(r.selector, r.selector).toMatch(/\.ui-savebar/);
      if (!r.media) {
        // ≥ 1200 sticks to scroll-region bottom (is-inline sticks locally).
        expect(decls(r.body).position, r.selector).toBe("sticky");
        continue;
      }
      // < 1024 (P2 batch 4; < 1200 before) whole document scrolls: slot returns to flow (static), bar itself fixes to viewport bottom.
      expect(r.media).toMatch(/max-width:\s*1023\.98px/);
      expect(decls(r.body).position, r.selector).toBe(r.selector.includes("> .ui-savebar") ? "fixed" : "static");
    }
  });

  it("< 1024（P2 第 4 批）保存栏贴视口底边、随软键盘抬起，插槽里的占位块撑出栏高；不靠 :has() 失效", () => {
    expect(exact(".ui-savebar-spacer")).toMatchObject({ display: "none" });
    expect(exact(".ui-savebar-spacer", /max-width:\s*1023\.98px/)).toMatchObject({ display: "block" });
    expect(exact(".ui-savebar-slot", /max-width:\s*1023\.98px/)).toMatchObject({ position: "static", "margin-bottom": "0" });
    expect(exact(".ui-savebar-slot > .ui-savebar", /max-width:\s*1023\.98px/)).toMatchObject({
      position: "fixed",
      left: "0",
      right: "0",
      bottom: "var(--savebar-keyboard-inset, 0px)",
    });
    expect(RULES.filter((r) => r.selector.includes(":has(") && r.selector.includes("savebar"))).toEqual([]);
  });
});

describe("引入位置", () => {
  it("keeps the ordered authenticated styles starting with ui-refresh-surfaces.css", () => {
    const layout = read("app/(app)/layout.tsx");
    const imports = Array.from(layout.matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    const expectedImports = ["../../styles/ui-refresh-surfaces.css", "../../styles/ui-refresh-pages-settings.css", "../../styles/ui-refresh-pages-admin.css", "../../styles/ui-refresh-pages-main.css", "../../styles/ui-refresh-canvas.css", "../../styles/ui-refresh-workbench.css", "../../styles/ui-refresh-materials.css"];
    const start = imports.indexOf(expectedImports[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(imports.slice(start, start + expectedImports.length)).toEqual(expectedImports);
  });

  it("公共布局不引入", () => {
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-pages-settings.css");
  });
});

describe("级联：后写的覆盖规则不能输给先写的规则", () => {
  it("本文件没有被先写规则压住的覆盖", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });

  it("能抓到「后写的覆盖权重更低」这一类（含简写覆盖长写）", () => {
    const bad: Rule[] = parseRules(`${PREFIX} :is(.a, .b .c) { margin-top: 0; }
${PREFIX} .a { margin: 8px 0 4px; }`);
    expect(cascadeProblems(bad)).toHaveLength(1);
    expect(specificity(`${PREFIX} :where(.x) .a`)).toEqual([0, 3, 0]);
  });
});

describe("分组卡片与左标签右控件（取舍 15、16；Page Layout Spec「设置项页面」「表单对齐」）", () => {
  it("uses a 260px label column, fluid controls, 44px minimum rows and 32px group spacing", () => {
    const tokens = Object.assign({}, ...RULES.filter((r) => r.selector === PREFIX && r.media === null).map((r) => decls(r.body)));
    expect(tokens["--group-label-w"]).toBe("260px");
    expect(tokens["--group-control-max"]).toBe("100%");
    expect(tokens["--group-row-min-h"]).toBe("44px");
    expect(tokens["--group-gap"]).toBe("32px");
  });

  it("keeps group headings outside cards and preserves spacing across intervening content", () => {
    expect(exact(".ui-group-title")).toMatchObject({ "font-size": "var(--fs-lg)", "font-weight": "650", "font-family": "var(--font-control)" });
    expect(exact(".ui-group-desc")).toMatchObject({ "font-size": "0.78125rem", color: "var(--c-ink-3)" });
    expect(exact(".ui-group-head")).toMatchObject({ margin: "0 0 8px" });
    expect(exact(".ui-group + .ui-group")).toMatchObject({ "margin-top": "var(--group-gap)" });
    expect(exact(".ui-group ~ .ui-group")).toMatchObject({ "margin-top": "var(--group-gap)" });
  });

  it("卡片：内容面、--r-4、0.5px 发丝线外框", () => {
    expect(exact(".ui-group-card")).toMatchObject({
      background: "var(--c-surface)",
      "border-radius": "var(--r-4)",
      "box-shadow": "0 0 0 var(--hairline) var(--c-separator)",
    });
  });

  it("aligns desktop labels and fluid controls with comfortable row padding and separators", () => {
    const row = exact(".ui-group-row");
    expect(row).toMatchObject({
      display: "grid",
      "grid-template-columns": "var(--group-label-w) minmax(0, 1fr)",
      "min-height": "var(--group-row-min-h)",
      padding: "18px 24px",
    });
    expect(exact(".ui-group-card > .settings-field")).toMatchObject(row);
    expect(exact(".ui-group-row + .ui-group-row")).toMatchObject({ "box-shadow": "inset 0 var(--hairline) 0 var(--c-separator)" });
    expect(exact(".ui-group-control")).toMatchObject({ "max-width": "var(--group-control-max)" });
    expect(exact(".ui-group-label")).toMatchObject({ "font-size": "var(--fs-md)", "font-weight": "500", color: "var(--c-ink)" });
    expect(exact(".ui-group-hint")).toMatchObject({ "font-size": "var(--fs-sm)", color: "var(--c-ink-4)" });
  });

  it("places grouped Field labels and hints on the left and controls on the right", () => {
    expect(exact(".ui-group-card > .settings-field > .settings-field__label")).toMatchObject({ "grid-column": "1", "grid-row": "1" });
    expect(exact(".ui-group-card > .settings-field > .settings-field__hint")).toMatchObject({ "grid-column": "1", "grid-row": "2", color: "var(--c-ink-4)" });
    expect(exact(".ui-group-card > .settings-field > :not(.settings-field__label, .settings-field__hint, .settings-field__error)")).toMatchObject({
      "grid-column": "2",
      "max-width": "var(--group-control-max)",
    });
  });

  it("< 768：标签改上下，控件不限宽", () => {
    const narrow = /max-width:\s*767\.98px/;
    expect(exact(".ui-group-row", narrow)).toMatchObject({ "grid-template-columns": "minmax(0, 1fr)" });
    expect(exact(".ui-group-card > .settings-field", narrow)).toMatchObject({ "grid-template-columns": "minmax(0, 1fr)" });
    expect(exact(".ui-group-control", narrow)).toMatchObject({ "max-width": "none" });
  });
});

describe("保存栏（取舍 17；Page Layout Spec「改动后才出现保存栏」）", () => {
  it("插槽吸在滚动区底边：sticky bottom -48（吸底参照扣掉外壳 48 内边距的内容框）、--z-sticky、上 auto 推到底边、下 -48 吃掉外壳内边距", () => {
    expect(exact(".ui-savebar-slot")).toMatchObject({
      position: "sticky",
      bottom: "-48px",
      "z-index": "var(--z-sticky)",
      margin: "auto calc(-1 * var(--page-gutter)) -48px",
    });
    expect(exact(".ui-savebar-slot:empty")).toMatchObject({ display: "none" });
    expect(exact(".settings-shell.is-settings-surface > .settings-content")).toMatchObject({ display: "flex", "flex-direction": "column" });
  });

  it("栏材质（P2 第 1 批；减少透明度时 background 实底）、上边发丝线、12.5 --c-ink-2；未保存标记是 7px 警示色圆点", () => {
    expect(exact(".ui-savebar")).toMatchObject({
      background: "var(--ui-material-bar)",
      "backdrop-filter": "var(--ui-material-filter)",
      "box-shadow": "0 calc(-1 * var(--hairline)) 0 var(--c-separator)",
      "font-size": "0.78125rem",
      color: "var(--c-ink-2)",
      padding: "10px var(--page-gutter) calc(10px + env(safe-area-inset-bottom, 0px))",
    });
    expect(exact(".ui-savebar-dot")).toMatchObject({ width: "7px", height: "7px", "border-radius": "50%", background: "var(--c-warn)" });
  });

  it("不在设置外壳里时就地吸底", () => {
    expect(exact(".ui-savebar.is-inline")).toMatchObject({ position: "sticky", bottom: "0", "z-index": "var(--z-sticky)" });
  });

  it("the settings shell renders the slot and passes it through context", () => {
    const shell = read("components/settings/settings-shell.tsx");
    expect(shell).toContain('<div ref={setSaveBarSlot} className="ui-savebar-slot" />');
    expect(shell).toContain("<SaveBarSlotContext.Provider value={saveBarSlot}>");
  });
});

describe("已选筛选 chip 行（取舍 20；Misc Spec「筛选栏」）", () => {
  it("chip 24 高、全圆角、--c-fill-2 底、12px；× 18 见方 --c-ink-3；「清除全部」强调色文字", () => {
    expect(exact(".ui-filter-chip")).toMatchObject({ height: "24px", "border-radius": "999px", background: "var(--c-fill-2)", "font-size": "var(--fs-sm)" });
    expect(exact(".ui-filter-chip-remove")).toMatchObject({ width: "18px", height: "18px", color: "var(--c-ink-3)" });
    expect(exact(".ui-filter-chips-clear")).toMatchObject({ color: "var(--c-accent-text)" });
  });

});

describe("页面级加载骨架（Page Layout Spec 第 4 节）", () => {
  it("列表骨架行与真实表格行同高（--table-row-h），骨架条 --c-fill-2、10 高 5 圆角", () => {
    expect(exact(".ui-skeleton-row")).toMatchObject({ "min-height": "var(--table-row-h)" });
    expect(exact(".ui-skeleton-bar")).toMatchObject({ height: "10px", "border-radius": "5px", background: "var(--c-fill-2)" });
    expect(exact(".ui-skeleton-circle")).toMatchObject({ width: "26px", height: "26px", background: "var(--c-fill-2)" });
  });

  it("matches form skeletons to the fluid grouped rows and delays their visibility", () => {
    expect(exact(".ui-skeleton--form .ui-skeleton-row")).toMatchObject({
      "grid-template-columns": "var(--group-label-w) minmax(0, 1fr)",
      "min-height": "var(--group-row-min-h)",
    });
    expect(exact(".ui-skeleton.is-pending")).toMatchObject({ visibility: "hidden" });
  });
});

describe("团队设置 · 引用保留（设置项页：分组卡片 + 保存栏）", () => {
  it("call site: days sit in a group card and the save bar submits", () => {
    const src = read("components/settings/reference-retention.tsx");
    expect(src).toContain('<div className="ui-group-card">{daysField}</div>');
    expect(src).toMatch(/<SaveBar dirty=\{dirty && !needsReload && !error\} saving=\{busy\} saveDisabled=\{!canSave\}\s+onSave=\{submit\}/);
    expect(src).not.toMatch(/style=\{\{ (marginTop: 24|maxWidth: 180|minHeight: 44|color: "var\(--c-ink-2\)")/);
  });
});
