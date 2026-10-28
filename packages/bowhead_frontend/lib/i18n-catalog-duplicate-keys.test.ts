/**
 * JSON in the messages directory must not contain duplicate keys.
 *
 * `JSON.parse` and `import x from "*.json"` silently keep the last duplicate key in an object; key-set comparison
 * (`i18n-catalog-integrity.test.ts`) thus cannot detect it: identical blocks pass, while copy edited in one
 * may be silently overridden by another. Scan source text directly, tracking seen keys per object.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

const MESSAGES_ROOT = join(__dirname, "..", "messages");

function jsonFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...jsonFiles(path));
    else if (entry.name.endsWith(".json")) out.push(path);
  }
  return out.sort();
}

/** Return “object path.key” for every duplicate key. Recognizes valid JSON only: strings, objects, arrays and scalars. */
export function duplicateJsonKeys(text: string): string[] {
  const duplicates: string[] = [];
  // Each stack level: objects use { keys, path }, arrays use null.
  const stack: Array<{ keys: Set<string>; path: string } | null> = [];
  let pendingKey: string | null = null;
  let i = 0;
  const readString = (): string => {
    let value = "";
    i += 1; // Opening quote
    while (i < text.length && text[i] !== '"') {
      if (text[i] === "\\") {
        value += text.slice(i, i + 2);
        i += 2;
        continue;
      }
      value += text[i];
      i += 1;
    }
    i += 1; // Closing quote
    return JSON.parse(`"${value}"`) as string;
  };
  while (i < text.length) {
    const ch = text[i];
    if (ch === '"') {
      const value = readString();
      let j = i;
      while (j < text.length && /\s/.test(text[j])) j += 1;
      const top = stack.at(-1);
      if (text[j] === ":" && top) {
        if (top.keys.has(value)) duplicates.push(top.path ? `${top.path}.${value}` : value);
        top.keys.add(value);
        pendingKey = value;
      }
      continue;
    }
    if (ch === "{") {
      const parent = stack.at(-1);
      const path = parent && pendingKey !== null ? (parent.path ? `${parent.path}.${pendingKey}` : pendingKey) : parent?.path ?? "";
      stack.push({ keys: new Set(), path });
    } else if (ch === "[") {
      stack.push(null);
    } else if (ch === "}" || ch === "]") {
      stack.pop();
    }
    i += 1;
  }
  return duplicates;
}

describe("messages 不允许重复键", () => {
  it("扫描器能认出同一对象里的重复键，不把不同对象里的同名键当重复", () => {
    expect(duplicateJsonKeys('{"a": {"x": 1, "y": "\\"x\\": 2", "x": 3}, "b": {"x": 1}}')).toEqual(["a.x"]);
    expect(duplicateJsonKeys('{"a": [{"x": 1}, {"x": 2}], "x": 1}')).toEqual([]);
  });

  it.each(jsonFiles(MESSAGES_ROOT).map((path) => [relative(MESSAGES_ROOT, path), path]))("%s", (_name, path) => {
    expect(duplicateJsonKeys(readFileSync(path, "utf8"))).toEqual([]);
  });
});
