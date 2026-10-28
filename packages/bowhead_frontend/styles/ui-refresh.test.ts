/**
 * Interface appearance source facts for batch 1 (buttons/inputs) and batch 2 (selection controls/badges).
 *
 * jsdom does not cascade; pin source-verifiable contracts such as every interface rule starting with the :root:root prefix and all new
 * colors using tokens. Real-browser screenshots validate pixels.
 *
 *   1. Every selector of every styles/ui-refresh.css rule starts with :root:root
 *      (including @media rules).
 *   2. Except token definitions on the :root:root token blocks, no rule value uses literal colors (#hex / rgb / hsl);
 *      hard-coded colors prevent buttons/inputs following team themes or light/dark mode changes.
 *   3. Imported last only by authenticated (app) layout; public layout does not import it.
 *   4. Base classes moved from inline styles and used in TSX all have CSS definitions.
 *   5. Batch 2: switches/tags/segments/card radios/badges are mapped; badge tone variables use only tokens;
 *      CSS Module interface rules start with :global(:root:root).
 */
import { afterEach, describe, expect, it } from "vitest";
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

type Rule = { selector: string; body: string };

/** Extract ordinary stylesheet rules; recurse through `@media` / @supports and skip `@keyframes`. */
function parseRules(css: string): Rule[] {
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
    if (/^@(media|supports)\b/.test(prelude)) rules.push(...parseRules(body));
    else if (!prelude.startsWith("@")) rules.push({ selector: prelude, body });
    i = close + 1;
  }
  return rules;
}

/** Split selectors at top-level commas, ignoring commas within :is() / :not(). */
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

const REFRESH_CSS = read("styles/ui-refresh.css");
const RULES = parseRules(stripComments(REFRESH_CSS));
/** Token definition block on :root:root only (theme/radius/density attributes allowed; no descendant selectors). */
const isTokenRoot = (sel: string) => /^:root:root(\[[^\]]+\])*$/.test(sel);

describe("styles/ui-refresh.css: every rule starts with the :root:root prefix", () => {
  it("解析出按钮与输入控件的规则", () => {
    const all = RULES.map((rule) => rule.selector).join("\n");
    expect(RULES.length).toBeGreaterThan(20);
    for (const family of [".settings-button", ".kb-btn", ".sset .btn", ".v2-icon-btn", ".settings-input", ".ui-input", ".f-search"]) {
      expect(all, family).toContain(family);
    }
  });

  it("解析出第二批选择控件与徽标的规则", () => {
    const all = RULES.map((rule) => rule.selector).join("\n");
    for (const family of [
      ".ui-switch",
      ".memory-v2-switch",
      'input:is([type="checkbox"], [type="radio"])',
      ".ui-radio-cards",
      ".ui-segmented",
      ".ui-tabs--segmented",
      ".ui-tabs--underline",
      ".memory-v2-clear-trash",
      ".ui-badge",
      ".team-role-badge",
      ".kb-pill",
      ".cbadge",
      ".legacy-badge-sched",
      ".ui-status-dot",
      ".sset .fchip",
      ".member-cell__avatar",
    ]) {
      expect(all, family).toContain(family);
    }
  });

  it("every selector starts with :root:root", () => {
    const offenders = RULES.flatMap((rule) => splitSelectors(rule.selector))
      .filter((sel) => !sel.startsWith(PREFIX));
    expect(offenders).toEqual([]);
  });

  it("no literal colours outside the :root:root token blocks", () => {
    const literal = /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?)\(/i;
    const offenders = RULES
      .filter((rule) => !splitSelectors(rule.selector).every(isTokenRoot))
      .flatMap((rule) =>
        rule.body
          .split(";")
          .map((decl) => decl.trim())
          .filter((decl) => literal.test(decl))
          .map((decl) => `${splitSelectors(rule.selector)[0]} { ${decl} }`),
      );
    expect(offenders).toEqual([]);
  });

  it("开关、复选框、分段、徽标按规格稿的尺寸", () => {
    const body = (selector: string) => {
      const rule = RULES.find((r) => splitSelectors(r.selector).includes(selector));
      expect(rule, selector).toBeDefined();
      return rule!.body.replace(/\s+/g, " ");
    };
    // Compare declarations ("min-width: 34px" cannot substitute for "width: 34px").
    const has = (text: string, prop: string, value: string) =>
      expect(new RegExp(`(?:^|;)\\s*${prop}: ${value.replace(/[()]/g, "\\$&")};`).test(text), `${prop}: ${value}`).toBe(true);
    const sw = body(`${PREFIX} .ui-switch[role="switch"]`);
    has(sw, "width", "34px");
    has(sw, "height", "20px");
    has(sw, "border-radius", "10px");
    const thumb = body(`${PREFIX} .ui-switch[role="switch"] > :first-child`);
    has(thumb, "width", "16px");
    has(thumb, "height", "16px");
    expect(body(`${PREFIX} .ui-switch[role="switch"][aria-checked="true"] > :first-child`)).toContain("translateX(14px)");
    const box = body(`${PREFIX} input:is([type="checkbox"], [type="radio"])`);
    has(box, "width", "16px");
    has(box, "border-radius", "4px");
    expect(body(`${PREFIX} input[type="radio"]:checked`)).toContain("inset 0 0 0 5px var(--c-accent)");
    const badge = RULES.find((r) => r.selector.includes(".ui-badge,") && r.body.includes("--ui-badge-h"));
    const badgeBody = (badge?.body ?? "").replace(/\s+/g, " ");
    has(badgeBody, "height", "auto");
    has(badgeBody, "min-height", "var(--ui-badge-min-h, var(--ui-badge-h, 20px))");
    has(badgeBody, "font-size", "var(--ui-badge-fs, 0.71875rem)");
    has(badgeBody, "border-radius", "999px");
  });

  it("徽标色调变量都来自主题 token", () => {
    const decls = RULES.flatMap((rule) =>
      Array.from(rule.body.matchAll(/--ui-badge-(bg|fg|ring)\s*:\s*([^;]+)/g), (m) => [m[1], m[2].trim()] as const),
    );
    expect(decls.length).toBeGreaterThan(20);
    for (const [kind, value] of decls) {
      const ok =
        kind === "ring"
          ? value === "none" || /^inset 0 0 0 0\.5px var\(--c-[\w-]+\)$/.test(value)
          : value === "transparent" || /^var\(--c-[\w-]+\)$/.test(value);
      expect(ok, `--ui-badge-${kind}: ${value}`).toBe(true);
    }
  });

  it("浅底色调徽标用向正文色混过的文字色（11px 小字在浅色主题也够 4.5:1）", () => {
    const FG_FOR_SOFT: Record<string, string> = {
      "--c-accent-soft": "var(--c-accent-text)",
      "--c-pos-soft": "var(--c-pos-ink)",
      "--c-warn-soft": "var(--c-warn-ink)",
      "--c-err-soft": "var(--c-err-ink)",
    };
    const tones = RULES.filter((rule) => /--ui-badge-bg:\s*var\(--c-(accent|pos|warn|err)-soft\)/.test(rule.body));
    expect(tones.length).toBeGreaterThanOrEqual(4);
    for (const rule of tones) {
      const bg = rule.body.match(/--ui-badge-bg:\s*var\((--c-[\w-]+)\)/)![1];
      const fg = rule.body.match(/--ui-badge-fg:\s*([^;]+)/)?.[1].trim();
      expect(fg, splitSelectors(rule.selector)[0]).toBe(FG_FOR_SOFT[bg]);
    }
    const tokenRoot = RULES.find((rule) => rule.selector.trim() === PREFIX && rule.body.includes("--c-accent-text"));
    expect(tokenRoot?.body).toMatch(/--c-accent-text:\s*color-mix\(in srgb, var\(--c-accent\) \d+%, var\(--c-ink\)\)/);
  });

  it("材质（菜单、Toast）上的文字不用纯强调色：最坏后方下不到 4.5:1，改用 --c-accent-text", () => {
    const onMaterial = RULES.filter((rule) => /\.(ui-menu|rho-toast)\b/.test(rule.selector));
    expect(onMaterial.length).toBeGreaterThan(5);
    const offenders = onMaterial
      .filter((rule) => /(^|[;\s])color:\s*var\(--c-accent\)/.test(rule.body))
      .map((rule) => splitSelectors(rule.selector)[0]);
    expect(offenders).toEqual([]);
    const action = RULES.find((rule) => rule.selector.includes(".rho-toast-action") && !rule.selector.includes(":hover"));
    expect(action?.body).toMatch(/color:\s*var\(--c-accent-text\)/);
  });

  it("按钮变体的颜色都来自主题 token", () => {
    const variantVars = RULES.flatMap((rule) =>
      Array.from(rule.body.matchAll(/--ui-btn-(?:bg|fg)[\w-]*\s*:\s*([^;]+)/g), (m) => m[1].trim()),
    );
    expect(variantVars.length).toBeGreaterThan(10);
    for (const value of variantVars) {
      expect(value === "transparent" || /^var\(--c-[\w-]+\)$/.test(value), value).toBe(true);
    }
  });
});

