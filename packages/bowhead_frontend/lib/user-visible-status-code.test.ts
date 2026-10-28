import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import de from "@/messages/de-DE.json";
import fr from "@/messages/fr-FR.json";
import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

type CatalogValue = string | number | boolean | null | CatalogValue[] | { [key: string]: CatalogValue };

function flatten(value: CatalogValue, prefix = "", out: Record<string, string> = {}): Record<string, string> {
  if (value === null || typeof value !== "object") {
    out[prefix] = String(value);
    return out;
  }
  for (const [key, child] of Object.entries(value as Record<string, CatalogValue>)) {
    flatten(child, prefix ? `${prefix}.${key}` : key, out);
  }
  return out;
}

// User-facing errors say what happened and what to do; HTTP/internal codes belong only in diagnostics (“复制诊断” or disclosure).
// Any status/error-code placeholder in following wording violates rule.
const STATUS_OR_CODE = new RegExp(
  [
    String.raw`\{(?:code|error_?code|errorCode|http_?status|httpStatus|statusCode)\}`,
    String.raw`HTTP\s*\{`,
    String.raw`HTTP\s*[1-5]\d\d`,
    String.raw`[（(]\s*[1-5]\d\d\s*[)）]`,
    // Parenthesized {status}: “（错误 {status}）”“（{status}）”“(error {status})”.
    String.raw`[（(][^）)]*\{status\}`,
    // 「(error {…})」「(Fehler {…})」
    String.raw`\((?:error|Fehler)\s*\{`,
  ].join("|"),
);

// Deliberate exceptions: not Bowhead’s own error wording.
const ALLOWED = new Set([
  // Model-connection validation panel: upstream model-service status codes belong in administrator diagnostics.
  "settings.models.v2.failure_http_status",
  // Email encryption port number, not status code.
  "settings.integrations.email_encryption_ssl",
]);

// Workbench/canvas: error hints must not use raw failure wording (“plan.revise 422: {…}”“canvas: 500”
// “rewrite 502: <response body>”) or concatenate status codes; choose localized wording by status/error code, diagnostics via “复制诊断”.
const WORKBENCH_SOURCES = [
  "components/chat",
  "components/canvas",
  "components/chat-workbench.tsx",
  "app/(app)/page.tsx",
  // Hint-emitting modules outside component tree (autosave, /remember).
  "lib/use-debounced-sync.ts",
  "lib/remember-slash.ts",
  "lib/report-save-errors.ts",
];
// Failure-object variable names (common catch parameters).
const ERR = String.raw`(?:err|e|error|cause|nextError|reason|failure|caught|ex)`;
// Within same statement (never across `;`), newlines allowed.
const SAME_STATEMENT = String.raw`[^;]{0,400}?`;
const RAW_FAILURE_IN_UI = new RegExp(
  [
    // UI exits directly using raw failure wording:
    // toast({ … text: err.message … }), setError(err.message), shownError(e.message, e), setErr(String(e)); may span lines.
    String.raw`\b(?:toast|setError|setErr|shownError|setBanner|setSlashMessage|showSilentFailure|setFailure)\(` + SAME_STATEMENT +
      String.raw`(?:\(\s*${ERR}(?:\s+as\s+\w+)?\s*\)|\b${ERR})\.message\b`,
    String.raw`\b(?:toast|setError|setErr|shownError|setBanner|setSlashMessage|showSilentFailure|setFailure)\(` + SAME_STATEMENT +
      String.raw`\bString\(\s*${ERR}\s*\)`,
    // Template interpolates raw failure: `保存失败：${err.message}` / `${(e as Error).message}`.
    // No variable-name restriction: `${(startError as Error).message}` also counts.
    String.raw`\$\{\s*(?:\(\s*\w+\s+as\s+\w+\s*\)|\w+)\.message\s*\}`,
    // Store raw wording in variable before exit: const message = e instanceof Error ? e.message : String(e); setFailure(…, message)
    String.raw`\b(?:const|let)\s+\w+\s*=\s*(?:${ERR}\s+instanceof\s+Error\s*\?\s*${ERR}\.message|\(\s*${ERR}\s+as\s+\w+\s*\)\.message|${ERR}\.message)\b`,
    // t("…", { err: String(error) }) / { err: (err as Error).message }
    String.raw`\{\s*(?:err|error|reason):\s*(?:String\(|\(?\s*${ERR}(?:\s+as\s+\w+)?\s*\)?\.message)`,
    // “HTTP 500” concatenated into UI wording.
    String.raw`HTTP \$\{`,
  ].join("|"),
  "g",
);

