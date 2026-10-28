/**
 * chat-ux-2026-08 batch F—self-referential CSS variable guard.
 *
 * Actual incident: `globals.css` contained
 *     --c-warn-soft: var(--c-warn-soft, #F4E8DA);
 * Intent clearly was retain existing value, otherwise fallback. CSS self-reference is a cycle,
 * making the declaration **guaranteed-invalid**—it does not preserve the old value; it
 * **erases the defined value** from preceding :root / dark rules.
 *
 * Result: every fallback-free `var(--c-warn-soft)` in the repository rendered without background,
 * including the amber retry badge on step rows (batch B's retry signal itself).
 * Unit tests/typecheck/build all passed—only browser probes of each token exposed it.
 *
 * Guard **this pattern**, not just this token: forbid every self-reference.
 * Mutation check: reintroducing that line must fail.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const CSS = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "globals.css"), "utf8");

/** `--x: ... var(--x ...) ...`—declaration body references itself. */
function selfReferentialDeclarations(css: string): string[] {
  const hits: string[] = [];
  // Scan each declaration; strip comment examples first, or the explanation at this file's top would trigger itself.
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const decl = /(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]*)/g;
  let m: RegExpExecArray | null;
  while ((m = decl.exec(stripped)) !== null) {
    const [, name, body] = m;
    // ⚠ Do not use `\b`: `-` is nonword, so `--c\b` falsely matches `var(--c-crit)`.
    // Explicitly require the name **not** to be followed by an identifier character.
    if (new RegExp(`var\\(\\s*${name}(?![A-Za-z0-9_-])`).test(body)) hits.push(`${name}: ${body.trim()}`);
  }
  return hits;
}

describe("globals.css — 没有自引用的自定义属性", () => {
  it("★ 自引用会让整条声明 guaranteed-invalid，把已有值抹掉而不是兜底", () => {
    expect(selfReferentialDeclarations(CSS)).toEqual([]);
  });

  it("检测器本身有效（否证面：造一条自引用必须被逮到）", () => {
    expect(
      selfReferentialDeclarations(":root { --c-warn-soft: var(--c-warn-soft, #F4E8DA); }"),
    ).toEqual(["--c-warn-soft: var(--c-warn-soft, #F4E8DA)"]);
    // Referencing **another** variable is valid; no false positives.
    expect(selfReferentialDeclarations(":root { --a: var(--b, 1px); }")).toEqual([]);
    // Shared name prefixes must not cause false positives either (--c-warn vs --c-warn-soft).
    expect(selfReferentialDeclarations(":root { --c-warn-soft: var(--c-warn); }")).toEqual([]);
  });
});
