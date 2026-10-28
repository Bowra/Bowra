import { readFileSync, readdirSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { describe, expect, it } from "vitest";

const FRONTEND_ROOT = join(__dirname, "..");
const SOURCE_ROOTS = [
  join(FRONTEND_ROOT, "app"),
  join(FRONTEND_ROOT, "components"),
  join(FRONTEND_ROOT, "lib"),
];
const METADATA_WRITER_ALLOWLIST = new Set([
  "lib/brand-bootstrap-script.ts",
  "lib/brand-metadata-bootstrap-script.ts",
  "lib/brand-metadata.ts",
]);
const EFFECTIVE_THEME_FETCH_ALLOWLIST = new Set([
  "lib/api.ts",
  "lib/use-theme-config.ts",
]);
const EFFECTIVE_THEME_APPLY_ALLOWLIST = new Set([
  "lib/brand-runtime.ts",
  "lib/use-theme-config.ts",
]);
const BRAND_RUNTIME_FILES = [
  "components/brand-metadata-controller.tsx",
  "components/brand-provider.tsx",
  "components/theme-bootstrap.tsx",
  "lib/brand-bootstrap-script.ts",
  "lib/brand-metadata-bootstrap-script.ts",
  "lib/brand-metadata.ts",
  "lib/brand-runtime.ts",
  "lib/effective-theme-request-guard.ts",
  "lib/page-title-store.ts",
  "lib/use-document-title.ts",
  "lib/use-theme-config.ts",
];

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    if (
      !/\.(?:ts|tsx)$/.test(entry.name) ||
      entry.name.endsWith(".d.ts") ||
      entry.name.includes(".test.")
    ) {
      return [];
    }
    return [path];
  });
}

function relativePath(path: string): string {
  return relative(FRONTEND_ROOT, path).split(sep).join("/");
}

describe("brand metadata architecture guard", () => {
  it("keeps title and favicon DOM writes inside the fixed-node owners", () => {
    const violations: string[] = [];
    const metadataSink =
      /document\.title\s*=|createElement\(\s*["']title["']|bowhead-document-title|bowhead-runtime-favicon|data-bowhead-(?:public|brand)-favicon|\.rel\s*=\s*["']icon["']|setAttribute\(\s*["']rel["']\s*,\s*["']icon["']/;

    for (const path of SOURCE_ROOTS.flatMap(sourceFiles)) {
      const source = readFileSync(path, "utf8");
      const file = relativePath(path);
      if (
        metadataSink.test(source) &&
        !METADATA_WRITER_ALLOWLIST.has(file)
      ) {
        violations.push(`${file}: metadata DOM sink outside allowlist`);
      }
    }

    expect(violations).toEqual([]);
  });

  it("keeps effective-theme fetch and apply behind the team guard", () => {
    const violations: string[] = [];

    for (const path of SOURCE_ROOTS.flatMap(sourceFiles)) {
      const source = readFileSync(path, "utf8");
      const file = relativePath(path);
      if (
        /\bgetEffectiveTheme\s*\(/.test(source) &&
        !EFFECTIVE_THEME_FETCH_ALLOWLIST.has(file)
      ) {
        violations.push(`${file}: unguarded effective-theme fetch`);
      }
      if (
        /\bapplyBrandBundleToDocument\s*\(/.test(source) &&
        !EFFECTIVE_THEME_APPLY_ALLOWLIST.has(file)
      ) {
        violations.push(`${file}: brand apply outside guarded owners`);
      }
    }

    expect(violations).toEqual([]);
  });

  it("forbids metadata observers, polling and deferred finalizers", () => {
    const violations: string[] = [];
    const forbidden =
      /\bMutationObserver\b|\bsetInterval\s*\(|\brequestAnimationFrame\s*\(|\bDOMContentLoaded\b/;

    for (const file of BRAND_RUNTIME_FILES) {
      const source = readFileSync(join(FRONTEND_ROOT, file), "utf8");
      if (forbidden.test(source)) {
        violations.push(`${file}: observer, polling or deferred finalizer`);
      }
    }

    expect(violations).toEqual([]);
  });
});
