/**
 * `crypto.randomUUID` is allowed only in `lib/random-id.ts`.
 *
 * Secure-context-only API, `undefined` on plain-HTTP deployments (116 prod), throwing on bare calls.
 * Before 2026-08-14, 7 calls existed; 6 had handwritten guards, **1 was missed**—that omission
 * (`settings/models.tsx` verification-confirm button) prevented 116 capability verification from ever succeeding since database creation.
 * Unit tests cannot catch a forgotten guard in some file; source scanning can.
 *
 * Criterion is **literal source**, not behavior: a new call may happen to run in a
 * secure context in every unit test (jsdom defaults to one), passing all behavior tests while failing only this guard.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = path.resolve(__dirname, "..");
const SKIP_DIRS = new Set(["node_modules", ".next", "out", "coverage", ".turbo"]);
const SOURCE_EXT = new Set([".ts", ".tsx"]);
/** The only **shipped code** allowed to access the native API directly (the helper itself). */
const ALLOWED = new Set(["lib/random-id.ts"]);

/** Rule covers shipped code; tests/e2e stubs are not bundled, and stubbing crypto is legitimate. */
function isShipped(rel: string): boolean {
  return !/\.test\.tsx?$/.test(rel) && !rel.startsWith("e2e/");
}

function sourceFiles(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry) || entry.startsWith(".")) continue;
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      sourceFiles(full, acc);
    } else if (SOURCE_EXT.has(path.extname(entry))) {
      acc.push(full);
    }
  }
  return acc;
}

/** Mentioning the API in comments is allowed (this rule itself needs explanation in comments). */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

/**
 * Match **actual calls** only, with any receiver: `crypto.randomUUID(`, `globalThis.crypto?.randomUUID?.(`,
 * or `const c = globalThis.crypto` followed by `c.randomUUID()`.
 *
 * Initially anchored to `crypto.`; positive control immediately failed because the helper itself uses a local variable,
 * so that version could be bypassed with an alias (exactly feedback_assert_absence_needs_positive_control's
 * warning: passing after guard removal means a dead test).
 */
const OFFENDING_CALL = /\.\s*randomUUID\s*\??\.?\s*\(/;

describe("crypto.randomUUID 收口", () => {
  it("扫描本身是活的（positive control）", () => {
    // Without this, an empty-directory scan / missed extension / overstripped comments makes the test below always pass.
    const helper = readFileSync(path.join(ROOT, "lib/random-id.ts"), "utf-8");
    expect(OFFENDING_CALL.test(stripComments(helper))).toBe(true);
    expect(sourceFiles(ROOT).filter((f) => isShipped(path.relative(ROOT, f))).length)
      .toBeGreaterThan(200);
  });

  it("除 helper 外没有任何发布代码直接调用 crypto.randomUUID", () => {
    const offenders = sourceFiles(ROOT)
      .map((file) => path.relative(ROOT, file).split(path.sep).join("/"))
      .filter((rel) => isShipped(rel) && !ALLOWED.has(rel))
      .filter((rel) => OFFENDING_CALL.test(stripComments(readFileSync(path.join(ROOT, rel), "utf-8"))));

    expect(offenders, `请改用 lib/random-id.ts 的 randomUuid()：${offenders.join(", ")}`).toEqual([]);
  });
});
