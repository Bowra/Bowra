/**
 * Contract: keys whose copy contains `{applicationName}` must be read through `useBrandedTranslations` (which passes the current brand's
 * application name), or the call site must pass `applicationName` explicitly. Reading them with plain `useTranslations` / `getTranslations` without the argument
 * makes next-intl raise FORMATTING_ERROR, leaks the key or a raw `{applicationName}` onto the page, and breaks white-labelling.
 *
 * Scan: list every message key with that argument, then scan source for translators bound by `const t = useTranslations("ns")` /
 * `getTranslations("ns" | { namespace: "ns" })` and report calls that hit those keys by literal key (namespace + sub key)
 * or template-string prefix without passing `applicationName`.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = path.resolve(__dirname, "..");
const SOURCE_DIRS = ["app", "components", "lib"];

function flatten(value: unknown, prefix = "", out: Record<string, string> = {}): Record<string, string> {
  if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      flatten(child, prefix ? `${prefix}.${key}` : key, out);
    }
  } else {
    out[prefix] = String(value);
  }
  return out;
}

function brandedKeys(): Set<string> {
  const keys = new Set<string>();
  for (const locale of ["zh-CN", "en-US", "de-DE", "fr-FR"]) {
    const catalog = JSON.parse(readFileSync(path.join(ROOT, "messages", `${locale}.json`), "utf-8"));
    for (const [key, text] of Object.entries(flatten(catalog))) {
      if (text.includes("{applicationName}")) keys.add(key);
    }
  }
  return keys;
}

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) sourceFiles(full, out);
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name)) out.push(full);
  }
  return out;
}

/** Call arguments (up to the matching closing parenthesis). */
function callArguments(source: string, openParen: number): string {
  let depth = 0;
  for (let index = openParen; index < source.length; index += 1) {
    const char = source[index];
    if (char === "(") depth += 1;
    else if (char === ")") {
      depth -= 1;
      if (depth === 0) return source.slice(openParen + 1, index);
    }
  }
  return source.slice(openParen + 1);
}

function findPlainUses(source: string, keys: Set<string>): string[] {
  const problems: string[] = [];
  const bindings = [
    ...source.matchAll(
      /(?:const|let)\s+(\w+)\s*=\s*(?:await\s+)?(?:useTranslations|getTranslations)\(\s*(?:"([^"]*)"|'([^']*)'|\{\s*[^}]*namespace:\s*"([^"]*)"[^}]*\})?\s*\)/g,
    ),
  ];
  for (const binding of bindings) {
    const name = binding[1];
    const namespace = binding[2] ?? binding[3] ?? binding[4] ?? "";
    const call = new RegExp(`\\b${name}(?:\\.(?:rich|markup))?\\(\\s*(?:"([^"]+)"|'([^']+)'|\`([^\`$]*)(\\$\\{)?)`, "g");
    for (const use of source.matchAll(call)) {
      const literal = use[1] ?? use[2] ?? (use[4] ? undefined : use[3]);
      const prefix = use[4] ? use[3] : undefined;
      const full = (sub: string) => (namespace ? `${namespace}.${sub}` : sub);
      const hit = literal !== undefined
        ? keys.has(full(literal))
          ? full(literal)
          : null
        : prefix !== undefined
          ? [...keys].find((key) => key.startsWith(full(prefix))) ?? null
          : null;
      if (!hit) continue;
      const args = callArguments(source, (use.index ?? 0) + use[0].indexOf("("));
      if (/\bapplicationName\b/.test(args)) continue;
      problems.push(hit);
    }
  }
  return problems;
}

describe("{applicationName} copy is read only through useBrandedTranslations", () => {
  const keys = brandedKeys();

  it("messages do contain {applicationName} keys (the scan is not vacuous)", () => {
    expect(keys.has("invite.ready.space_title")).toBe(true);
    expect(keys.has("auth.activate.title")).toBe(true);
  });

  it("the scanner detects plain useTranslations reading these keys (positive control)", () => {
    const sample = `const t = useTranslations("invite");\nreturn t("ready.space_title");\nconst s = useTranslations();\ns(\`invite.progress.step_\${n}.title\`);`;
    expect(findPlainUses(sample, keys)).toEqual(["invite.ready.space_title", "invite.progress.step_3.title"]);
    expect(findPlainUses(`const t = useTranslations("invite"); t("ready.space_title", { applicationName: name });`, keys)).toEqual([]);
  });

  it("no source reads these keys with plain useTranslations / getTranslations without applicationName", () => {
    const problems: string[] = [];
    for (const dir of SOURCE_DIRS) {
      for (const file of sourceFiles(path.join(ROOT, dir))) {
        for (const key of findPlainUses(readFileSync(file, "utf-8"), keys)) {
          problems.push(`${path.relative(ROOT, file)}: ${key}`);
        }
      }
    }
    expect(problems).toEqual([]);
  });
});