/** Top-level @media / @supports blocks: [prelude, contained rules]. */
function mediaBlocks(css: string): { prelude: string; rules: Rule[] }[] {
  const out: { prelude: string; rules: Rule[] }[] = [];
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
    if (/^@(media|supports)\b/.test(prelude)) out.push({ prelude, rules: parseRules(css.slice(open + 1, close)) });
    i = close + 1;
  }
  return out;
}

describe("第三批：浮层（菜单、弹窗、抽屉、Toast、Tooltip）", () => {
  const ITEM = ':is([role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"], [role="option"])';
  const norm = (text: string) => text.replace(/\s+/g, " ").trim();
  const ruleFor = (selector: string) => {
    const rule = RULES.find((r) => splitSelectors(r.selector).includes(norm(selector)));
    expect(rule, selector).toBeDefined();
    return norm(rule!.body);
  };
  const has = (text: string, prop: string, value: string) =>
    expect(new RegExp(`(?:^|;)\\s*${prop}: ${value.replace(/[()]/g, "\\$&")};`).test(text), `${prop}: ${value}`).toBe(true);
  const tokenRoot = RULES.filter((r) => r.selector.trim() === PREFIX).map((r) => norm(r.body)).join(" ");

  it("菜单、弹窗、抽屉、Toast、Tooltip 的家族都映射进来", () => {
    const all = RULES.map((rule) => norm(rule.selector)).join("\n");
    for (const family of [
      `${PREFIX} .ui-menu`,
      `.ui-menu ${ITEM}`,
      ".v2-user-menu-label",
      ".legacy-menu-model-group-title",
      ".composer-v2__planning-item",
      ".legacy-menu-space-item",
      ".legacy-menu-space-search",
      ".unified-work-sr-only",
      ".settings-dialog",
      ".kb-modal",
      ".mset-modal",
      ".sset .modal",
      ".files-main .modal",
      ".memory-modal-box",
      ".v29-modal",
      ".personal-mcp-confirmation",
      ".settings-dialog-backdrop",
      ".settings-dialog--drawer",
      ".kb-drawer-panel",
      ".rc-exports-drawer",
      ".review-panel",
      ".sset .drawer",
      ".rho-toast",
      ".legacy-toast-container",
      ".unified-sidebar-tooltip",
      ".composer-v2__tooltip",
    ]) {
      expect(all, family).toContain(family);
    }
  });

  it("菜单按规格稿的尺寸：行高 32（触屏 44）、容器内边距 6 圆角 12、最小宽 220、分组标题与分隔线", () => {
    expect(tokenRoot).toContain("--menu-item-h: 32px;");
    const coarse = mediaBlocks(stripComments(REFRESH_CSS)).filter((m) => m.prelude.includes("pointer: coarse"));
    expect(coarse.some((m) => m.rules.some((r) => /--menu-item-h:\s*var\(--control-h-touch\)/.test(r.body)))).toBe(true);
    const menu = ruleFor(`${PREFIX} .ui-menu`);
    has(menu, "padding", "6px");
    has(menu, "border-radius", "12px");
    // Minimum 220; yield when available width (--menu-available-width set by useMenu) is narrower, avoiding viewport overflow.
    has(menu, "min-width", "min(220px, var(--menu-available-width, 220px))");
    // Exception (user decision 2026-09-26): selection-toolbar font dropdown stays compact, retaining the legacy minimum width 100.
    has(ruleFor(`${PREFIX} .ui-menu.legacy-menu-font`), "min-width", "min(100px, var(--menu-available-width, 100px))");
    const item = ruleFor(`${PREFIX} .ui-menu ${ITEM}`);
    has(item, "min-height", "var(--menu-item-h)");
    has(item, "font-size", "var(--fs-md)");
    has(item, "padding", "0 10px 0 var(--ui-menu-item-pl)");
    has(item, "background", "transparent");
    const group = RULES.find((r) => r.selector.includes(".v2-user-menu-label") && r.selector.includes(".legacy-menu-model-group-title"));
    const groupBody = norm(group?.body ?? "");
    has(groupBody, "padding", "6px 10px 2px");
    has(groupBody, "font-size", "var(--fs-xs)");
    has(groupBody, "font-weight", "600");
    has(groupBody, "color", "var(--c-ink-3)");
    const sep = norm(RULES.find((r) => r.selector.includes(".kb-pop-sep"))?.body ?? "");
    has(sep, "height", "var(--hairline)");
    has(sep, "margin", "4px 8px");
    has(sep, "background", "var(--c-separator)");
  });

  it("高亮与选中分开：高亮是强调色整行填充，选中是行首勾；选中规则不改底色", () => {
    const highlight = RULES.find(
      (r) => r.selector.includes(":hover") && r.selector.includes(":focus") && r.selector.includes(".ui-menu") &&
        !r.selector.includes("danger") && !r.selector.includes("v2-user-menu") && /background:\s*var\(--c-accent\)/.test(r.body),
    );
    expect(highlight, "highlight rule").toBeDefined();
    expect(highlight!.selector).not.toContain("aria-checked");
    expect(norm(highlight!.body)).toContain("color: var(--c-accent-ink);");
    const checked = RULES.find((r) => r.selector.includes('[aria-checked="true"]') && r.selector.includes("::before"));
    expect(checked, "checked marker rule").toBeDefined();
    expect(norm(checked!.body)).toBe("opacity: 1;");
    const marker = RULES.find((r) => r.selector.includes('[role="menuitemradio"]') && r.selector.trim().endsWith("::before") && /mask:/.test(r.body));
    expect(marker, "marker column rule").toBeDefined();
    has(norm(marker!.body), "flex", "0 0 18px");
    has(norm(marker!.body), "opacity", "0");
  });

  it("材质上的次级文字最浅 --c-ink-3（不用 --c-ink-4 / --c-warn）", () => {
    const onMaterial = RULES.filter((r) => /\.ui-menu|\.rho-toast|\.legacy-toast/.test(r.selector));
    expect(onMaterial.length).toBeGreaterThan(20);
    for (const r of onMaterial) {
      expect(r.body, r.selector).not.toMatch(/color:\s*var\(--c-ink-4\)/);
      expect(r.body, r.selector).not.toMatch(/(?:^|[^-])color:\s*var\(--c-warn\)/);
    }
  });

  it("模型选择：过期提示 + 「使用团队默认」为选中项时，默认项的 ::before 仍是勾，分隔线由过期提示自己画", () => {
    // No rule ending with ::before and matching legacy-menu-model-item may replace the checkmark with a separator.
    const offenders = RULES.flatMap((r) => splitSelectors(r.selector))
      .filter((sel) => sel.endsWith("::before") && /legacy-menu-model-item(?![\w-])/.test(sel.replace(/legacy-menu-model-item--default \+/g, "")));
    expect(offenders).toEqual([]);
    const stale = ruleFor(`${PREFIX} .ui-menu .legacy-menu-model-stale::after`);
    has(stale, "height", "var(--hairline)");
    has(stale, "background", "var(--c-separator)");
    // jsdom: use actual DOM structure to check default item matches the leading-checkmark rule, not the separator rule.
    document.body.innerHTML =
      '<div class="ui-menu composer-v2__model-menu legacy-menu-model" role="menu">' +
      '<div class="legacy-menu-model-stale">stale</div>' +
      '<button role="menuitemradio" aria-checked="true" data-active="true" class="composer-v2__model-menu-item legacy-menu-model-item legacy-menu-model-item--default"><span class="legacy-menu-model-mark">已选</span><span>默认</span></button>' +
      '<div class="legacy-menu-model-group"><div class="legacy-menu-model-group-title">ds</div></div></div>';
    const item = document.querySelector('[role="menuitemradio"]') as HTMLElement;
    const pseudoTargets = (pred: (r: Rule) => boolean) =>
      RULES.filter(pred).flatMap((r) => splitSelectors(r.selector)).filter((sel) => sel.endsWith("::before")).map((sel) => sel.slice(0, -"::before".length));
    const checkSelectors = pseudoTargets((r) => norm(r.body) === "opacity: 1;");
    const separatorSelectors = pseudoTargets((r) => r.body.includes("--c-separator"));
    expect(checkSelectors.some((sel) => item.matches(sel))).toBe(true);
    expect(separatorSelectors.filter((sel) => item.matches(sel))).toEqual([]);
    document.body.innerHTML = "";
  });

  it("危险项红字，「退出登录」普通文字色；禁用项 ink3", () => {
    const danger = RULES.find((r) => r.selector.includes(".is-danger") && r.selector.includes('[data-danger="true"]') && !r.selector.includes(":hover"));
    expect(norm(danger?.body ?? "")).toBe("color: var(--c-err);");
    expect(ruleFor(`${PREFIX} .ui-menu.v2-user-menu .v2-user-menu-item.is-danger`)).toBe("color: var(--c-ink);");
    const disabled = RULES.find((r) => r.selector.includes(".ui-menu") && r.selector.includes(":disabled") && r.selector.includes('[aria-disabled="true"]') && /color:\s*var\(--c-ink-3\)/.test(r.body) && !r.selector.includes("sr-only"));
    expect(disabled, "disabled item rule").toBeDefined();
  });

  it("材质只给菜单和 Toast；浅色 88%、深色 96%；「减少透明度」、高对比度、不支持背景模糊时退回实底", () => {
    expect(tokenRoot).toContain("--ui-material-bg: color-mix(in srgb, var(--c-surface) 88%, transparent);");
    const dark = RULES.find((r) => r.selector.trim() === `${PREFIX}[data-theme="dark"]` && r.body.includes("--ui-material-bg"));
    expect(norm(dark?.body ?? "")).toContain("--ui-material-bg: color-mix(in srgb, var(--c-surface) 96%, transparent);");
    expect(tokenRoot).toMatch(/--ui-material-filter: blur\(24px\) saturate\(1\.4\);/);
    for (const selector of [`${PREFIX} .ui-menu`, `${PREFIX} .rho-toast`]) {
      const body = ruleFor(selector);
      has(body, "background", "var(--ui-material-bg)");
      has(body, "backdrop-filter", "var(--ui-material-filter)");
      has(body, "-webkit-backdrop-filter", "var(--ui-material-filter)");
    }
    const reduce = mediaBlocks(stripComments(REFRESH_CSS)).find((m) => m.prelude.includes("prefers-reduced-transparency: reduce"));
    expect(reduce, "reduced-transparency block").toBeDefined();
    expect(reduce!.prelude).toContain("prefers-contrast: more");
    const body = norm(reduce!.rules.map((r) => r.body).join(";"));
    expect(body).toContain("--ui-material-bg: var(--c-surface);");
    expect(body).toContain("--ui-material-filter: none;");
    const unsupported = mediaBlocks(stripComments(REFRESH_CSS)).find((m) => /^@supports not/.test(m.prelude) && m.prelude.includes("backdrop-filter"));
    expect(unsupported, "@supports not (backdrop-filter) block").toBeDefined();
    const fallback = norm(unsupported!.rules.map((r) => r.body).join(";"));
    expect(fallback).toContain("--ui-material-bg: var(--c-surface);");
    // Fallback must override dark mode's 96%: selector list includes the dark root.
    for (const block of [reduce!, unsupported!]) {
      expect(block.rules.flatMap((r) => splitSelectors(r.selector))).toContain(`${PREFIX}[data-theme="dark"]`);
    }
    // Dialogs/drawers have solid backgrounds, without material effects.
    for (const r of RULES.filter((rule) => /\.settings-dialog\b|\.kb-drawer-panel/.test(rule.selector))) {
      expect(r.body, r.selector).not.toContain("--ui-material");
    }
  });

  it("弹窗与抽屉：实底、圆角 r4、标题 15/650、底栏右对齐 gap 8；遮罩统一 --c-scrim", () => {
    const panel = RULES.find((r) => r.selector.includes(".settings-dialog,") && r.selector.includes(".kb-modal"));
    const panelBody = norm(panel?.body ?? "");
    has(panelBody, "background", "var(--c-surface)");
    has(panelBody, "border-radius", "var(--r-4)");
    has(panelBody, "box-shadow", "var(--shadow-overlay)");
    const title = norm(RULES.find((r) => r.selector.includes(".settings-dialog__title,"))?.body ?? "");
    has(title, "font-size", "var(--fs-dialog-title)");
    has(title, "font-weight", "650");
    const foot = norm(RULES.find((r) => r.selector.includes(".settings-dialog__footer,") && r.selector.includes(".v29-modal-foot"))?.body ?? "");
    has(foot, "justify-content", "flex-end");
    has(foot, "gap", "8px");
    const scrim = norm(RULES.find((r) => r.selector.includes(".settings-dialog-backdrop,"))?.body ?? "");
    has(scrim, "background", "var(--c-scrim)");
    const drawer = norm(RULES.find((r) => r.selector.includes(".settings-dialog--drawer,") && r.selector.includes(".kb-drawer-panel"))?.body ?? "");
    has(drawer, "border-radius", "var(--r-4) 0 0 var(--r-4)");
    has(drawer, "background", "var(--c-surface)");
  });

  it("Toast 圆角 r3、手机置顶；Tooltip 12px、最宽 240、圆角 7", () => {
    const toast = ruleFor(`${PREFIX} .rho-toast`);
    has(toast, "border-radius", "var(--r-3)");
    has(toast, "font-size", "var(--fs-md)");
    const phone = mediaBlocks(stripComments(REFRESH_CSS)).find((m) => m.prelude.includes("max-width: 639px"));
    const container = phone?.rules.find((r) => r.selector.includes(".legacy-toast-container"));
    expect(norm(container?.body ?? "")).toMatch(/left: 10px;.*right: 10px;|right: 10px;.*left: 10px;/);
    const tip = norm(RULES.find((r) => r.selector.includes(".unified-sidebar-tooltip,") && r.selector.includes(".composer-v2__tooltip"))?.body ?? "");
    has(tip, "max-width", "240px");
    has(tip, "padding", "6px 9px");
    has(tip, "border-radius", "7px");
    has(tip, "font-size", "var(--fs-sm)");
    has(tip, "background", "var(--c-tooltip)");
  });
});

