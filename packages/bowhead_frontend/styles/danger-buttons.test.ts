/**
 * Destructive-control colour contract.
 *
 * Destructive buttons and menu items must be painted from the danger tokens (`--c-err`, `--c-err-soft`,
 * `--c-err-hover`, `--c-err-on`) in every theme × light/dark, and their label must keep
 * 4.5:1 against the fill it sits on. Two regressions this pins:
 *
 *   - A destructive modifier on a button whose base family is styled with the accent (for example the interface
 *     ghost family `.memory-v2-text-button` → `--ui-btn-fg: var(--c-accent)`) rendered the destructive action in
 *     the theme accent (green in green themes) because the modifier was missing from the danger family.
 *   - Solid destructive fills used fixed white / accent-ink / warning-palette labels (`.modal .btn.danger`
 *     white on the light dark-mode red was 2.9:1; memory dialogs used the warning palette for "danger").
 *
 * jsdom does not cascade, so this file runs a small cascade itself: it reads the authenticated layout's CSS in
 * import order, matches every top-level rule against a realistic DOM fixture with `Element.matches`, picks the
 * winner per property by specificity then source order, and resolves `var()` / `color-mix(in srgb, …)` against
 * the fixture's ancestors and the themed `<html>`. Rules inside `@media` / `@supports` blocks are skipped (they
 * only adjust sizing for touch / narrow screens; none of them colours a destructive control).
 */
import { readFileSync } from "node:fs";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

const FRONTEND = path.resolve(__dirname, "..");
const REPO = path.resolve(FRONTEND, "..", "..");

// ── CSS rules ────────────────────────────────────────────────────────────────

type Decl = { prop: string; value: string; important: boolean };
type Rule = { selector: string; decls: Decl[]; order: number; file: string };

function cssImports(layout: string): string[] {
  const source = readFileSync(path.join(FRONTEND, layout), "utf8");
  const dir = path.dirname(path.join(FRONTEND, layout));
  return Array.from(source.matchAll(/^import "(\.[^"]+\.css)";/gm), (m) => path.resolve(dir, m[1]));
}

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** Top-level style rules only; at-rule blocks are skipped as a whole. */
function topLevelRules(css: string, file: string, start: number): Rule[] {
  const rules: Rule[] = [];
  let depth = 0;
  let last = 0;
  let selector = "";
  let bodyStart = 0;
  let skipping = false;
  for (let i = 0; i < css.length; i += 1) {
    const ch = css[i];
    if (ch === "{") {
      if (depth === 0) {
        selector = css.slice(last, i).trim();
        skipping = selector.startsWith("@");
        bodyStart = i + 1;
      }
      depth += 1;
    } else if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        if (!skipping && selector) {
          const decls: Decl[] = [];
          for (const m of css.slice(bodyStart, i).matchAll(/([\w-]+)\s*:\s*([^;]+?)\s*(!important)?\s*(?:;|$)/g)) {
            decls.push({ prop: m[1].trim(), value: m[2].replace(/\s+/g, " ").trim(), important: Boolean(m[3]) });
          }
          rules.push({ selector: selector.replace(/\s+/g, " "), decls, order: start + rules.length, file });
        }
        last = i + 1;
      }
    } else if (ch === ";" && depth === 0) {
      last = i + 1; // e.g. @import / @charset statements
    }
  }
  return rules;
}

function loadRules(): Rule[] {
  const rules: Rule[] = [];
  for (const file of cssImports("app/(app)/layout.tsx")) {
    rules.push(...topLevelRules(stripComments(readFileSync(file, "utf8")), path.relative(FRONTEND, file), rules.length));
  }
  return rules;
}

