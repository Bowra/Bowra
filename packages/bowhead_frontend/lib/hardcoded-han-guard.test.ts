/**
 * Guard: cleaned files must not regain hardcoded user-facing Chinese.
 *
 * Scan string literals, template strings, and JSX text with TypeScript AST (comments naturally excluded),
 * matching i18n-cleanup scanner, with more precise locale-branch detection:
 *   · Allow Chinese under ancestor `"zh-CN"` only if **entire ancestor chain lacks** `"en-US"` / `"de-DE"` / `"fr-FR"`,
 *     regardless of intervening object/function depth (locale lookup table like `{ "zh-CN": { fail: (e) => `失败：${e}` } }`);
 *     object property keys; strings in logging calls (console.*, etc.).
 *   · Forbid Chinese elsewhere, or beneath **any** `"en-US"` / `"de-DE"` / `"fr-FR"` ancestor
 *     (even `{ "en-US": { x: { "zh-CN": "藏" } } }` nesting a Chinese key inside an English table).
 *
 * Two modes:
 *   · GUARDED_FILES: generated locale-branch tables use the branch criterion above.
 *   · ZERO_HAN_FILES: handwritten modules fully migrated to messages. **No Han character** beyond comments,
 *     including strings/templates/JSX/property keys/logging; restoring Chinese after cleanup fails immediately.
 *     Add newly cleaned files here.
 *
 * Excluded: `lib/scenarios.ts` is development/e2e mock fixture, used only with NEXT_PUBLIC_BOWHEAD_SSE_MODE=mock,
 * untranslated; absent from both lists, with mock-only referencing enforced by `lib/scenarios.test.ts`.
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import ts from "typescript";
import { describe, expect, it } from "vitest";

const FRONTEND_ROOT = path.resolve(__dirname, "..");

/** Batch 1: generated tool-name table (tool_aliases.yaml → codegen). */
const GUARDED_FILES = ["lib/_generated/tool-display.ts"];

/**
 * Cleaned files requiring zero Han (comments excepted). Batch 1: tool labels/step-card catalog; batch 2: workflow solidification;
 * batch 3B: miscellaneous files; batch 3C: chat-send failures/error wording; coded file categories.
 */
const ZERO_HAN_FILES = [
  "lib/tool-labels.ts",
  "components/chat/step-list/tool-catalog.ts",
  "components/workflow-solidify/solidify-card.tsx",
  "components/workflow-solidify/manage-page.tsx",
  "components/workflow-solidify/wf-shared.tsx",
  "components/workflow-solidify/turn-chip.tsx",
  "components/workflow-solidify/suggestions.tsx",
  "app/(app)/spike/workflow-solidify/page.tsx",
  "components/canvas/sections/unknown-section.tsx",
  "components/knowledge/kb-file-types.ts",
  "components/settings/models.tsx",
  "components/settings/tools/powerbi-tool-section.tsx",
  "lib/canvas-pm-adapter/extensions/_helpers.tsx",
  "lib/canvas-pm-adapter/extensions/citation-popover.tsx",
  "lib/canvas-pm-adapter/format-num.ts",
  "lib/chart/ref-line-plugin.ts",
  "lib/intent-mapping.ts",
  "lib/prettify-step-label.ts",
  "lib/report-share.ts",
  "lib/report-title.ts",
  "lib/use-space.ts",
  "lib/canvas-pm-adapter/extensions/citation-plugin.ts",
  "lib/chat-send-errors.ts",
  "components/chat/send-init-error-banner.tsx",
  "lib/error-messages.ts",
  "lib/entitlement-error.ts",
  "lib/transport-health.ts",
  // File categories become locale-neutral codes (display wording in messages api_client.file_categories).
  "lib/api.ts",
  "lib/files-taxonomy.ts",
  "components/files/file-drawer.tsx",
  // Batch D: previous Chinese/other or English/other binary branches; wording moved to messages.
  "components/chat/step-list/agent-meta.ts",
  "lib/auth-api.ts",
  "lib/canvas-navigation-guard.ts",
  "lib/canvas-pm-adapter/extensions/section-delete-guard.ts",
  "lib/report-save-errors.ts",
  "lib/use-debounced-sync.ts",
  "lib/skill-workflow-error.ts",
  "lib/team-subagents-api.ts",
];