describe("引入位置", () => {
  it("keeps the ordered authenticated styles starting with ui-controls-legacy.css", () => {
    const layout = read("app/(app)/layout.tsx");
    const imports = Array.from(layout.matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    // Batch 5 ui-refresh-surfaces.css follows ui-refresh.css immediately (styles/ui-refresh-surfaces.test.ts);
    // page acceptance A/B ui-refresh-pages-settings.css, ui-refresh-pages-admin.css, ui-refresh-pages-main.css come last (their respective tests);
    // canvas batch 3 ui-refresh-canvas.css and workbench batch 2 ui-refresh-workbench.css follow those (their respective tests);
    // P2 batch 1 ui-refresh-materials.css comes last (styles/ui-refresh-materials.test.ts).
    const expectedImports = [
      "../../styles/ui-controls-legacy.css",
      "../../styles/ui-refresh.css",
      "../../styles/ui-refresh-surfaces.css",
      "../../styles/ui-refresh-pages-settings.css",
      "../../styles/ui-refresh-pages-admin.css",
      "../../styles/ui-refresh-pages-main.css",
      "../../styles/ui-refresh-canvas.css",
      "../../styles/ui-refresh-workbench.css",
      "../../styles/ui-refresh-materials.css",
    ];
    const start = imports.indexOf(expectedImports[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(imports.slice(start, start + expectedImports.length)).toEqual(expectedImports);
  });

  it("公共布局不引入", () => {
    const layout = read("app/(public)/layout.tsx");
    expect(layout).not.toMatch(/ui-refresh\.css|ui-refresh-surfaces\.css|ui-controls-legacy\.css/);
  });
});

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "node_modules" ? [] : tsxFiles(path);
    return name.endsWith(".tsx") && !name.endsWith(".test.tsx") ? [path] : [];
  });
}