/** Split on top-level commas. */
function splitTop(list: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let cur = "";
  for (const ch of list) {
    if (ch === "(" || ch === "[") depth += 1;
    if (ch === ")" || ch === "]") depth -= 1;
    if (ch === "," && depth === 0) {
      out.push(cur.trim());
      cur = "";
    } else cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

type Spec = [number, number, number];
const add = (a: Spec, b: Spec): Spec => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const cmp = (a: Spec, b: Spec) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
const maxSpec = (list: Spec[]): Spec => list.reduce((m, s) => (cmp(s, m) > 0 ? s : m), [0, 0, 0]);

/** Selectors Level 4 specificity for one complex selector (:is/:not/:has take their most specific argument). */
function specificity(sel: string): Spec {
  let spec: Spec = [0, 0, 0];
  let i = 0;
  const ident = () => {
    const m = /^[\w-]+/.exec(sel.slice(i));
    i += m ? m[0].length : 0;
    return m ? m[0] : "";
  };
  const parenBody = () => {
    let depth = 0;
    const begin = i + 1;
    for (; i < sel.length; i += 1) {
      if (sel[i] === "(") depth += 1;
      else if (sel[i] === ")" && --depth === 0) break;
    }
    i += 1;
    return sel.slice(begin, i - 1);
  };
  while (i < sel.length) {
    const ch = sel[i];
    if (ch === "#") {
      i += 1;
      ident();
      spec = add(spec, [1, 0, 0]);
    } else if (ch === ".") {
      i += 1;
      ident();
      spec = add(spec, [0, 1, 0]);
    } else if (ch === "[") {
      i = sel.indexOf("]", i) + 1;
      spec = add(spec, [0, 1, 0]);
    } else if (ch === ":" && sel[i + 1] === ":") {
      i += 2;
      ident();
      if (sel[i] === "(") parenBody();
      spec = add(spec, [0, 0, 1]);
    } else if (ch === ":") {
      i += 1;
      const name = ident().toLowerCase();
      const args = sel[i] === "(" ? parenBody() : null;
      if (name === "where") continue;
      if ((name === "is" || name === "not" || name === "has" || name === "matches") && args !== null) {
        spec = add(spec, maxSpec(splitTop(args).map(specificity)));
      } else {
        spec = add(spec, [0, 1, 0]);
      }
    } else if (/[a-zA-Z]/.test(ch)) {
      ident();
      spec = add(spec, [0, 0, 1]);
    } else {
      i += 1; // combinators, whitespace, '*'
    }
  }
  return spec;
}

/** Interaction states are forced through data attributes so jsdom can match them. */
function forceStates(sel: string): string {
  return sel
    .replace(/:hover\b/g, "[data-x-hover]")
    .replace(/:focus-visible\b/g, "[data-x-focus-visible]")
    .replace(/:focus(?![-\w])/g, "[data-x-focus]")
    .replace(/:active\b/g, "[data-x-active]");
}

// ── Cascade ──────────────────────────────────────────────────────────────────

type Winner = { value: string; spec: Spec; order: number; important: boolean; where: string };

const RULES = loadRules();
const unparseable = new Set<string>();

type Compiled = { rule: Rule; complex: string; forced: string; spec: Spec; required: string[]; tag: string | null };

/** Subject compound (rightmost, parentheses removed): the classes and tag it requires, a cheap pre-filter before `matches`. */
function subjectOf(complex: string): { required: string[]; tag: string | null } {
  let flat = "";
  let depth = 0;
  for (const ch of complex) {
    if (ch === "(") depth += 1;
    else if (ch === ")") depth -= 1;
    else if (depth === 0) flat += ch;
  }
  const subject = flat.trim().split(/\s*[\s>+~]\s*/).at(-1) ?? "";
  const tag = /^:root\b/.test(subject) ? "HTML" : (/^([a-zA-Z][\w-]*)/.exec(subject)?.[1].toUpperCase() ?? null);
  return { required: Array.from(subject.matchAll(/\.([\w-]+)/g), (m) => m[1]), tag };
}

function compile(rules: Rule[]): Compiled[] {
  return rules.flatMap((rule) =>
    splitTop(rule.selector)
      .filter((complex) => !/::|:-webkit-|:-moz-/.test(complex)) // pseudo-elements do not paint the element itself
      .map((complex) => ({ rule, complex, forced: forceStates(complex), spec: specificity(complex), ...subjectOf(complex) })),
  );
}

/** Index by one required class so each element only tests rules that can possibly match it. */
function indexRules(compiled: Compiled[]): { byClass: Map<string, Compiled[]>; unclassed: Compiled[] } {
  const byClass = new Map<string, Compiled[]>();
  const unclassed: Compiled[] = [];
  for (const entry of compiled) {
    const key = entry.required[0];
    if (key === undefined) unclassed.push(entry);
    else byClass.set(key, [...(byClass.get(key) ?? []), entry]);
  }
  return { byClass, unclassed };
}

const INDEX = indexRules(compile(RULES));

/** Element plus ancestors (tag, classes, attributes): two elements with the same signature match the same rules. */
function signature(el: Element): string {
  const parts: string[] = [];
  for (let node: Element | null = el; node; node = node.parentElement) {
    const attrs = Array.from(node.attributes)
      .filter((a) => a.name !== "class")
      .map((a) => `${a.name}=${a.value}`)
      .sort()
      .join(",");
    parts.push(`${node.tagName}.${Array.from(node.classList).sort().join(".")}[${attrs}]`);
  }
  return parts.join("<");
}

function matching(el: Element, compiled: Compiled[]): Compiled[] {
  return compiled.filter(({ complex, forced, required, tag }) => {
    if (tag && tag !== el.tagName.toUpperCase()) return false;
    if (!required.every((c) => el.classList.contains(c))) return false;
    try {
      return el.matches(forced);
    } catch {
      unparseable.add(complex);
      return false;
    }
  });
}

const matchCache = new Map<string, Compiled[]>();

/** Stylesheet rules matching the element (memoised by signature) plus the per-theme team brand rules. */
function matchedRules(el: Element, extra: Rule[]): Compiled[] {
  const key = signature(el);
  let hits = matchCache.get(key);
  if (!hits) {
    const pool = [...INDEX.unclassed, ...Array.from(el.classList).flatMap((c) => INDEX.byClass.get(c) ?? [])];
    hits = matching(el, pool);
    matchCache.set(key, hits);
  }
  return [...matching(el, compile(extra)), ...hits];
}

function winningDecls(el: Element, extra: Rule[]): Map<string, Winner> {
  const result = new Map<string, Winner>();
  for (const { rule, complex, spec } of matchedRules(el, extra)) {
    for (const d of rule.decls) {
      for (const [prop, value] of expandShorthand(d.prop, d.value)) {
        const prev = result.get(prop);
        const wins =
          !prev ||
          (d.important && !prev.important) ||
          (d.important === prev.important && (cmp(spec, prev.spec) > 0 || (cmp(spec, prev.spec) === 0 && rule.order >= prev.order)));
        if (wins) result.set(prop, { value, spec, order: rule.order, important: d.important, where: `${rule.file}: ${complex}` });
      }
    }
  }
  return result;
}

/** Only the colour longhands this contract reads. */
function expandShorthand(prop: string, value: string): Array<[string, string]> {
  if (prop.startsWith("--") || prop === "color" || prop === "background-color") return [[prop, value]];
  if (prop === "background") {
    const colour = splitTop(value.replace(/\s+/g, " ")).at(-1) ?? "";
    return isColourExpression(colour) ? [["background-color", colour]] : [["background-color", "transparent"]];
  }
  return [];
}

function isColourExpression(value: string): boolean {
  return /^(var\(|color-mix\(|#|rgb|hsl|transparent$|white$|black$|none$|inherit$|currentcolor$)/i.test(value.trim());
}

type Resolved = { value: string; tokens: string[] };
const declCache = new Map<Element, Map<string, Winner>>();

function declsOf(el: Element, extra: Rule[]): Map<string, Winner> {
  let decls = declCache.get(el);
  if (!decls) {
    decls = winningDecls(el, extra);
    declCache.set(el, decls);
  }
  return decls;
}

/** Custom properties inherit: look on the element, then up the ancestors to <html>. */
function customProp(el: Element, name: string, extra: Rule[]): { value: string; owner: Element } | null {
  for (let node: Element | null = el; node; node = node.parentElement) {
    const decl = declsOf(node, extra).get(name);
    if (decl) return { value: decl.value, owner: node };
  }
  return null;
}

function substitute(value: string, el: Element, extra: Rule[], tokens: string[], depth = 0): string {
  if (depth > 20) throw new Error(`var() cycle at ${value}`);
  return value.replace(/var\((--[\w-]+)\s*(?:,\s*((?:[^()]|\([^()]*(?:\([^()]*\)[^()]*)*\))*))?\)/g, (_m, name: string, fallback?: string) => {
    tokens.push(name);
    const found = customProp(el, name, extra);
    // A custom property's own var() resolves on the element that declares it (computed-value time).
    if (found) return substitute(found.value, found.owner, extra, tokens, depth + 1);
    if (fallback !== undefined) return substitute(fallback, el, extra, tokens, depth + 1);
    return "unset";
  });
}

/** Resolved used value of an inherited (`color`) or non-inherited (`background-color`) property. */
function resolve(el: Element, prop: "color" | "background-color", extra: Rule[]): Resolved {
  const tokens: string[] = [];
  for (let node: Element | null = el; node; node = node.parentElement) {
    const decl = declsOf(node, extra).get(prop);
    if (decl && decl.value !== "inherit") {
      const value = substitute(decl.value, node, extra, tokens);
      if (value !== "unset") return { value, tokens };
    }
    if (prop === "background-color") return { value: "transparent", tokens };
  }
  return { value: "unset", tokens };
}

// ── Colour maths ─────────────────────────────────────────────────────────────

type RGBA = [number, number, number, number];

function parseColour(raw: string): RGBA {
  const value = raw.trim().toLowerCase();
  if (value === "transparent" || value === "none") return [0, 0, 0, 0];
  if (value === "white") return [255, 255, 255, 1];
  if (value === "black") return [0, 0, 0, 1];
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(value);
  if (hex) {
    const h = hex[1].length === 3 ? [...hex[1]].map((c) => c + c).join("") : hex[1];
    return [0, 2, 4].map((k) => Number.parseInt(h.slice(k, k + 2), 16)).concat(1) as RGBA;
  }
  const rgb = /^rgba?\(([^)]+)\)$/.exec(value);
  if (rgb) {
    const parts = rgb[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    return [parts[0], parts[1], parts[2], parts[3] ?? 1];
  }
  const mix = /^color-mix\(in srgb,\s*(.+)\)$/.exec(value);
  if (mix) {
    const [a, b] = splitTop(mix[1]);
    const pa = /\s(\d+(?:\.\d+)?)%$/.exec(a);
    const pb = /\s(\d+(?:\.\d+)?)%$/.exec(b);
    const wa = pa ? Number(pa[1]) / 100 : pb ? 1 - Number(pb[1]) / 100 : 0.5;
    const ca = parseColour(pa ? a.slice(0, pa.index) : a);
    const cb = parseColour(pb ? b.slice(0, pb.index) : b);
    // Premultiplied interpolation, as CSS Color 5 specifies for color-mix().
    const alpha = ca[3] * wa + cb[3] * (1 - wa);
    if (alpha === 0) return [0, 0, 0, 0];
    const ch = (k: number) => (ca[k] * ca[3] * wa + cb[k] * cb[3] * (1 - wa)) / alpha;
    return [ch(0), ch(1), ch(2), alpha];
  }
  throw new Error(`unsupported colour: ${raw}`);
}

function over(top: RGBA, bottom: RGBA): RGBA {
  const a = top[3];
  return [0, 1, 2].map((k) => top[k] * a + bottom[k] * (1 - a)).concat(1) as RGBA;
}

function luminance([r, g, b]: RGBA): number {
  const lin = (v: number) => {
    const s = v / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(a: RGBA, b: RGBA): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// ── Themes ───────────────────────────────────────────────────────────────────

type Scheme = Record<"error" | "errorSoft" | "surface" | "background" | "accent" | "accentInk" | "ink", string>;
type Theme = { name: string; profile: string; brand: { light: Partial<Scheme>; dark: Partial<Scheme> } | null };

const BRAND_NAMES: Record<keyof Scheme, string[]> = {
  error: ["--c-err", "--c-neg", "--c-red"],
  errorSoft: ["--c-err-soft", "--c-neg-soft", "--c-red-soft"],
  surface: ["--c-surface", "--surface"],
  background: ["--c-bg", "--bg"],
  accent: ["--c-accent"],
  accentInk: ["--c-accent-ink"],
  ink: ["--c-ink", "--text"],
};

/** Mirrors lib/brand-bootstrap-script.ts: team schemes land as `:root[data-theme=…]` declarations. */
function brandRules(theme: Theme): Rule[] {
  if (!theme.brand) return [];
  return (["light", "dark"] as const).map((mode, k) => ({
    selector: `:root[data-theme="${mode}"]`,
    decls: Object.entries(theme.brand![mode]).flatMap(([key, value]) =>
      (BRAND_NAMES[key as keyof Scheme] ?? []).map((prop) => ({ prop, value: value!, important: false })),
    ),
    order: -10 + k,
    file: `team-brand:${theme.name}`,
  }));
}

/** Team status colours come from the backend compiler; read them from its source instead of copying them. */
function compilerStatusTokens(): { light: Partial<Scheme>; dark: Partial<Scheme> } {
  const source = readFileSync(path.join(REPO, "packages/bowhead_core/src/bowhead_core/theme_compiler.py"), "utf8");
  const block = source.slice(source.indexOf("_STATUS_TOKENS"));
  const pick = (mode: string) => {
    const body = block.slice(block.indexOf(`"${mode}": {`));
    const get = (key: string) => new RegExp(`"${key}": "(#[0-9A-Fa-f]{6})"`).exec(body)?.[1];
    return { error: get("error"), errorSoft: get("errorSoft") };
  };
  return { light: pick("light"), dark: pick("dark") };
}

function fixtureScheme(name: string): { light: Partial<Scheme>; dark: Partial<Scheme> } {
  const themes = JSON.parse(readFileSync(path.join(FRONTEND, "e2e/fixtures/home-accent-themes.json"), "utf8"));
  const scheme = themes[name].colorScheme;
  return { light: scheme.light, dark: scheme.dark };
}

const MOCK_TEAM = {
  // e2e/mock-api-server.mjs team scheme (the scheme dev servers and screenshots run with)
  light: { error: "#B33A3A", errorSoft: "#F8DEDE", surface: "#FFFFFF", background: "#FAF9F6", accent: "#B04A2A" },
  dark: { error: "#E27B7B", errorSoft: "#482525", surface: "#1D1B18", background: "#151412", accent: "#E18A68" },
};

const THEMES: Theme[] = [
  { name: "product default (blue)", profile: "bowhead", brand: null },
  { name: "ocean profile (blue)", profile: "ocean", brand: null },
  { name: "forest profile (green)", profile: "forest", brand: null },
  { name: "plum profile", profile: "plum", brand: null },
  { name: "graphite profile", profile: "graphite", brand: null },
  { name: "team preset (compiler status tokens)", profile: "bowhead", brand: compilerStatusTokens() },
  { name: "team custom blue", profile: "bowhead", brand: fixtureScheme("blue") },
  { name: "team custom plum", profile: "bowhead", brand: fixtureScheme("plum") },
  { name: "mock team", profile: "bowhead", brand: MOCK_TEAM },
];

// ── Destructive controls (DOM mirrors the real call sites) ──────────────────

type Control = { name: string; html: string; look: "solid" | "text" };

const CONTROLS: Control[] = [
  { name: "Button danger on a page", look: "text", html: '<button data-probe class="settings-button settings-button--danger settings-button--md">Clear</button>' },
  {
    name: "ConfirmDialog / Dialog footer (delete, MCP Refresh Tool List)",
    look: "solid",
    html: '<div role="dialog" class="settings-dialog"><div class="settings-dialog__footer"><button class="settings-button settings-button--secondary settings-button--md">Cancel</button><button data-probe class="settings-button settings-button--danger settings-button--md">Delete</button></div></div>',
  },
  { name: "approvals .btn-danger", look: "text", html: '<button data-probe class="btn-danger">Reject</button>' },
  {
    name: "files confirm .modal .mrow .btn.danger",
    look: "solid",
    html: '<div class="files-main"><div class="scrim"><div role="dialog" class="modal"><div class="mrow"><button data-probe class="btn danger">Delete</button></div></div></div></div>',
  },
  {
    name: "skills confirm .sset .modal-foot .btn.danger",
    look: "solid",
    html: '<div class="sset"><div role="dialog" class="modal"><div class="modal-foot"><button data-probe class="btn danger sm">Delete</button></div></div></div>',
  },
  {
    name: "models confirm .mset .mset-modal .btn.danger",
    look: "solid",
    html: '<div class="mset"><div class="mset-scrim"><div role="dialog" class="mset-modal"><div class="modal-foot"><button data-probe class="btn danger sm">Delete</button></div></div></div></div>',
  },
  { name: "KB bulk delete .kb-btn-danger", look: "text", html: '<div class="kb-bulkbar"><button data-probe class="kb-btn kb-btn-danger kb-btn-sm">Delete</button></div>' },
  {
    name: "KB confirm .kb-btn-danger-solid",
    look: "solid",
    html: '<div class="kb-overlay"><div role="dialog" class="kb-modal"><div class="kb-modal-foot"><button data-probe class="kb-btn kb-btn-danger-solid">Delete</button></div></div></div>',
  },
  { name: "memory .memory-v2-button.is-danger", look: "text", html: '<button data-probe class="memory-v2-button is-danger">Delete</button>' },
  { name: "memory bulk ignore .memory-v2-button.danger", look: "text", html: '<button data-probe class="memory-v2-button danger small">Ignore</button>' },
  { name: "memory permanent delete .memory-v2-text-button.is-danger", look: "text", html: '<button data-probe class="memory-v2-text-button is-danger">Delete permanently</button>' },
  {
    name: "memory delete-all .memory-v2-privacy-danger .memory-v2-text-button",
    look: "text",
    html: '<div class="memory-v2-privacy-grid"><div class="memory-v2-privacy-danger"><button data-probe class="memory-v2-text-button">Delete all</button></div></div>',
  },
  { name: "memory empty trash .memory-v2-clear-trash", look: "text", html: '<div class="memory-v2-state-tabs"><button data-probe class="is-danger memory-v2-clear-trash">Empty trash</button></div>' },
  {
    name: "memory confirm .memory-modal-actions .danger",
    look: "solid",
    html: '<div role="dialog" class="memory-modal"><div class="memory-modal-actions"><button>Cancel</button><button data-probe class="danger">Delete</button></div></div>',
  },
  { name: "credits .credits-btn--danger (page)", look: "text", html: '<button data-probe class="credits-btn credits-btn--danger">Reset</button>' },
  {
    name: "credits reset confirm .credits-modal-foot",
    look: "solid",
    html: '<div role="dialog"><div class="credits-modal-foot"><button data-probe class="credits-btn credits-btn--danger">Reset</button></div></div>',
  },
  {
    name: "share revoke .legacy-share-btn--danger",
    look: "solid",
    html: '<div role="dialog" class="legacy-share-revoke"><div class="legacy-share-revoke-foot"><button data-probe class="legacy-share-btn legacy-share-btn--danger">Stop sharing</button></div></div>',
  },
  {
    name: "agent dialog .am-danger-confirm",
    look: "solid",
    html: '<div role="dialog" class="am-dialog"><footer><button data-probe class="am-danger-confirm">Disable</button></footer></div>',
  },
  { name: "agent management .am-danger-action", look: "text", html: '<div class="am-management-actions"><button data-probe class="am-danger-action">Unhire</button></div>' },
  { name: "floating menu item .is-danger", look: "text", html: '<div role="menu" class="app-floating-menu"><button role="menuitem" data-probe class="app-floating-menu-item is-danger">Delete</button></div>' },
  {
    name: "row actions menu item",
    look: "text",
    html: '<div role="menu" class="ui-menu ui-row-menu"><button role="menuitem" data-probe class="ui-row-menu-item is-danger">Delete…</button></div>',
  },
];

/** Logout is styled as a neutral item on purpose (component spec decision 7: it deletes no data). */
const NOT_DESTRUCTIVE = [".v2-user-menu-item.is-danger"];
/** Danger-named panels, badges and icons that are not buttons (status surfaces keep their own palette). */
const NOT_A_CONTROL =
  /danger-zone|dangerZone|modal-ico|wf-state|-pill|notification-empty|card__danger|verify--danger|warnbar|danger-dialog|privacy-grid > \.memory-v2-privacy-danger/;

// ── Harness ──────────────────────────────────────────────────────────────────

type Paint = { fg: RGBA; bg: RGBA; fgTokens: string[]; bgTokens: string[]; where: string };

function paint(control: Control, theme: Theme, mode: "light" | "dark", hover: boolean): Paint {
  const root = document.documentElement;
  root.setAttribute("data-theme", mode);
  root.setAttribute("data-color-profile", theme.profile);
  root.setAttribute("data-theme-accent", theme.profile === "bowhead" ? "default" : theme.profile);
  document.body.innerHTML = `<main class="v2-main">${control.html}</main>`;
  const probe = document.querySelector("[data-probe]")!;
  if (hover) probe.setAttribute("data-x-hover", "");
  declCache.clear();
  const extra = brandRules(theme);
  const fg = resolve(probe, "color", extra);
  const bg = resolve(probe, "background-color", extra);
  const surface = parseColour(substitute("var(--c-surface)", probe, extra, []));
  const bgColour = over(parseColour(bg.value), surface);
  return {
    fg: over(parseColour(fg.value), bgColour),
    bg: bgColour,
    fgTokens: fg.tokens,
    bgTokens: bg.tokens,
    where: `${declsOf(probe, extra).get("color")?.where ?? "inherited"} | ${declsOf(probe, extra).get("background-color")?.where ?? "-"}`,
  };
}

const DANGER_TOKEN = /^--c-(err|neg|red|danger)(-|$)/;
const ACCENT_TOKEN = /^--(c-accent|c-terra|color-brand|ag-lead|c-focus)/;
const OFF_PALETTE_TOKEN = /^--(color-alert|c-warn|c-pos|c-info)/;

afterEach(() => {
  document.body.innerHTML = "";
  for (const attr of ["data-theme", "data-color-profile", "data-theme-accent"]) {
    document.documentElement.removeAttribute(attr);
  }
});

describe("destructive controls paint with the danger tokens", () => {
  it("the cascade harness understands every selector that names a destructive class", () => {
    for (const control of CONTROLS) paint(control, THEMES[0], "dark", true);
    expect([...unparseable].filter((s) => /danger|destructive/.test(s))).toEqual([]);
  });

  it("no rule colours a destructive control with the accent or another status palette", () => {
    const problems: string[] = [];
    for (const rule of RULES) {
      const destructive = splitTop(rule.selector).filter(
        (s) => /danger|destructive/.test(s) && !NOT_DESTRUCTIVE.some((n) => s.includes(n)) && !NOT_A_CONTROL.test(s),
      );
      if (!destructive.length) continue;
      for (const d of rule.decls) {
        if (!/^(color|background|background-color|border|border-color|--ui-btn-(bg|fg)[\w-]*)$/.test(d.prop)) continue;
        // A literal fallback inside a danger var() never applies (the danger tokens are always defined).
        const value = d.value.replace(/var\((--c-(?:err|neg|red|danger)[\w-]*)\s*,(?:[^()]|\([^()]*\))*\)/g, "var($1)");
        if (/var\(--(c-accent|c-terra|color-brand|color-alert|c-warn|c-pos|c-info)/.test(value) || /#[0-9a-f]{3,8}\b|\bwhite\b|rgba?\(/i.test(value)) {
          problems.push(`${rule.file}: ${destructive.join(", ")} { ${d.prop}: ${d.value} }`);
        }
      }
    }
    expect(problems).toEqual([]);
  });

  const MODES = ["light", "dark"] as const;
  const cases = THEMES.flatMap((theme) => MODES.map((mode) => ({ theme, mode })));

  it.each(cases.map((c) => [`${c.theme.name} · ${c.mode}`, c] as const))(
    "%s: danger colour, never accent, label ≥ 4.5:1 (rest and hover)",
    (_label, { theme, mode }) => {
      const problems: string[] = [];
      for (const control of CONTROLS) {
        const look = control.look;
        for (const hover of [false, true]) {
          const p = paint(control, theme, mode, hover);
          const state = `${control.name}${hover ? " :hover" : ""}`;
          const painted = look === "solid" ? p.bgTokens : p.fgTokens;
          if (!painted.some((t) => DANGER_TOKEN.test(t))) problems.push(`${state}: ${look} colour does not come from a danger token (${painted.join(" → ") || "no token"}; ${p.where})`);
          const used = [...p.fgTokens, ...p.bgTokens];
          if (used.some((t) => ACCENT_TOKEN.test(t) || OFF_PALETTE_TOKEN.test(t))) problems.push(`${state}: uses ${used.filter((t) => ACCENT_TOKEN.test(t) || OFF_PALETTE_TOKEN.test(t)).join(", ")} (${p.where})`);
          const ratio = contrast(p.fg, p.bg);
          if (ratio < 4.5) problems.push(`${state}: label contrast ${ratio.toFixed(2)} < 4.5 (${p.where})`);
        }
      }
      expect(problems).toEqual([]);
    },
  );
});

describe("danger tokens per theme × mode", () => {
  /** Resolved root danger colours; a change here is a deliberate design decision, not a side effect. */
  const EXPECTED: Record<string, Record<"light" | "dark", { err: string; on: string }>> = {
    "product default (blue)": { light: { err: "#b42318", on: "#ffffff" }, dark: { err: "#f58b82", on: "#190e0d" } },
    "forest profile (green)": { light: { err: "#b42318", on: "#ffffff" }, dark: { err: "#f58b82", on: "#190e0d" } },
    "team preset (compiler status tokens)": { light: { err: "#b42318", on: "#ffffff" }, dark: { err: "#f58b82", on: "#190e0d" } },
    "team custom blue": { light: { err: "#9f2438", on: "#ffffff" }, dark: { err: "#f58b82", on: "#190e0d" } },
    "mock team": { light: { err: "#b33a3a", on: "#ffffff" }, dark: { err: "#e27b7b", on: "#170c0c" } },
  };
  const hex = (c: RGBA) => `#${c.slice(0, 3).map((v) => Math.round(v).toString(16).padStart(2, "0")).join("")}`;

  it.each(Object.keys(EXPECTED))("%s", (name) => {
    const theme = THEMES.find((t) => t.name === name)!;
    for (const mode of ["light", "dark"] as const) {
      const control = CONTROLS[1];
      paint(control, theme, mode, false);
      const probe = document.querySelector("[data-probe]")!;
      const extra = brandRules(theme);
      const err = hex(parseColour(substitute("var(--c-err)", probe, extra, [])));
      const on = hex(parseColour(substitute("var(--c-err-on)", probe, extra, [])));
      expect({ err, on }, `${name} ${mode}`).toEqual(EXPECTED[name][mode]);
    }
  });

  it("colour profiles change the accent only, never the danger tokens", () => {
    const problems = RULES.filter((r) => /data-color-profile|data-theme-accent/.test(r.selector))
      .flatMap((r) => r.decls.filter((d) => DANGER_TOKEN.test(d.prop)).map((d) => `${r.selector} { ${d.prop} }`));
    expect(problems).toEqual([]);
  });
});
