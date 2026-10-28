/**
 * Interface appearance style tail: dialog/drawer inline styles, Agents-page cards, Tools-page mobile card layout.
 *
 * jsdom does no cascading; enforce source facts here, with real-browser element comparison/screenshots for pixels (design “样式尾巴”).
 *   1. Call sites use the classes migrated from inline styles (styles/ui-controls-legacy.css “style-tail:*” sections) and never
 *      restore inline styles; state-dependent values use modifier classes, with exactly the original ternary condition enforced;
 *   2. Interface rules (ui-refresh-surfaces.css “style-tail:dialogs”, ui-refresh-pages-main.css “style-tail:agents”,
 *      ui-refresh-pages-settings.css “style-tail:tools-mobile”) start every selector with :root:root, use no literal colors,
 *      later rules never lose to earlier ones; focus ring uses var(--c-focus, var(--c-accent));
 *   3. Original Tools mobile !important rules remain as the base layer; refreshed rules write only under the prefix.
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
const LEGACY_SECTIONS = [
  "style-tail:tools",
  "style-tail:spaces",
  "style-tail:usage",
  "style-tail:scheduled",
  "style-tail:skills",
  "style-tail:settings-dialogs",
  "style-tail:credits",
  "style-tail:share",
];
const LEGACY = LEGACY_SECTIONS.flatMap((name) => parseRules(stripComments(readSection(LEGACY_FILE, name))));

/** Legacy-rule declarations (preserve order). */
function legacyOrdered(selector: string): Array<[string, string]> {
  const found = LEGACY.filter((r) => r.media === null && splitSelectors(r.selector).includes(selector));
  expect(found, selector).toHaveLength(1);
  return stripComments(found[0].body)
    .split(";")
    .map((d) => d.trim())
    .filter(Boolean)
    .map((d) => {
      const i = d.indexOf(":");
      return [d.slice(0, i).trim(), norm(d.slice(i + 1))] as [string, string];
    });
}

const SECTIONS: Array<[string, string]> = [
  ["styles/ui-refresh-surfaces.css", "style-tail:dialogs"],
  ["styles/ui-refresh-pages-main.css", "style-tail:agents"],
  ["styles/ui-refresh-pages-settings.css", "style-tail:tools-mobile"],
];
const ruleSet = (file: string, name: string) => parseRules(stripComments(readSection(file, name)));
const DIALOGS = ruleSet(...SECTIONS[0]);
const AGENTS = ruleSet(...SECTIONS[1]);
const TOOLS_MOBILE = ruleSet(...SECTIONS[2]);
const RULES = [...DIALOGS, ...AGENTS, ...TOOLS_MOBILE];