const HAN = /\p{Script=Han}/u;
const LOG_CALLEE = /^(console\.\w+|logger\.\w+|log|debug|warn|trace|captureException|reportError)$/;
// Accept only full UI locale keys: ad hoc `zh` is not a Chinese branch (or `({ zh: "完成" }).zh`
// could sneak Chinese into English tables undetected).
const ZH_BRANCH = /^["']?zh-CN["']?$/;
const OTHER_LOCALE_BRANCH = /^["']?(en-US|de-DE|fr-FR)["']?$/;

function propertyName(node: ts.Node, sf: ts.SourceFile): string | null {
  if (ts.isPropertyAssignment(node) || ts.isPropertySignature(node)) return node.name.getText(sf);
  return null;
}

/**
 * Locale branch: inspect entire ancestor chain; any `"en-US"` / `"de-DE"` / `"fr-FR"` means "other"
 * (even if nearer ancestor is `"zh-CN"`); otherwise `"zh-CN"` means "zh"; neither means null.
 */
function localeBranch(node: ts.Node, sf: ts.SourceFile): "zh" | "other" | null {
  let zh = false;
  for (let cur: ts.Node | undefined = node.parent; cur; cur = cur.parent) {
    const name = propertyName(cur, sf);
    if (name && OTHER_LOCALE_BRANCH.test(name)) return "other";
    if (name && ZH_BRANCH.test(name)) zh = true;
  }
  return zh ? "zh" : null;
}

function inLogCall(node: ts.Node, sf: ts.SourceFile): boolean {
  for (let cur: ts.Node | undefined = node.parent; cur; cur = cur.parent) {
    if (ts.isCallExpression(cur) && LOG_CALLEE.test(cur.expression.getText(sf))) return true;
    if (ts.isImportDeclaration(cur) || ts.isExportDeclaration(cur) || ts.isLiteralTypeNode(cur)) return true;
  }
  return false;
}

interface HanViolation {
  file: string;
  line: number;
  text: string;
  reason: "hardcoded" | "chinese-in-other-locale" | "han-in-zero-han-file";
}

/**
 * Zero-Han mode: traverse every AST leaf token (strings/template fragments/JSX/identifiers/property keys…).
 * Leaf text excludes leading trivia, naturally omitting comments; skip entire JSDoc nodes.
 */
function scanAnyHan(relFile: string, source: string): HanViolation[] {
  const kind = relFile.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(relFile, source, ts.ScriptTarget.Latest, true, kind);
  const out: HanViolation[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isJSDoc(node)) return;
    const children = node.getChildren(sf);
    if (children.length === 0) {
      const text = node.getText(sf);
      if (HAN.test(text)) {
        out.push({
          file: relFile,
          line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1,
          text: text.replace(/\s+/g, " ").trim().slice(0, 80),
          reason: "han-in-zero-han-file",
        });
      }
      return;
    }
    children.forEach(visit);
  };
  visit(sf);
  return out;
}

function scanHardcodedHan(relFile: string, source: string): HanViolation[] {
  const kind = relFile.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(relFile, source, ts.ScriptTarget.Latest, true, kind);
  const out: HanViolation[] = [];
  const visit = (node: ts.Node): void => {
    let value: string | null = null;
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) value = node.text;
    else if (ts.isTemplateExpression(node)) value = node.getText(sf);
    else if (ts.isJsxText(node)) value = node.getText(sf).trim();
    if (value && HAN.test(value)) {
      const isKey = propertyName(node.parent, sf) !== null && (node.parent as ts.PropertyAssignment).name === node;
      if (!isKey && !inLogCall(node, sf)) {
        const branch = localeBranch(node, sf);
        if (branch !== "zh") {
          out.push({
            file: relFile,
            line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1,
            text: value.replace(/\s+/g, " ").slice(0, 80),
            reason: branch === "other" ? "chinese-in-other-locale" : "hardcoded",
          });
        }
      }
      if (ts.isTemplateExpression(node)) return;
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return out;
}

describe("已清理文件不再写死中文", () => {
  it.each(GUARDED_FILES)("%s", (relFile) => {
    const source = readFileSync(path.join(FRONTEND_ROOT, relFile), "utf8");
    // Anti-vacuity: file was read and actually contains Chinese (inside Chinese branch).
    expect(HAN.test(source), `${relFile} 没有任何中文——路径写错了或守卫空转`).toBe(true);
    expect(scanHardcodedHan(relFile, source)).toEqual([]);
  });

  it("判据本身：写死的中文、英文分支里的中文会被抓到，中文分支里的不会", () => {
    const sample = `
      const T = { "zh-CN": { a: "好", f: (n: number) => \`共 \${n} 个\` }, "en-US": { a: "坏", b: ({ zh: "藏" }).zh } };
      const U = { "de-DE": { x: { "zh-CN": "套一层" } }, "zh-CN": { y: { "en-US": { z: "反套" } } } };
      const V = { "fr-FR": { a: "法语表里的中文", b: { "zh-CN": "法语里套中文" } } };
      const bad = "写死";
      console.warn("日志不算");
      const key = { "键不算": 1 };
      function g() { return \`拿到 \${1} 个\`; }
    `;
    expect(scanHardcodedHan("sample.ts", sample).map((v) => [v.text, v.reason])).toEqual([
      ["坏", "chinese-in-other-locale"],
      ["藏", "chinese-in-other-locale"],
      // Nearest ancestor is "zh-CN", outer ancestor "de-DE": still Chinese in German table.
      ["套一层", "chinese-in-other-locale"],
      ["反套", "chinese-in-other-locale"],
      ["法语表里的中文", "chinese-in-other-locale"],
      ["法语里套中文", "chinese-in-other-locale"],
      ["写死", "hardcoded"],
      ["`拿到 ${1} 个`", "hardcoded"],
    ]);
  });
});

describe("已清干净的文件必须零汉字（注释除外）", () => {
  it.each(ZERO_HAN_FILES)("%s", (relFile) => {
    const source = readFileSync(path.join(FRONTEND_ROOT, relFile), "utf8");
    // Anti-vacuity: file parses and criterion detects violations; appending one Chinese line must produce exactly one finding.
    expect(scanAnyHan(relFile, `${source}\nexport const __probe = "探针";\n`)).toHaveLength(1);
    expect(scanAnyHan(relFile, source)).toEqual([]);
  });

  it("判据本身：注释放过，字符串、模板、JSX 文本、属性键、日志调用、中文分支里的中文都算", () => {
    const sample = `
      // 行注释不算
      /** JSDoc 不算 */
      /* 块注释不算 */
      const a = "字符串";
      const b = \`模板 \${a} 尾\`;
      const c = { "键": 1, "zh-CN": "分支" };
      console.warn("日志");
      const d = <span title="属性">文本</span>;
    `;
    expect(scanAnyHan("sample.tsx", sample).map((v) => v.text)).toEqual([
      '"字符串"',
      "`模板 ${",
      "} 尾`",
      '"键"',
      '"分支"',
      '"日志"',
      '"属性"',
      "文本",
    ]);
  });
});