export function rawFailureOffenders(source: string): string[] {
  return [...source.matchAll(RAW_FAILURE_IN_UI)].map((match) => match[0].replace(/\s+/g, " ").slice(0, 160));
}

function sourceFiles(relative: string): string[] {
  const absolute = join(ROOT, relative);
  if (statSync(absolute).isFile()) return [relative];
  return readdirSync(absolute, { recursive: true, encoding: "utf8" })
    .filter((name) => /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name))
    .map((name) => join(relative, name));
}

// Individually reviewed non-failure raw text (file + matching fragment), not error hints:
const RAW_FAILURE_ALLOWED = new Set([
  // Composer draft merge: latest is draft saved in another tab, message is user-entered body.
  "components/chat/v2-composer.tsx: ${latest.message}",
  // Local attachment validation classification: msg detects oversize/unsupported type only; upload failures already localized by fileError above.
  "components/chat/v2-composer.tsx: const msg = e instanceof Error ? e.message",
  // Workflow solidification: oralError is localized presentation from semanticSkillWorkflowErrorPresentation, not failure object.
  "components/canvas/sections/create-workflow-card.tsx: ${oralError.message}",
]);

describe("workbench and canvas error prompts do not render raw failures", () => {
  it("no toast / error text is built from err.message, String(error) or HTTP ${status}", () => {
    const offenders = WORKBENCH_SOURCES.flatMap(sourceFiles).flatMap((file) =>
      rawFailureOffenders(readFileSync(join(ROOT, file), "utf8")).map((text) => `${file}: ${text}`),
    ).filter((entry) => !RAW_FAILURE_ALLOWED.has(entry));
    expect(offenders).toEqual([]);
  });

  // Positive controls: guard must recognize these forms (including multiline), otherwise zero-violations result meaningless.
  it.each([
    ["toast text across lines", 'toast({\n  kind: "error",\n  text: err instanceof Error\n    ? err.message\n    : t("x"),\n});'],
    ["setError with message", "setError(cause instanceof Error ? cause.message : String(cause));"],
    ["setError across lines", "setError(\n  (nextError as Error).message,\n);"],
    ["shownError with message", "setError(shownError(e.message, e));"],
    ["String(err) into setErr", "setErr(String(e));"],
    ["template with message", "const text = `保存失败：${(e as Error).message}`;"],
    ["template with any variable name", "const text = `${(startError as Error).message}`;"],
    ["setFailure with message", "const message = e instanceof Error ? e.message : String(e); setFailure(sectionId, message);"],
    ["t() with err", 't("hdr_clear_failed", { err: String(error) })'],
    ["HTTP status", "throw new Error(`HTTP ${r.status}`);"],
  ] as const)("recognizes %s", (_name, snippet) => {
    expect(rawFailureOffenders(snippet)).not.toEqual([]);
  });

  it.each([
    ["localized toast", 'toast({ kind: "error", text: t(planActionErrorKey(err, "skip")), action: copyDiagnosticsAction(err, "chat") });'],
    ["localized shownError", 'setError(shownError(t("load_failed_plain"), e));'],
    ["log only", 'console.warn("failed", String(error));'],
  ] as const)("leaves %s alone", (_name, snippet) => {
    expect(rawFailureOffenders(snippet)).toEqual([]);
  });
});

describe("user-visible copy carries no HTTP status or internal error code", () => {
  it.each([
    ["zh-CN", zh],
    ["en-US", en],
    ["de-DE", de],
    ["fr-FR", fr],
  ] as const)("%s catalog", (_locale, catalog) => {
    const leaked = Object.entries(flatten(catalog as unknown as CatalogValue)).filter(
      ([key, value]) => !ALLOWED.has(key) && STATUS_OR_CODE.test(value),
    );
    expect(leaked).toEqual([]);
  });
});