describe("base-layer classes moved from inline styles", () => {
  const legacyCss = stripComments(read("styles/ui-controls-legacy.css"));
  const LEGACY = /(?:legacy-(?:input|switch|badge|kb-outer|menu|toast)|credits-btn|report-edit-btn)[\w-]*/;
  // Count only base rules ending in this class (.x { or .x,); modifier-only rules (.x.is-on) do not define the original appearance.
  const defined = new Set(Array.from(legacyCss.matchAll(new RegExp(`\\.(${LEGACY.source})\\s*[{,]`, "g")), (m) => m[1]));
  const used = new Set(
    [...tsxFiles(join(FRONTEND, "components")), ...tsxFiles(join(FRONTEND, "app"))].flatMap((file) =>
      Array.from(readFileSync(file, "utf8").matchAll(new RegExp(`\\b(${LEGACY.source})`, "g")), (m) => m[1]),
    ),
  );

  it("TSX 里用到的都有定义", () => {
    expect(used.size).toBeGreaterThan(5);
    for (const second of [
      "legacy-switch-sched",
      "legacy-switch-tools",
      "legacy-badge-tool-kind",
      // Batch 3: inline menu/Toast styles.
      "legacy-menu-model-item",
      "legacy-menu-space-item",
      "legacy-menu-credits-item",
      "legacy-menu-font-item",
      "legacy-toast",
    ]) {
      expect(used.has(second), second).toBe(true);
    }
    expect([...used].filter((name) => !defined.has(name))).toEqual([]);
  });

  it("the base-layer file carries no :root:root prefix", () => {
    expect(legacyCss).not.toContain(":root:root");
  });

  it("moved inputs carry .ui-input", () => {
    const refreshSelectors = RULES.map((rule) => rule.selector).join("\n");
    expect(refreshSelectors).toContain(".ui-input");
    expect(refreshSelectors).toContain(".credits-btn");
    for (const file of [...tsxFiles(join(FRONTEND, "components"))]) {
      for (const m of readFileSync(file, "utf8").matchAll(/className="([^"]*legacy-input-[^"]*)"/g)) {
        expect(m[1].split(" "), file).toContain("ui-input");
      }
    }
  });

  describe("危险按钮：只有弹窗底栏里的确认动作是实心红，其余是红字文字按钮（已定取舍第 3、23 条）", () => {
    const rules = parseRules(stripComments(read("styles/ui-refresh.css")));
    const danger = rules.filter((r) => r.selector.includes(".settings-button--danger"));
    const textRule = danger.find((r) => !r.selector.includes('[role="dialog"]'));
    const solidRule = danger.find((r) => r.selector.includes('[role="dialog"]') && r.selector.includes('[role="alertdialog"]'));

    /** Build a fragment matching the real caller DOM; return the [data-probe] button's appearance. */
    function lookOf(html: string): "solid" | "text" | "none" {
      document.body.innerHTML = html;
      const button = document.querySelector("[data-probe]") as HTMLElement;
      // Solid rule has greater specificity than red-text rule; matching both yields solid appearance.
      if (solidRule && button.matches(solidRule.selector)) return "solid";
      if (textRule && button.matches(textRule.selector)) return "text";
      return "none";
    }

    afterEach(() => {
      document.body.innerHTML = "";
    });

    it("两条规则的取值：红字无底色 / 实心红", () => {
      expect(textRule?.body).toMatch(/--ui-btn-bg:\s*transparent/);
      expect(textRule?.body).toMatch(/--ui-btn-fg:\s*var\(--c-err\)/);
      expect(solidRule?.body).toMatch(/--ui-btn-bg:\s*var\(--c-err\)/);
      expect(solidRule?.body).toMatch(/--ui-btn-fg:\s*var\(--c-err-on\)/);
    });

    it.each([
      ["Dialog / ConfirmDialog 底栏", '<div role="dialog" class="settings-dialog"><div class="settings-dialog__footer"><button class="settings-button settings-button--secondary"></button><button data-probe class="settings-button settings-button--danger"></button></div></div>'],
      ["技能设置自写确认框 .modal-foot", '<div class="sset"><div role="dialog" class="modal"><div class="modal-foot"><button data-probe class="btn danger sm"></button></div></div></div>'],
      ["模型设置删除提供方 .mset-modal .modal-foot", '<div class="mset"><div class="mset-scrim"><div role="dialog" class="mset-modal"><div class="modal-foot"><button data-probe class="btn danger sm"></button></div></div></div></div>'],
      ["文件页确认框 .modal .mrow", '<div class="files-main"><div class="scrim"><div role="dialog" class="modal"><div class="mrow"><button data-probe class="btn danger"></button></div></div></div></div>'],
      ["额度重置 .credits-modal-foot", '<div role="dialog"><div class="credits-modal-foot"><button data-probe class="credits-btn credits-btn--danger"></button></div></div>'],
      ["知识库确认框 kb-btn-danger-solid", '<div class="kb-overlay"><div role="dialog" class="kb-modal"><div class="kb-modal-foot"><button data-probe class="kb-btn kb-btn-danger-solid"></button></div></div></div>'],
      ["知识库文件抽屉里的删除确认条 kb-btn-danger-solid", '<div role="dialog" class="kb-drawer-panel"><div class="confirm"><div class="r"><button data-probe class="kb-btn kb-btn-danger-solid kb-btn-sm"></button></div></div></div>'],
    ])("底栏确认是实心：%s", (_name, html) => {
      expect(lookOf(html)).toBe("solid");
    });

    it.each([
      ["知识库文件抽屉的「删除」入口（解析未完成）", '<div class="kb-drawer-overlay"><div role="dialog" class="kb-drawer-panel"><div class="dr-acts"><button data-probe class="kb-btn kb-btn-danger del"></button></div></div></div>'],
      ["知识库文件抽屉的「删除」入口（解析完成）", '<div class="kb-drawer-overlay"><div role="dialog" class="kb-drawer-panel"><div class="dr-acts"><button data-probe class="kb-btn kb-btn-danger del kb-btn-sm"></button></div></div></div>'],
      ["弹窗正文列表行上的「移除」（团队主题库）", '<div role="dialog" class="settings-dialog"><div class="settings-dialog__body"><ul><li><button data-probe class="settings-button settings-button--danger settings-button--sm"></button></li></ul></div></div>'],
      ["安全页会话「撤销」", '<section class="personal-security-card"><button data-probe class="settings-button settings-button--danger settings-button--sm personal-security-session__revoke"></button></section>'],
      ["页面上的危险按钮（邮件服务「清除配置」）", '<div><button data-probe class="settings-button settings-button--danger settings-button--md"></button></div>'],
      ["页面上的批量删除（知识库批量栏）", '<div class="kb-bulkbar"><button data-probe class="kb-btn kb-btn-danger kb-btn-sm"></button></div>'],
    ])("入口 / 行内动作是红字：%s", (_name, html) => {
      expect(lookOf(html)).toBe("text");
    });
  });
});

function cssFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "node_modules" ? [] : cssFiles(path);
    return name.endsWith(".module.css") ? [path] : [];
  });
}

describe("refreshed rules inside CSS Modules", () => {
  const MODULE_PREFIX = ":global(:root:root)";
  const modules = [...cssFiles(join(FRONTEND, "components")), ...cssFiles(join(FRONTEND, "app"))]
    .map((file) => ({ file, rules: parseRules(stripComments(readFileSync(file, "utf8"))) }))
    .filter(({ rules }) => rules.some((rule) => rule.selector.includes(MODULE_PREFIX)));

  it("batch 2 badge families (Feishu stage status, email service status) are mapped inside their modules", () => {
    const names = modules.map(({ file }) => file.split("/").pop());
    expect(names).toEqual(expect.arrayContaining(["integration-pages.module.css", "email-service.module.css"]));
  });

  it("every selector of a rule that uses the prefix starts with :global(:root:root), without literal colors", () => {
    const literal = /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?)\(/i;
    for (const { file, rules } of modules) {
      for (const rule of rules.filter((r) => r.selector.includes(MODULE_PREFIX))) {
        for (const sel of splitSelectors(rule.selector)) {
          expect(sel.startsWith(MODULE_PREFIX), `${file}: ${sel}`).toBe(true);
        }
        expect(literal.test(rule.body), `${file}: ${rule.selector}`).toBe(false);
      }
    }
  });
});