/** Merge rules containing exact `${PREFIX} ${sel}` (or inside :is()); null media selects only rules outside @media. */
function on(rules: typeof RULES, sel: string, media: RegExp | null = null): Record<string, string> {
  const found = rules.filter((r) => {
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

describe("调用点：原内联样式已移走", () => {
  const styleLines = (rel: string) =>
    read(rel)
      .split("\n")
      .filter((line) => line.includes("style={"))
      .map((line) => norm(line.slice(line.indexOf("style={"))));

  it.each([
    "components/settings/tools/tools-manager.tsx",
    "components/settings/tools/powerbi-tool-section.tsx",
    "components/settings/team-spaces-admin.tsx",
    "components/settings/resource-usage.tsx",
    "components/skill-drawer.tsx",
    "components/settings/datarc-connections.tsx",
    "components/settings/builtin-subagents-section.tsx",
    "components/admin/credits/period-reset-modal.tsx",
    "components/admin/credits/team-quota-modal.tsx",
    "components/admin/credits/grant-credit-modal.tsx",
    "components/admin/credits/edit-user-modal.tsx",
  ])("%s 不再写内联样式", (rel) => {
    expect(styleLines(rel)).toEqual([]);
  });

  it("scheduled tasks keep no inline styles and the S style table is gone", () => {
    const src = read("components/settings/scheduled.tsx");
    expect(styleLines("components/settings/scheduled.tsx")).toEqual([]);
    expect(src).not.toMatch(/\bconst S = \{/);
    expect(src).not.toContain("CSSProperties");
  });

  it("分享弹窗只剩按调用点取值的按钮宽度（ShBtn 的 style 参数）；头像、分段与按钮尺寸仍经 CSS 变量或 size 内联", () => {
    expect(styleLines("components/share/share-dialog.tsx")).toEqual([
      'style={mobile ? { flex: 1 } : undefined}>{t("cancel")}</ShBtn>',
      'style={mobile ? { flex: 1 } : undefined}>{t("stop_access")}</ShBtn>',
    ]);
    const ui = read("components/share/share-ui.tsx");
    expect(ui).toContain('<span className="legacy-share-type-tag">{kind}</span>');
    expect(ui).toContain('className={`legacy-share-perm-tag${small ? " is-small" : ""}`}');
    expect(ui).toContain('className={`legacy-share-quiet-mark${tint ? " is-tint" : ""}`}');
    const dialog = read("components/share/share-dialog.tsx");
    for (const snippet of [
      'className={`legacy-share-state-err${mobile ? " is-mobile" : ""}`}',
      'className={`legacy-share-state-loading${mobile ? " is-mobile" : ""}`}',
      'className={`legacy-share-body${mobile ? " is-mobile" : ""}`}',
      'className={`legacy-share-row${hover ? " is-hover" : ""}`}',
      'className={`legacy-share-row-remove${hover ? " is-visible" : ""}`}',
      'className={`legacy-share-search-box${focus ? " is-focus" : ""}`}',
      'className={`legacy-share-suggest-row${hover ? " is-hover" : ""}`}',
      'className={`legacy-share-ro-body${mobile ? " is-mobile" : ""}`}',
    ]) {
      expect(dialog, snippet).toContain(snippet);
    }
  });

  it("修饰类与原三元表达式同一个条件（逐分支）", () => {
    const tools = read("components/settings/tools/tools-manager.tsx");
    expect(tools).toContain('className={`legacy-tools-scope-head${verified ? " is-verified" : ""}`}');
    expect(tools).toContain('className={`legacy-tools-scope-mark${verified ? " is-verified" : ""}`}');
    expect(tools).toContain('className={`legacy-tools-scope-status${verified ? " is-verified" : ""}`}');
    expect(tools).toContain('className={`legacy-tools-impact-cell${index % 2 ? " is-col2" : ""}${index >= 2 ? " is-row2" : ""}`}');
    expect(read("components/settings/tools/powerbi-tool-section.tsx")).toContain(
      'className={`legacy-tools-pbi-row${tool.status === "enabled" ? "" : " is-off"}`}',
    );
    const usage = read("components/settings/resource-usage.tsx");
    expect(usage).toContain('`legacy-usage-summary${summary.completeness === "partial" ? " is-partial" : ""}`');
    expect(usage).toContain("className={`legacy-usage-readiness is-${item.readiness}`}");
    expect(read("components/skill-drawer.tsx")).toContain(
      '`legacy-skill-drawer-team-state${sync.state === "rejected" || sync.state === "update_rejected" ? " is-rejected" : ""}`',
    );
    expect(read("components/admin/credits/edit-user-modal.tsx")).toContain('className={`legacy-credits-edit-check${disabled ? " is-disabled" : ""}`}');
    const sched = read("components/settings/scheduled.tsx");
    for (const snippet of [
      'className={`legacy-sched-dot${paused ? " is-paused" : ""}`}',
      'className={`legacy-sched-dot legacy-sched-dot--m${paused ? " is-paused" : ""}`}',
      'className={`legacy-sched-r-name${paused ? " is-dim" : ""}`}',
      'className={`legacy-sched-r-name legacy-sched-r-name--m${paused ? " is-dim" : ""}`}',
      'className={`legacy-sched-dr-body${copilotEnabled ? " ccp" : ""}`}',
      'className={`legacy-sched-preset-btn${preset === p.key && !usingCustom ? " is-on" : ""}`}',
      'className={`legacy-sched-preset-icon${preset === p.key && !usingCustom ? " is-on" : ""}`}',
      'className={`legacy-sched-weekday-btn${weekday === wk ? " is-on" : ""}`}',
      'className={`legacy-sched-adv-chev${advOpen ? " is-open" : ""}`}',
      'className={`legacy-sched-inp-mono${cronErr ? " is-err" : ""}`}',
      'className={`legacy-sched-chips${copilotEnabled ? " chips" : ""}`}',
      'className={`legacy-sched-btn-pri${canSave ? "" : " is-disabled"}`}',
      'className={`legacy-sched-icon-btn${hoverClass}${disabled ? " is-disabled" : ""}`}',
      'className={ai ? `legacy-sched-field is-ai fld ${ai.state}` : "legacy-sched-field"}',
      'className={`legacy-sched-select${disabled ? " is-disabled" : ""}`}',
      'className={`legacy-sched-rcard${on ? " is-on" : ""}`}',
      'className={`legacy-sched-radio${on ? " is-on" : ""}`}',
      'className={`legacy-sched-pill${on ? " is-on" : ""}`}',
    ]) {
      expect(sched, snippet).toContain(snippet);
    }
    expect(sched.match(/className=\{`legacy-sched-meta-ln\$\{paused \? " is-dim" : ""\}`\}/g)).toHaveLength(4);
    expect(sched).toContain(
      'const hoverClass = !hover ? "" : variant === "danger" ? " is-hover-danger" : variant === "run" ? " is-hover-run" : " is-hover";',
    );
  });
});

describe("interface rules start with the :root:root prefix", () => {
  it("每段都解析出规则，每个选择器都带开关前缀（含 @media 内）", () => {
    for (const [file, name] of SECTIONS) {
      expect(ruleSet(file, name).length, `${file} ${name}`).toBeGreaterThan(2);
    }
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("不写字面色值、不写 z-index", () => {
    const literal = RULES.filter((r) => !splitSelectors(r.selector).every(isTokenRoot)).flatMap((r) =>
      r.body
        .split(";")
        .map((x) => x.trim())
        .filter((x) => LITERAL_COLOR.test(x)),
    );
    expect(literal).toEqual([]);
    expect(RULES.filter((r) => "z-index" in decls(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("没有被先写规则压住的覆盖", () => {
    expect(cascadeProblems(DIALOGS)).toEqual([]);
    expect(cascadeProblems(AGENTS)).toEqual([]);
    expect(cascadeProblems(TOOLS_MOBILE)).toEqual([]);
  });

  it("焦点环一律 var(--c-focus, var(--c-accent))", () => {
    const outlines = RULES.filter((r) => /:focus-visible/.test(r.selector))
      .map((r) => decls(r.body).outline)
      .filter(Boolean);
    expect(outlines.length).toBeGreaterThan(5);
    expect(outlines.every((o) => o === "2px solid var(--c-focus, var(--c-accent))")).toBe(true);
  });

  it("控件用系统字体；等宽只留给 cron 这类内容", () => {
    const families = RULES.flatMap((r) => (decls(r.body)["font-family"] ? [[norm(r.selector), decls(r.body)["font-family"]]] : []));
    expect(families.filter(([, f]) => f !== "var(--font-control)")).toEqual([[`${PREFIX} .routine-drawer .legacy-sched-inp-mono`, "var(--font-v2-mono)"]]);
  });
});

describe("弹窗与抽屉内部（Component Spec / Page Layout Spec 第 3 节）", () => {
  it("弹窗正文字段间距 12；引导句 13 --c-ink-2；补充说明 12 --c-ink-3", () => {
    expect(on(DIALOGS, ".settings-dialog:not(.settings-dialog--drawer) > .settings-dialog__body")).toEqual({ gap: "12px" });
    expect(on(DIALOGS, ".legacy-tools-dialog-lead")).toMatchObject({ margin: "0", color: "var(--c-ink-2)", "font-size": "var(--fs-md)" });
    expect(on(DIALOGS, ".legacy-tools-dialog-note")).toMatchObject({ margin: "0", color: "var(--c-ink-3)", "font-size": "var(--fs-sm)" });
  });

  it("选择卡：发丝线控件描边、--r-3、悬停填充底", () => {
    expect(on(DIALOGS, ".legacy-ai-tools-picker-option")).toMatchObject({
      border: "0",
      "border-radius": "var(--r-3)",
      "box-shadow": "0 0 0 var(--hairline) var(--c-control-line)",
    });
    expect(on(DIALOGS, ".legacy-ai-tools-picker-option:hover")).toEqual({ background: "var(--c-fill-1)" });
  });

  it("应用位置抽屉：实底、左侧 --r-4、标题 15/650、行最小 44", () => {
    expect(on(DIALOGS, ".legacy-usage-drawer")).toMatchObject({
      border: "0",
      "border-radius": "var(--r-4) 0 0 var(--r-4)",
      background: "var(--c-surface)",
      "box-shadow": "var(--shadow-overlay)",
    });
    expect(on(DIALOGS, ".legacy-usage-drawer-title")).toMatchObject({ "font-size": "var(--fs-dialog-title)", "font-weight": "650" });
    expect(on(DIALOGS, ".legacy-usage-row")).toMatchObject({ "min-height": "44px" });
  });

  it("定时任务抽屉：只作用于独立抽屉（.routine-drawer），对话里嵌入的同一表单不受影响", () => {
    const scoped = DIALOGS.filter((r) => /legacy-sched-/.test(r.selector));
    expect(scoped.length).toBeGreaterThan(20);
    expect(scoped.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !/\.routine-drawer/.test(sel))).toEqual([]);
  });

  it("定时任务抽屉：输入 28 高、标签 12/600、预设选中为强调色描边 + 浅强调底、单选 5px 内环", () => {
    expect(
      on(DIALOGS, ".routine-drawer :is(.legacy-sched-inp, .legacy-sched-inp-textarea, .legacy-sched-inp-mono, .legacy-sched-select)"),
    ).toMatchObject({ "min-height": "var(--control-h-md)", "border-radius": "var(--r-2)" });
    expect(on(DIALOGS, ".routine-drawer .legacy-sched-field-label")).toMatchObject({
      color: "var(--c-ink-2)",
      "font-size": "var(--fs-sm)",
      "font-weight": "600",
    });
    expect(on(DIALOGS, ".routine-drawer .legacy-sched-preset-btn.is-on")).toMatchObject({
      background: "var(--c-accent-soft)",
      "box-shadow": "inset 0 0 0 1px var(--c-accent)",
    });
    expect(on(DIALOGS, ".routine-drawer .legacy-sched-radio.is-on")).toEqual({ "box-shadow": "inset 0 0 0 5px var(--c-accent)" });
  });

  it("分享名单头：计数与权限说明之间有间距、窄屏可换行（手机上不再挤成「3权限」）", () => {
    expect(on(DIALOGS, ".legacy-share-roster-head")).toMatchObject({ "column-gap": "8px", "flex-wrap": "wrap" });
  });

  it("额度弹窗字段标签去掉等宽大写", () => {
    expect(on(DIALOGS, ".legacy-credits-field-label")).toMatchObject({
      "font-family": "var(--font-control)",
      "text-transform": "none",
      "letter-spacing": "normal",
      "font-weight": "600",
    });
  });
});

describe("智能体页卡片", () => {
  it("一级卡片：内容面、--r-4、发丝线、不投影、不上浮", () => {
    for (const sel of [".am-card", ".am-featured-card", ".am-management-row", ".am-release-section", ".am-review-card"]) {
      expect(on(AGENTS, sel), sel).toMatchObject({
        border: "0",
        "border-radius": "var(--r-4)",
        background: "var(--c-surface)",
        "box-shadow": "0 0 0 var(--hairline) var(--c-separator)",
        transform: "none",
      });
    }
    expect(on(AGENTS, ":is(.am-card, .am-featured-card):hover")).toMatchObject({ transform: "none" });
  });

  it("次级块：填充底、--r-3", () => {
    for (const sel of [".am-trust-panel", ".am-fork-lineage-card", ".am-review-facts > div"]) {
      expect(on(AGENTS, sel), sel).toMatchObject({ border: "0", "border-radius": "var(--r-3)", background: "var(--c-fill-1)" });
    }
  });

  it("没有小于 11px 的字号", () => {
    const sizes = AGENTS.flatMap((r) => (decls(r.body)["font-size"] ? [decls(r.body)["font-size"]] : []));
    // Literal sizes are rem (16px root) or px; both are compared in px at the default root.
    const px = (s: string) => Number.parseFloat(s) * (s.endsWith("rem") ? 16 : 1);
    expect(sizes.filter((s) => /^\d/.test(s) && px(s) < 11)).toEqual([]);
  });
});

describe("工具页手机卡片（< 640）", () => {
  it("the original !important rules stay as the base layer", () => {
    expect(read("styles/skills-settings.css")).toContain(
      '.tool-source-card-actions{display:grid!important;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"manage manage" "availability expand";align-items:center!important;align-self:stretch!important;gap:10px 8px!important;',
    );
  });

  it("actions in one row (toggle · ⋯ · expand); the !important gap is overridden with !important too", () => {
    const actions = on(TOOLS_MOBILE, ".ai-tools-card .tool-source-card-actions", /max-width: 640px/);
    expect(actions).toMatchObject({
      "grid-template-areas": '"availability manage expand"',
      gap: "4px !important",
      "border-top": "var(--hairline) solid var(--c-separator)",
    });
    expect(TOOLS_MOBILE.every((r) => /max-width: 640px/.test(r.media ?? ""))).toBe(true);
  });
});
