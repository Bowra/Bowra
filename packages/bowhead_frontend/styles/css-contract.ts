/**
 * Shared style-contract test helpers for the interface stylesheets (styles/ui-refresh*.css and their CSS Modules).
 * Source-only parsing: rules, selectors, specificity, declarations. jsdom does no cascading; real browsers validate pixels.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

export const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
/**
 * Every rule in the interface stylesheets starts with `:root:root`: the (0,2,0) it adds keeps these rules above the base
 * styles they restyle (app/globals.css, styles/ui-controls-legacy.css and the other base stylesheets).
 */
export const PREFIX = ":root:root";

export function read(rel: string): string {
  return readFileSync(join(FRONTEND, rel), "utf8");
}

/**
 * Read stylesheet section beginning `/* @section <name> …`, ending at next `@section` marker or EOF.
 * Page-acceptance A group mappings/legacy appearance share one file; each contract enforces only its own section.
 */
export function readSection(rel: string, name: string): string {
  const css = read(rel);
  const start = css.indexOf(`/* @section ${name} `);
  if (start < 0) throw new Error(`${rel}: 找不到 @section ${name}`);
  const next = css.indexOf("/* @section ", start + 1);
  return css.slice(start, next < 0 ? undefined : next);
}

export function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

export type Rule = { selector: string; body: string; media: string | null };

/** Parse ordinary stylesheet rules;@media / @supports recurse (record outer conditions),@keyframes skip. */
export function parseRules(css: string, media: string | null = null): Rule[] {
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
    if (/^@(media|supports)\b/.test(prelude)) rules.push(...parseRules(body, media ? `${media} ${prelude}` : prelude));
    else if (!prelude.startsWith("@")) rules.push({ selector: prelude, body, media });
    i = close + 1;
  }
  return rules;
}

/** Split selectors at top-level commas (ignore commas inside :is() / :where() parentheses). */
export function splitSelectors(selector: string): string[] {
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

export const norm = (text: string) => text.replace(/\s+/g, " ").trim();

export type Spec = [number, number, number];

export function cmpSpec(x: Spec, y: Spec): number {
  return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
}

/** CSS specificity [id, class, type]: :is() / :not() / :has() take highest argument; :where() is zero; pseudo-elements count as type. */
export function specificity(selector: string): Spec {
  const max = (list: string): Spec =>
    splitSelectors(list).map(specificity).reduce((a, b) => (cmpSpec(a, b) >= 0 ? a : b), [0, 0, 0] as Spec);
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
    } else {
      i += 1;
    }
  }
  return [a, b, c];
}

/** Declaration map: property → value (normalize whitespace inside values). */
export function decls(body: string): Record<string, string> {
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

/** Rule subjects: strip the :root:root prefix; split sole :is(a, b) into a/b, otherwise use whole selector. */
function subjects(sel: string): string[] {
  const rest = norm(sel.startsWith(PREFIX) ? sel.slice(PREFIX.length) : sel);
  if (!rest.startsWith(":is(")) return [rest];
  let depth = 0;
  for (let k = 3; k < rest.length; k += 1) {
    if (rest[k] === "(") depth += 1;
    else if (rest[k] === ")" && --depth === 0) return k === rest.length - 1 ? splitSelectors(rest.slice(4, -1)).map(norm) : [rest];
  }
  return [rest];
}
/** State rules (hover, pressed, focus, disabled, selected, highlighted) apply only then; exclude from default-override comparison. */
const STATEFUL = /:(hover|active|focus|focus-visible|focus-within|disabled|checked|empty)\b|aria-(disabled|checked|selected|current|pressed|expanded)|data-active|\.is-(active|on|selected|set|pending|dirty)\b|\.selected\b/;
/** Properties overlap if same-named or one is shorthand for other (margin/margin-top, border/border-top-color). */
const overlaps = (x: string, y: string) => x === y || x.startsWith(`${y}-`) || y.startsWith(`${x}-`);

/** Same subject/property: later specificity (:is() takes maximum) must ≥ earlier specificity or override fails. */
export function cascadeProblems(rules: Rule[]): string[] {
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

/** Every global stylesheet the (app) layout can load: app/globals.css plus styles/*.css. */
export function allStylesheets(): string {
  const sheets = ["app/globals.css", ...readdirSync(join(FRONTEND, "styles")).filter((name) => name.endsWith(".css")).map((name) => `styles/${name}`)];
  return stripComments(sheets.map(read).join("\n"));
}

export function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "node_modules" ? [] : tsxFiles(path);
    return name.endsWith(".tsx") && !name.endsWith(".test.tsx") ? [path] : [];
  });
}

/** Literal colors (hex, rgb/hsl functions, white/black keywords). */
export const LITERAL_COLOR = /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?)\(|(?<![\w-])(?:white|black)(?![\w-])/i;
export const isTokenRoot = (sel: string) => /^:root(?::root)?(\[[^\]]+\])*$/.test(sel);