describe("第四批：页面结构与数据表格", () => {
  // Inspect only batch 4 (split by its opening banner); contracts such as separators avoiding --c-line apply only to this batch.
  const BATCH4_CSS = stripComments(REFRESH_CSS.slice(REFRESH_CSS.lastIndexOf("/*", REFRESH_CSS.indexOf("第四批：页面结构与数据表格"))));
  const BATCH4 = parseRules(BATCH4_CSS);
  const norm = (text: string) => text.replace(/\s+/g, " ").trim();
  const ruleFor = (selector: string) => {
    const rule = RULES.find((r) => splitSelectors(r.selector).includes(norm(selector)));
    expect(rule, selector).toBeDefined();
    return norm(rule!.body);
  };
  const has = (text: string, prop: string, value: string) =>
    expect(new RegExp(`(?:^|;)\\s*${prop}: ${value.replace(/[()+]/g, "\\$&")};`).test(text), `${prop}: ${value}`).toBe(true);
  const TABLE = `${PREFIX} .ui-table.ui-table`;

  it("页头、分区、表格、行尾「⋯」、分页、筛选栏、空状态、拖放区的家族都映射进来", () => {
    expect(BATCH4.length).toBeGreaterThan(40);
    const all = BATCH4.map((rule) => norm(rule.selector)).join("\n");
    for (const family of [
      ".settings-shell.is-settings-surface > .settings-content",
      '.settings-content-inner[data-ui-width="table"]',
      ".admin-config-page",
      "h1.ui-page-title",
      ".ui-page-desc",
      ".settings-people-hero__eyebrow",
      ".ui-section-title",
      ".ui-section-card",
      ".ui-table-frame",
      ".team-members-table-wrap",
      `${TABLE} th`,
      `${TABLE} td`,
      ".team-members-table",
      ".access-table.ui-table.ui-table",
      ".legacy-credits-user-row.is-removed",
      ".kb-th-row",
      ".kb-row",
      ".f-head",
      ".f-row",
      ".memory-v2-item",
      ".ui-row-more",
      ".kb-pagebtn",
      ".f-pager .pgb",
      ".ui-pager-btn",
      ".people-pagination",
      ".team-spaces-pagination",
      ".f-tools",
      ".ui-empty",
      ".team-members-empty",
      ".memory-v2-empty",
      ".kb-upload .drop",
      ".sset .dropzone",
    ]) {
      expect(all, family).toContain(family);
    }
  });

  it("token：行高随密度（控件高 + 16，舒适 44）、表头 32、触屏行 56；内容宽 880 / 1120；页边距 40 / 32 / 24 / 16", () => {
    const root = BATCH4.filter((r) => norm(r.selector) === PREFIX).map((r) => norm(r.body)).join(" ");
    has(root, "--table-row-h", "calc(var(--control-h-md) + 16px)");
    has(root, "--table-head-h", "32px");
    has(root, "--content-w-form", "880px");
    has(root, "--content-w-table", "1120px");
    has(root, "--page-gutter", "40px");
    const blocks = mediaBlocks(BATCH4_CSS);
    const gutter = (query: string) =>
      norm(blocks.find((b) => b.prelude.includes(query) && b.rules.some((r) => r.body.includes("--page-gutter")))?.rules[0].body ?? "");
    has(gutter("max-width: 1279.98px"), "--page-gutter", "32px");
    has(gutter("max-width: 1023.98px"), "--page-gutter", "24px");
    has(gutter("max-width: 767.98px"), "--page-gutter", "16px");
    const coarse = blocks.find((b) => b.prelude.includes("pointer: coarse") && b.rules.some((r) => r.body.includes("--table-row-h")));
    has(norm(coarse!.rules[0].body), "--table-row-h", "56px");
    has(ruleFor(`${PREFIX} .settings-shell .settings-content-inner`), "width", "min(var(--content-w-form), 100%)");
    has(ruleFor(`${PREFIX} .settings-shell .settings-content-inner[data-ui-width="table"]`), "width", "min(var(--content-w-table), 100%)");
  });

  it("页头 22/700、说明 13 ink3；分区标题 16/650；控件字体", () => {
    const title = ruleFor(`${PREFIX} h1.ui-page-title`);
    has(title, "font-size", "var(--fs-page-title)");
    has(title, "font-weight", "700");
    has(title, "font-family", "var(--font-control)");
    const desc = ruleFor(`${PREFIX} .ui-page-desc`);
    has(desc, "font-size", "var(--fs-md)");
    has(desc, "color", "var(--c-ink-3)");
    const section = ruleFor(`${PREFIX} .ui-section-title`);
    has(section, "font-size", "var(--fs-lg)");
    has(section, "font-weight", "650");
  });

  it("tables: 32 high 12/600 ink-3 headers on the content surface, 44 rows with hairlines, quiet hover, accent-tint selection, tabular numeric columns", () => {
    const th = ruleFor(`${TABLE} th`);
    has(th, "height", "var(--table-head-h)");
    has(th, "font-size", "0.75rem");
    has(th, "font-weight", "600");
    has(th, "color", "var(--c-ink-3)");
    has(th, "background", "var(--c-surface)");
    // The Files grid header matches (the knowledge-base header moved to the content surface in 1.5.0).
    const fileHead = ruleFor(`${PREFIX} .f-head`);
    has(fileHead, "background", "var(--c-surface)");
    has(fileHead, "font-size", "0.75rem");
    has(fileHead, "height", "max(var(--table-head-h), 2.4em)");
    const td = ruleFor(`${TABLE} td`);
    has(td, "height", "var(--table-row-h)");
    has(td, "border-top", "var(--hairline) solid var(--c-separator)");
    has(ruleFor(`${TABLE} tbody tr:hover`), "background", "var(--row-hover-bg)");
    has(BATCH4.filter((r) => norm(r.selector) === PREFIX).map((r) => norm(r.body)).join(" "), "--row-hover-bg", "color-mix(in srgb, var(--c-ink) 4%, transparent)");
    const selected = ruleFor(`${TABLE} tbody tr:is([aria-selected="true"], .is-selected, .row--selected)`);
    has(selected, "background", "var(--c-accent-tint)");
    has(selected, "--c-ink-4", "var(--c-ink-3)");
    has(ruleFor(`${TABLE} tbody tr:hover`), "--c-ink-4", "var(--c-ink-3)");
    const gridSelected = ruleFor(`${PREFIX} :is(.kb-row.sel, .kb-row.sel:hover, .f-row.sel-on)`);
    has(gridSelected, "background", "var(--c-accent-tint)");
    has(gridSelected, "--c-ink-4", "var(--c-ink-3)");
    // Knowledge/files div grid rows: hover background matches tables; raise inline secondary text one level too (otherwise measured below 4.5).
    const gridHover = ruleFor(`${PREFIX} :is(.kb-row, .f-row):hover`);
    has(gridHover, "background", "var(--row-hover-bg)");
    has(gridHover, "--c-ink-4", "var(--c-ink-3)");
    const num = ruleFor(`${TABLE} :is(th, td).ui-num`);
    has(num, "text-align", "right");
    has(num, "font-variant-numeric", "tabular-nums");
    // Div grid lists share row height/separators too.
    has(ruleFor(`${PREFIX} .kb-row`), "min-height", "var(--table-row-h)");
    has(ruleFor(`${PREFIX} .f-row`), "border-top", "var(--hairline) solid var(--c-separator)");
    // Frame: content surface, r4, hairline border; on narrow screens only the frame itself may scroll horizontally.
    const frame = ruleFor(`${PREFIX} .ui-table-frame`);
    has(frame, "border-radius", "var(--r-4)");
    has(frame, "box-shadow", "0 0 0 var(--hairline) var(--c-separator)");
    has(ruleFor(`${PREFIX} :is(.ui-table-frame, .team-members-table-wrap)`), "overflow-x", "auto");
  });

  it("--c-line 的两种用途拆开：本批分隔线只用 --c-separator，控件描边（拖放区）用 --c-control-line", () => {
    const offenders = BATCH4.filter((r) => /var\(--c-line(?:-2)?\)/.test(r.body)).map((r) => norm(r.selector));
    expect(offenders).toEqual([]);
    expect(BATCH4.filter((r) => r.body.includes("var(--c-separator)")).length).toBeGreaterThan(10);
    has(ruleFor(`${PREFIX} .kb-upload .drop`), "border", "1.5px dashed var(--c-control-line)");
  });

  it("行尾「⋯」：表格行里默认隐藏，悬停 / 键盘聚焦 / 菜单打开时出现，触屏常显", () => {
    has(ruleFor(`${PREFIX} tr .ui-row-more`), "opacity", "0");
    has(ruleFor(`${PREFIX} tr:is(:hover, :focus-within) .ui-row-more`), "opacity", "1");
    expect(RULES.some((r) => splitSelectors(r.selector).includes(`${PREFIX} tr .ui-row-more[aria-expanded="true"]`))).toBe(true);
    const touch = mediaBlocks(BATCH4_CSS).find((b) => b.prelude.includes("hover: none") && b.rules.some((r) => r.selector.includes(".ui-row-more")));
    expect(norm(touch!.rules.find((r) => r.selector.includes(".ui-row-more"))!.body)).toBe("opacity: 1;");
    // Card header: status badge and "⋯" grouped at right, rather than spread into the middle by space-between.
    const headEnd = ruleFor(`${PREFIX} .ui-card-head-end`);
    has(headEnd, "display", "flex");
    has(headEnd, "margin-left", "auto");
    has(headEnd, "flex-shrink", "0");
    const more = ruleFor(`${PREFIX} .ui-row-more`);
    has(more, "width", "var(--control-h-md)");
    has(more, "height", "var(--control-h-md)");
  });

  it("分页 28 高（控件高）、当前页强调色浅底；拖放区 r4、拖入时强调色边框 + 浅底；空状态居中的发丝线卡片", () => {
    const btn = ruleFor(`${PREFIX} :is(.kb-pagebtn, .f-pager .pgb, .ui-pager-btn)`);
    has(btn, "height", "var(--control-h-md)");
    has(btn, "min-width", "var(--control-h-md)");
    // Icon + label sit on one line (CharIconText renders an SVG and text; a grid stacked them).
    has(btn, "display", "inline-flex");
    has(ruleFor(`${PREFIX} .kb-pagebtn.active`), "background", "var(--c-accent-soft)");
    has(ruleFor(`${PREFIX} .kb-upload .drop`), "border-radius", "var(--r-4)");
    const over = ruleFor(`${PREFIX} .kb-upload .drop.active`);
    has(over, "border-color", "var(--c-accent)");
    has(over, "background", "var(--c-accent-soft)");
    has(ruleFor(`${PREFIX} :is(.ui-empty, .team-members-empty, .people-pending-empty, .memory-v2-empty)`), "text-align", "center");
  });

  it("every legacy- class used in TSX is defined in a stylesheet", () => {
    const defined = new Set(Array.from(allStylesheets().matchAll(/\.(legacy-[\w-]+)/g), (m) => m[1]));
    const used = new Set(
      [...tsxFiles(join(FRONTEND, "components")), ...tsxFiles(join(FRONTEND, "app"))].flatMap((file) =>
        // Count only className values, not strings such as route.type === "legacy-data".
        Array.from(readFileSync(file, "utf8").matchAll(/className=(?:"([^"]*)"|\{`([^`]*)`\})/g), (m) => m[1] ?? m[2])
          .flatMap((value) => Array.from(value.matchAll(/(?:^|\s)(legacy-[a-z0-9]+(?:-{1,2}[a-z0-9]+)*)/g), (m) => m[1])),
      ),
    );
    for (const name of ["legacy-members-cell", "legacy-credits-th", "legacy-credits-pagebtn", "legacy-scheduled-title"]) {
      expect(used.has(name), name).toBe(true);
    }
    expect([...used].filter((name) => !defined.has(name))).toEqual([]);
  });
});

describe("行为批 A：菜单的窄屏底部面板、子菜单、禁用项高亮、键盘提示条", () => {
  const norm = (text: string) => text.replace(/\s+/g, " ").trim();
  const ruleFor = (selector: string) => {
    const rule = RULES.find((r) => splitSelectors(r.selector).includes(norm(selector)));
    expect(rule, selector).toBeDefined();
    return norm(rule!.body);
  };
  const has = (text: string, prop: string, value: string) =>
    expect(new RegExp(`(?:^|;)\\s*${prop}: ${value.replace(/[()+]/g, "\\$&")};`).test(text), `${prop}: ${value}`).toBe(true);
  const ITEM = ':is([role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"], [role="option"])';

  it("面板宿主在浮层形态下不占盒子；底部面板贴底、留安全区、控件层材质；遮罩用 --c-scrim", () => {
    has(ruleFor(`${PREFIX} .ui-menu-sheet-host`), "display", "contents");
    const sheet = ruleFor(`${PREFIX} .ui-menu-sheet`);
    has(sheet, "position", "fixed");
    has(sheet, "bottom", "calc(8px + env(safe-area-inset-bottom, 0px))");
    has(sheet, "border-radius", "20px");
    has(sheet, "background", "var(--ui-material-bg)");
    has(ruleFor(`${PREFIX} .ui-menu-sheet-scrim`), "background", "var(--c-scrim)");
    has(ruleFor(`${PREFIX} .ui-menu-sheet-grab::before`), "width", "36px");
    has(ruleFor(`${PREFIX} .ui-menu-sheet-grab`), "touch-action", "none");
  });

  it("面板里的菜单不再定位（盖掉 floating-ui 的内联尺寸），行高 44、分组为 surface 圆角列表", () => {
    const menu = ruleFor(`${PREFIX} .ui-menu-sheet .ui-menu:is(.is-sheet, .is-drill)`);
    has(menu, "position", "static !important");
    has(menu, "max-height", "none !important");
    has(menu, "max-width", "none !important");
    const item = ruleFor(`${PREFIX} .ui-menu:is(.is-sheet, .is-drill) ${ITEM}`);
    has(item, "min-height", "44px");
    has(item, "background", "var(--c-surface)");
    has(ruleFor(`${PREFIX} .ui-menu-sheet[data-drilled="true"] .ui-menu.is-sheet`), "display", "none !important");
  });

  it("面板里只按焦点高亮：触屏轻点后粘住的 :hover 不画强调色", () => {
    const hover = ruleFor(`${PREFIX} .ui-menu:is(.is-sheet, .is-drill) ${ITEM}:hover:not(:focus)`);
    has(hover, "background", "var(--c-surface)");
    has(hover, "color", "var(--c-ink)");
  });

  it("禁用项被键盘高亮时是填充底，不是强调色；原因第二行常显", () => {
    const focused = ruleFor(`${PREFIX} .ui-menu ${ITEM}:is( :disabled, [aria-disabled="true"] ):focus`);
    has(focused, "background", "var(--c-fill-2)");
    expect(focused).not.toContain("--c-accent");
    const reason = ruleFor(`${PREFIX} .ui-menu .ui-menu-reason`);
    has(reason, "display", "block");
    has(reason, "color", "var(--c-ink-3)");
  });

  it("子菜单打开时父项保持高亮；行尾 › 随文字色", () => {
    const expanded = ruleFor(
      `${PREFIX} .ui-menu:not(.is-sheet) [role="menuitem"][aria-haspopup="menu"][aria-expanded="true"]:not(:disabled, [aria-disabled="true"])`,
    );
    has(expanded, "background", "var(--c-accent)");
    has(ruleFor(`${PREFIX} .ui-menu .ui-menu-chevron`), "border-top", "1.5px solid currentColor");
  });

  it("键盘提示条默认不显示，只在桌面指针（hover: hover 且 pointer: fine）下显示", () => {
    has(ruleFor(`${PREFIX} .ui-menu-hints`), "display", "none");
    const fine = mediaBlocks(stripComments(REFRESH_CSS)).filter((m) => /\(hover: hover\) and \(pointer: fine\)/.test(m.prelude));
    expect(fine.some((m) => m.rules.some((r) => norm(r.selector) === `${PREFIX} .ui-menu-hints` && /display: flex/.test(r.body)))).toBe(true);
    // Bottom-stuck notices cover final rows; menus with notices reserve notice height during arrow-key scrolling (measured individually in e2e).
    expect(
      fine.some((m) =>
        m.rules.some((r) => norm(r.selector) === `${PREFIX} .ui-menu:has(> .ui-menu-hints)` && /scroll-padding-bottom: 36px/.test(r.body)),
      ),
    ).toBe(true);
  });
});

describe("行为批 B：窄屏弹窗底部面板、撤销 Toast 与轻扫", () => {
  const norm = (text: string) => text.replace(/\s+/g, " ").trim();
  const ruleFor = (selector: string) => {
    const rule = RULES.find((r) => splitSelectors(r.selector).includes(norm(selector)));
    expect(rule, selector).toBeDefined();
    return norm(rule!.body);
  };
  const has = (text: string, prop: string, value: string) =>
    expect(new RegExp(`(?:^|;)\\s*${prop}: ${value.replace(/[()+*]/g, "\\$&")};`).test(text), `${prop}: ${value}`).toBe(true);
  const PANEL = `${PREFIX} .ui-dialog-sheet > [data-dialog-presentation="sheet"]`;
  const SHEET_DIALOG = `${PREFIX} .settings-dialog[data-dialog-presentation="sheet"]`;

  it("宿主在居中形态下不占盒子；面板全宽贴底、顶部圆角 16、实底，最高为可见区域的 92%，键盘弹起时抬起", () => {
    has(ruleFor(`${PREFIX} .ui-dialog-sheet-host`), "display", "contents");
    const sheet = ruleFor(`${PREFIX} .ui-dialog-sheet`);
    has(sheet, "position", "fixed");
    has(sheet, "left", "0");
    has(sheet, "right", "0");
    has(sheet, "bottom", "var(--ui-sheet-keyboard-inset, 0px)");
    has(sheet, "border-radius", "16px 16px 0 0");
    has(sheet, "background", "var(--c-surface)");
    has(sheet, "max-height", "calc(var(--ui-sheet-viewport-height, 100dvh) * 0.92)");
    has(sheet, "--ui-sheet-safe-bottom", "env(safe-area-inset-bottom, 0px)");
    has(ruleFor(`${PREFIX} .ui-dialog-sheet[data-keyboard="open"]`), "--ui-sheet-safe-bottom", "0px");
    has(ruleFor(`${PREFIX} .ui-dialog-sheet-grab::before`), "width", "36px");
    has(ruleFor(`${PREFIX} .ui-dialog-sheet-grab::before`), "height", "5px");
    has(ruleFor(`${PREFIX} .ui-dialog-sheet-grab`), "touch-action", "none");
  });

  it("面板里的弹窗本身不再浮起：盖掉调用方的宽度与各家族的最高、圆角、阴影，由自己滚动", () => {
    const panel = ruleFor(PANEL);
    has(panel, "width", "auto !important");
    has(panel, "max-width", "none !important");
    has(panel, "max-height", "none !important");
    has(panel, "border-radius", "0 !important");
    has(panel, "box-shadow", "none !important");
    has(panel, "overflow-y", "auto");
    has(panel, "padding-bottom", "0 !important");
  });

  it("自写弹窗的底栏吸底并留安全区", () => {
    const foot = RULES.find((r) => norm(r.selector).startsWith(`${PANEL} :is(`) && /\.kb-modal-foot/.test(r.selector));
    expect(foot).toBeDefined();
    const body = norm(foot!.body);
    has(body, "position", "sticky");
    has(body, "bottom", "0");
    has(body, "padding-bottom", "calc(10px + var(--ui-sheet-safe-bottom))");
  });

  it("Dialog：标题栏常驻关闭按钮、正文单独滚动、操作栏按钮 44 高平分宽度", () => {
    has(ruleFor(`${PREFIX} .ui-dialog-sheet > .settings-dialog[data-dialog-presentation="sheet"]`), "overflow", "hidden");
    has(ruleFor(`${PREFIX} .ui-dialog-sheet-head`), "touch-action", "none");
    has(ruleFor(`${PREFIX} .ui-dialog-sheet-head .settings-dialog__title`), "font-size", "var(--fs-dialog-title)");
    has(ruleFor(`${PREFIX} .ui-dialog-sheet-close`), "background", "var(--c-fill-2)");
    has(ruleFor(`${SHEET_DIALOG} > .settings-dialog__body`), "overflow-y", "auto");
    const footer = ruleFor(`${SHEET_DIALOG} > .settings-dialog__footer`);
    has(footer, "grid-auto-columns", "minmax(0, 1fr)");
    has(footer, "grid-auto-flow", "column");
    has(footer, "padding", "10px 16px calc(10px + var(--ui-sheet-safe-bottom))");
    has(ruleFor(`${SHEET_DIALOG} > .settings-dialog__footer > :is(button, a)`), "min-height", "44px");
  });

  it("面板里的文本输入 16px（iOS 聚焦不放大）", () => {
    const rule = RULES.find((r) => norm(r.selector).startsWith(`${PREFIX} .ui-dialog-sheet :is(input:not(`));
    expect(rule).toBeDefined();
    has(norm(rule!.body), "font-size", "max(1rem, 16px)");
  });

  it("撤销 Toast 不画状态标记；Toast 触屏可轻扫（touch-action: none）", () => {
    has(ruleFor(`${PREFIX} .rho-toast.ui-toast--undo .legacy-toast-dot`), "display", "none");
    const toastRules = RULES.filter((r) => splitSelectors(r.selector).includes(`${PREFIX} .rho-toast`)).map((r) => norm(r.body));
    expect(toastRules.some((body) => /touch-action: none;/.test(body))).toBe(true);
  });
});
