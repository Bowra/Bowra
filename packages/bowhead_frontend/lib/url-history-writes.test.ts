/**
 * Every page-owned history write must keep Next routing aligned with the real URL (see `withoutNextRouterHistoryMarkers`).
 *
 * Calling `replaceState` / `pushState` with `window.history.state` (containing Next's `__NA`) makes Next
 * pass through directly, leaving `useSearchParams` at the old URL: enter `?session=s2` → click s3 in sidebar → notification returns to s2,
 * while the page stays at s3. Scan source here:
 * - History writes must use `window.history.replaceState(` / `window.history.pushState(` exclusively
 *   (aliases, `globalThis.history`, indexed calls, `.call/.apply/.bind` evade scanning and are all forbidden);
 * - First argument must pass through `withoutNextRouterHistoryMarkers` or be fresh state already lacking markers
 *   (`null` / `{}`).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");
const SCANNED_DIRS = ["app", "components", "lib"];
const FRESH_STATE = new Set(["null", "{}"]);
const ALLOWED_CALL = /window\.history\.(replaceState|pushState)\s*\(/g;

/**
 * These writes precede Next's history patch (public-page first-frame effect; child effects run before AppRouter);
 * removing markers loses `__NA` on this entry, making Next reload the whole page on later back/forward navigation. Preserve unchanged.
 */
const BEFORE_ROUTER_PATCH = new Set(["app/(public)/login/page.tsx"]);

/** Strip comments and replace string contents with spaces (`//` and `"pushState"` inside strings are not code). */
function stripComments(source: string): string {
  let out = "";
  let index = 0;
  while (index < source.length) {
    const char = source[index]!;
    const next = source[index + 1];
    if (char === "/" && next === "/") {
      while (index < source.length && source[index] !== "\n") index += 1;
      continue;
    }
    if (char === "/" && next === "*") {
      const end = source.indexOf("*/", index + 2);
      index = end === -1 ? source.length : end + 2;
      continue;
    }
    if (char === "\"" || char === "'" || char === "`") {
      let end = index + 1;
      while (end < source.length && source[end] !== char) end += source[end] === "\\" ? 2 : 1;
      out += char + source.slice(index + 1, end).replace(/[^\n]/g, " ") + (source[end] ?? "");
      index = end + 1;
      continue;
    }
    out += char;
    index += 1;
  }
  return out;
}

function firstArgument(source: string, start: number): string {
  let depth = 0;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (char === "(" || char === "[" || char === "{") depth += 1;
    else if (char === ")" || char === "]" || char === "}") {
      if (depth === 0) return source.slice(start, index).trim();
      depth -= 1;
    } else if (char === "," && depth === 0) {
      return source.slice(start, index).trim();
    }
  }
  throw new Error("unterminated call");
}

const lineOf = (source: string, index: number) => source.slice(0, index).split("\n").length;

/** Return violation descriptions; an empty array means this source complies. */
export function historyWriteViolations(rawSource: string): string[] {
  const source = stripComments(rawSource);
  const violations: string[] = [];
  const allowedStarts = new Set<number>();
  for (const match of source.matchAll(ALLOWED_CALL)) {
    const methodIndex = match.index! + "window.history.".length;
    allowedStarts.add(methodIndex);
    const argument = firstArgument(source, match.index! + match[0].length);
    if (!argument.startsWith("withoutNextRouterHistoryMarkers(") && !FRESH_STATE.has(argument)) {
      violations.push(`${lineOf(source, match.index!)}: 第一个参数没有去掉 Next 标记：${argument.slice(0, 60)}`);
    }
  }
  for (const match of source.matchAll(/\b(replaceState|pushState)\b/g)) {
    if (!allowedStarts.has(match.index!)) {
      violations.push(`${lineOf(source, match.index!)}: 只允许 window.history.${match[1]}(...) 直接调用`);
    }
  }
  for (const match of source.matchAll(/\bhistory\s*\[|\b(globalThis|self|window\.window)\.history\b/g)) {
    violations.push(`${lineOf(source, match.index!)}: 不允许 ${match[0]} 这种写法`);
  }
  for (const match of source.matchAll(/=\s*(window\.)?history\s*[;,)\n]|\{[^}]*\bhistory\b[^}]*\}\s*=\s*window\b/g)) {
    violations.push(`${lineOf(source, match.index!)}: 不允许给 history 起别名`);
  }
  return violations;
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    if (!/\.(ts|tsx)$/.test(name) || /\.test\.tsx?$/.test(name)) return [];
    return [path];
  });
}

function scannedSources() {
  return SCANNED_DIRS.flatMap((dir) => sourceFiles(join(ROOT, dir))).map((path) => ({
    file: relative(ROOT, path),
    source: readFileSync(path, "utf8"),
  }));
}

describe("扫描器自测", () => {
  it.each([
    ["不带 window", "history.replaceState(withoutNextRouterHistoryMarkers(s), \"\", url);"],
    ["别名", "const h = window.history;\nh.replaceState(withoutNextRouterHistoryMarkers(s), \"\", url);"],
    [".call", "window.history.replaceState.call(window.history, withoutNextRouterHistoryMarkers(s), \"\", url);"],
    ["下标", "const m = \"replaceState\";\nwindow.history[m](withoutNextRouterHistoryMarkers(s), \"\", url);"],
    ["globalThis", "globalThis.history.pushState(withoutNextRouterHistoryMarkers(s), \"\", url);"],
    ["带着标记", "window.history.replaceState(window.history.state, \"\", url);"],
  ])("%s → 违规", (_label, source) => {
    expect(historyWriteViolations(source)).not.toEqual([]);
  });

  it.each([
    ["去掉标记", "window.history.replaceState(\n  withoutNextRouterHistoryMarkers(window.history.state),\n  \"\",\n  url,\n);"],
    ["全新状态", "window.history.pushState(null, \"\", url);\nwindow.history.replaceState({}, \"\", url);"],
    ["只读", "const state = window.history.state;\nwindow.history.back();"],
    ["注释与字符串里的提及", "// history.replaceState(x) 不算\nconst doc = \"see https://x/pushState\";"],
  ])("%s → 合规", (_label, source) => {
    expect(historyWriteViolations(source)).toEqual([]);
  });
});

describe("页面自己写历史记录", () => {
  it("只用 window.history.replaceState / pushState，第一个参数去掉 Next 标记或是全新状态", () => {
    const sources = scannedSources();
    // Positive control: scanning finds actual session-URL synchronization and results navigation writes.
    const count = (file: string) => [...stripComments(sources.find((entry) => entry.file === file)!.source).matchAll(ALLOWED_CALL)].length;
    expect(count("app/(app)/page.tsx")).toBeGreaterThanOrEqual(2);
    expect(count("components/chat-workbench.tsx")).toBeGreaterThanOrEqual(10);

    const offenders = sources
      .filter((entry) => !BEFORE_ROUTER_PATCH.has(entry.file))
      .flatMap((entry) => historyWriteViolations(entry.source).map((violation) => `${entry.file}:${violation}`));
    expect(offenders).toEqual([]);
  });

  it("首帧写入的例外仍然存在（文件改动后要重新判断它是否还需要例外）", () => {
    for (const file of BEFORE_ROUTER_PATCH) {
      const entry = scannedSources().find((candidate) => candidate.file === file);
      expect(entry && [...stripComments(entry.source).matchAll(ALLOWED_CALL)].length).toBeGreaterThan(0);
    }
  });
});
