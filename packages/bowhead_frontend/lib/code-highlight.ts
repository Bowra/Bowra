/**
 * Shared syntax highlighting for every surface that shows code (chat Markdown fences, report code blocks,
 * execution steps, run sources, artifact text previews, and the report editor).
 *
 * - One lowlight instance with a curated grammar set keeps the bundle bounded and every surface consistent.
 * - Output is a hast tree of `<span class="hljs-*">` and text nodes only; React and ProseMirror render it as
 *   escaped text, so highlighting never introduces markup from the code itself.
 * - Colors come from the `--c-code-*` theme tokens in `styles/code-highlight.css`.
 * - Unlabeled or unknown languages stay plain: guessing would color prose or tool output as code.
 */
import { createElement, type ReactNode } from "react";
import type { Element, ElementContent, Root, RootContent } from "hast";
import { createLowlight } from "lowlight";
import { visit } from "unist-util-visit";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import markdown from "highlight.js/lib/languages/markdown";
import php from "highlight.js/lib/languages/php";
import plaintext from "highlight.js/lib/languages/plaintext";
import python from "highlight.js/lib/languages/python";
import r from "highlight.js/lib/languages/r";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import scala from "highlight.js/lib/languages/scala";
import scss from "highlight.js/lib/languages/scss";
import sql from "highlight.js/lib/languages/sql";
import swift from "highlight.js/lib/languages/swift";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

export const codeLowlight = createLowlight({
  bash,
  c,
  cpp,
  csharp,
  css,
  diff,
  dockerfile,
  go,
  ini,
  java,
  javascript,
  json,
  kotlin,
  markdown,
  php,
  plaintext,
  python,
  r,
  ruby,
  rust,
  scala,
  scss,
  sql,
  swift,
  typescript,
  xml,
  yaml,
});

// The plaintext grammar and its aliases produce no tokens; resolving them to null skips the work.
const PLAIN_LABELS = ["plain", "output", "stdout"];
const PLAIN_NAMES = new Set(["plaintext", "text", "txt", ...PLAIN_LABELS]);

// Labels our own producers and models commonly emit that highlight.js does not alias itself.
// `shell` here means a script (the code_block `shell` kind), not an interactive console transcript.
codeLowlight.registerAlias({
  bash: ["shell", "console", "shellscript"],
  json: ["jsonl", "ndjson", "json5", "jsonc"],
  plaintext: PLAIN_LABELS,
  sql: [
    "bigquery",
    "clickhouse",
    "doris",
    "duckdb",
    "hive",
    "hiveql",
    "mysql",
    "pgsql",
    "plsql",
    "postgres",
    "postgresql",
    "presto",
    "snowflake",
    "spark-sql",
    "sparksql",
    "sqlite",
    "starrocks",
    "trino",
    "tsql",
  ],
});

/** Larger inputs render plain: tokenizing them would block the main thread for little reading benefit. */
export const MAX_HIGHLIGHT_CHARS = 200_000;

/** Normalize a language label (`SQL`, `py`, `postgresql`, …) to a registered name; null when it stays plain. */
export function resolveCodeLanguage(label: string | null | undefined): string | null {
  const name = label?.trim().toLowerCase();
  if (!name || PLAIN_NAMES.has(name) || !codeLowlight.registered(name)) return null;
  return name;
}

const MIME_LANGUAGE: Record<string, string> = {
  "application/javascript": "javascript",
  "application/json": "json",
  "application/jsonl": "json",
  "application/ld+json": "json",
  "application/sql": "sql",
  "application/typescript": "typescript",
  "application/x-javascript": "javascript",
  "application/x-ndjson": "json",
  "application/x-python": "python",
  "application/x-sh": "bash",
  "application/x-yaml": "yaml",
  "application/xml": "xml",
  "application/yaml": "yaml",
  "text/css": "css",
  "text/javascript": "javascript",
  "text/markdown": "markdown",
  "text/x-markdown": "markdown",
  "text/x-python": "python",
  "text/x-shellscript": "bash",
  "text/x-sql": "sql",
  "text/xml": "xml",
  "text/yaml": "yaml",
};

/** Pick the grammar for a stored file: its extension first, then its MIME type. */
export function codeLanguageForFile(
  filename: string | null | undefined,
  mime: string | null | undefined,
): string | null {
  const base = (filename ?? "").trim().toLowerCase().split(/[\\/]/).pop() ?? "";
  if (base === "dockerfile" || base.startsWith("dockerfile.")) return "dockerfile";
  const dot = base.lastIndexOf(".");
  const fromSuffix = dot > 0 ? resolveCodeLanguage(base.slice(dot + 1)) : null;
  if (fromSuffix) return fromSuffix;
  const normalizedMime = (mime ?? "").split(";", 1)[0]?.trim().toLowerCase() ?? "";
  if (MIME_LANGUAGE[normalizedMime]) return MIME_LANGUAGE[normalizedMime];
  return normalizedMime.endsWith("+json") ? "json" : null;
}

/** Highlight to a hast root; null when the language stays plain or the input is too large. */
export function highlightCodeTree(code: string, label: string | null | undefined): Root | null {
  const language = resolveCodeLanguage(label);
  if (!language || !code || code.length > MAX_HIGHLIGHT_CHARS) return null;
  try {
    return codeLowlight.highlight(language, code);
  } catch {
    // A grammar failure must never hide the code itself.
    return null;
  }
}

function classNameOf(node: Element): string | undefined {
  const value = node.properties?.className;
  if (Array.isArray(value)) return value.map(String).join(" ");
  return typeof value === "string" ? value : undefined;
}

function toReactNodes(nodes: (RootContent | ElementContent)[]): ReactNode[] {
  return nodes.map((node, index) => {
    if (node.type === "text") return node.value;
    if (node.type !== "element") return null;
    return createElement(
      "span",
      { key: index, className: classNameOf(node) },
      ...toReactNodes(node.children),
    );
  });
}

/** Highlighted React children for a `<code>` element; null when the caller should render plain text. */
export function highlightCodeToReact(code: string, label: string | null | undefined): ReactNode[] | null {
  const tree = highlightCodeTree(code, label);
  return tree ? toReactNodes(tree.children) : null;
}

function textOf(node: Element): string {
  let out = "";
  for (const child of node.children) {
    if (child.type === "text") out += child.value;
    else if (child.type === "element") out += textOf(child);
  }
  return out;
}

/**
 * Rehype plugin for Markdown fences: highlight `pre > code.language-*` in place.
 * Inline code, unlabeled fences, and `language-math` (KaTeX) are left untouched.
 */
export function rehypeCodeHighlight() {
  return (tree: Root) => {
    visit(tree, "element", (node: Element, _index, parent) => {
      if (node.tagName !== "code" || !parent || parent.type !== "element" || parent.tagName !== "pre") return;
      const classes = (classNameOf(node) ?? "").split(/\s+/).filter(Boolean);
      const label = classes.find((name) => name.startsWith("language-"))?.slice("language-".length);
      const highlighted = highlightCodeTree(textOf(node), label);
      if (!highlighted) return;
      node.children = highlighted.children as ElementContent[];
      node.properties = { ...node.properties, className: [...classes, "hljs"] };
    });
  };
}
