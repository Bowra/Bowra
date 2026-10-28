/**
 * **Scope** gate for prose hierarchy (h3–h6 / lists / quotes / body tables).
 *
 * These rules restore what Tailwind preflight suppresses: `list-style`, heading
 * `font-size`/`margin`. Fuller restoration raises the cost of wrong scope: report prose is not the only
 * user of `<ol>`; sources sections (116 v1.2.21: 1 ol / 12 li) lay out their own
 * `[N] · tool: query` rows; counters would add a spurious number column. Scope must
 * exactly match schema TEXT_BASED_KINDS (narrative/deck/callout): only those three contain
 * editable prose made of standard PM blocks; all other kinds are self-layout components.
 *
 * Two assertions cover two real drifts:
 *   1. Schema adds a fourth text-based kind, CSS does not follow → prose hierarchy silently fails there;
 *   2. Someone broadens selector to `.canvas-report-article ol` for convenience → component lists are affected.
 *
 * jsdom cannot arbitrate cascading, but source deterministically establishes correct selector scope.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const CANVAS_CSS = readFileSync(join(HERE, "report-canvas.css"), "utf8");
const SCHEMA_TS = readFileSync(
  join(HERE, "..", "lib", "canvas-pm-adapter", "schema.ts"),
  "utf8",
);

/** Strip /* *\/ comments so example selectors inside them never count as rules. */
function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

const CSS = stripComments(CANVAS_CSS);

/** schema.ts `export const TEXT_BASED_KINDS = [...] as const;` */
function schemaTextBasedKinds(): string[] {
  const m = SCHEMA_TS.match(/TEXT_BASED_KINDS\s*=\s*\[([^\]]*)\]/);
  if (!m) throw new Error("schema.ts 里找不到 TEXT_BASED_KINDS —— 它改名了？");
  return [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]).sort();
}

/** All kinds appearing as `[data-section-kind="x"]` in this file. */
function cssScopedKinds(): string[] {
  const kinds = new Set<string>();
  for (const m of CSS.matchAll(/\[data-section-kind="([a-z_]+)"\]/g)) {
    kinds.add(m[1]);
  }
  return [...kinds].sort();
}

/** [selector string, declaration block], including rules nested in `@media` and similar blocks. */
function rules(): Array<[string, string]> {
  return [...CSS.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map((m) => [m[1].trim(), m[2]] as [string, string])
    .filter(
      ([sel]) =>
        sel.length > 0 && !sel.startsWith("@") && !sel.startsWith(":root"),
    );
}

describe("散文层级的作用域", () => {
  it("CSS 认的 kind 与 schema 的 TEXT_BASED_KINDS 逐字一致", () => {
    expect(cssScopedKinds()).toEqual(schemaTextBasedKinds());
  });

  it("把列表标记**开出来**的规则一律带 kind 作用域", () => {
    // Check only marker restoration (decimal/disc/…), not `list-style: none`; components
    // independently disable their own markers as intended (e.g. .rc-prov-expand).
    const offenders = rules()
      .filter(([, decl]) => /list-style(-type)?:\s*(?!none)[a-z]/.test(decl))
      .map(([sel]) => sel)
      .filter((sel) => !sel.includes("data-section-kind"));
    expect(offenders).toEqual([]);
  });

  it("散文元素不裸挂在文档列上（`.canvas-report-article li` 这种形状）", () => {
    // Component-specific layout rules (`.canvas-report-article .v2-dt-wrap td`) are valid; allow them.
    // Reject direct element selectors **without any intermediate scope**: they leak into
    // sources / recommendations / chart tables and all other component sections.
    const PROSE_EL = /^[\s>]*(?:li|ol|ul|th|td|blockquote|h3|h4|h5|h6)\b/;
    const offenders = rules()
      .map(([sel]) => sel)
      .flatMap((sel) => sel.split(",").map((s) => s.trim()))
      .filter((sel) => {
        const at = sel.indexOf(".canvas-report-article");
        if (at < 0) return false;
        const tail = sel.slice(at + ".canvas-report-article".length);
        return PROSE_EL.test(tail);
      });
    expect(offenders).toEqual([]);
  });
});
