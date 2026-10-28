/**
 * Markdown rendering — react-markdown + GFM.
 * Three exports:
 *   - <Markdown markdown={...} />          Block rendering (paragraphs/lists/headings/tables/code blocks)
 *   - <MarkdownInline markdown={...} />    Inline rendering (bold/italic/code/link, no line breaks)
 *   - <MarkdownWithCharts markdown={...} /> Block + replace `<!-- chart:sec_xxx -->` markers
 *                                          with InlineChartThumb components (V6 Phase A B4)
 *
 * Sprint µ fail-safe: replace every [num:V:U(:src:S)?] marker with formatted
 * plain text before react-markdown. Design error #2 (frontend lacked fail-safe, raw markers
 * leaked to users). See docs/sprints/mu-fail-safe-citations/discovery.md.
 *
 * 0.39.4 bug F2 fix: `remarkGfm` defaults to `singleTilde: true` — single `~` is also strikethrough
 * delimiter. Chinese numeric ranges like `9.8~10.0`, `171~173万人次` became strikethrough →
 * users saw "营业额低谷（9.8" crossed out. Explicitly disable singleTilde, accepting only
 * GFM standard `~~text~~`.
 */

import React from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import remarkParse from "remark-parse";
import remarkRehype from "remark-rehype";
import { unified } from "unified";
import { visit } from "unist-util-visit";

import { stripLegacy, stripProvenanceTags } from "./citation-marks";

/**
 * Raw HTML in model Markdown (`<br>` in a table cell, a `<h1>` heading line) is never rendered as HTML.
 *
 * react-markdown keeps raw HTML as `raw` hast nodes and, without rehype-raw, prints them as literal text, so
 * users saw "a<br>b" in table cells and "<h1>Title</h1>" in chat. Rendering arbitrary HTML would be XSS, so
 * this plugin rebuilds raw tags from a small allowlist as **attribute-free** elements and strips every
 * other known HTML tag to its text (prose like `List<String>` stays literal): `<br>` is a line break, headings/paragraphs/emphasis keep their structure, and
 * script-like containers are dropped with their content. Headings and paragraphs inside a phrasing parent
 * (table cell, paragraph, heading) become bold/plain text so the DOM never nests blocks in inline content.
 * Rendering is a pure function of the text, so live, persisted and replayed answers render the same.
 */
const _RAW_HTML_TOKEN_RE = /<!--[\s\S]*?(?:-->|$)|<(\/?)([A-Za-z][A-Za-z0-9]*)\b(?:[^>"']|"[^"]*"|'[^']*')*>/g;
const _RAW_INLINE_TAGS = new Set(["b", "strong", "i", "em", "u", "s", "del", "sub", "sup", "code", "mark", "small", "kbd"]);
const _RAW_BLOCK_TAGS = new Set(["h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote"]);
const _RAW_DROP_CONTENT_TAGS = new Set(["script", "style", "iframe", "object", "embed", "template", "textarea", "noscript", "svg", "math", "select"]);
const _KNOWN_HTML_TAGS = new Set([
  "a", "abbr", "address", "article", "aside", "audio", "base", "bdi", "bdo", "body", "button", "canvas", "caption", "center",
  "cite", "col", "colgroup", "data", "dd", "details", "dfn", "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure",
  "font", "footer", "form", "frame", "frameset", "head", "header", "hr", "html", "img", "input", "ins", "label", "legend",
  "li", "link", "main", "map", "meta", "meter", "nav", "ol", "optgroup", "option", "output", "picture", "pre", "progress",
  "q", "rp", "rt", "ruby", "samp", "section", "source", "span", "summary", "table", "tbody", "td", "tfoot", "th", "thead",
  "time", "title", "tr", "track", "tt", "ul", "var", "video", "wbr",
]);
const _PHRASING_PARENTS = new Set([
  "p", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "a", "span", "strong", "em", "b", "i", "u", "s", "del", "sub", "sup", "code", "mark", "small", "kbd",
]);
const _HTML_ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

function decodeRawText(value: string): string {
  return value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (entity, body: string) => {
    if (body[0] === "#") {
      const code = body[1] === "x" || body[1] === "X" ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      return Number.isFinite(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : entity;
    }
    return _HTML_ENTITIES[body.toLowerCase()] ?? entity;
  });
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function rebuildRawHtmlChildren(parent: any): void {
  const children = parent.children;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  if (!Array.isArray(children) || !children.some((child: any) => child.type === "raw")) return;
  const phrasing = parent.type === "element" && _PHRASING_PARENTS.has(parent.tagName);
  // Only the document root wraps loose text in a paragraph; list items and quotes keep tight inline content.
  const wrapsLooseText = parent.type === "root";
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const out: any[] = [];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const stack: { tag: string; node: any }[] = [];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let looseParagraph: any = null;
  let dropping: string | null = null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const append = (node: any, block = false) => {
    // A block never nests inside an inline element left open by raw HTML (`<strong>` then a table).
    while (block && stack.length && !_RAW_BLOCK_TAGS.has(stack[stack.length - 1].tag)) stack.pop();
    if (stack.length) {
      stack[stack.length - 1].node.children.push(node);
    } else if (!wrapsLooseText || block) {
      looseParagraph = null;
      out.push(node);
    } else {
      // Text outside any block at the document root gets its own paragraph.
      if (node.type === "text" && !node.value.trim() && !looseParagraph) return;
      if (!looseParagraph) {
        looseParagraph = { type: "element", tagName: "p", properties: {}, children: [] };
        out.push(looseParagraph);
      }
      looseParagraph.children.push(node);
    }
  };
  const pushText = (value: string) => {
    if (!dropping && value) append({ type: "text", value: decodeRawText(value) });
  };
  for (const child of children) {
    if (child.type !== "raw") {
      if (!dropping) {
        const block = child.type === "element" && !_PHRASING_PARENTS.has(child.tagName) && child.tagName !== "br";
        append(child, block);
      }
      continue;
    }
    const value: string = String(child.value ?? "");
    _RAW_HTML_TOKEN_RE.lastIndex = 0;
    let last = 0;
    let match: RegExpExecArray | null;
    while ((match = _RAW_HTML_TOKEN_RE.exec(value)) !== null) {
      pushText(value.slice(last, match.index));
      last = match.index + match[0].length;
      const name = match[2]?.toLowerCase();
      if (!name) continue; // comment
      const closing = match[1] === "/";
      if (dropping) {
        if (closing && name === dropping) dropping = null;
        continue;
      }
      if (_RAW_DROP_CONTENT_TAGS.has(name)) {
        if (!closing && !match[0].endsWith("/>")) dropping = name;
        continue;
      }
      if (name === "br") {
        if (!closing) append({ type: "element", tagName: "br", properties: {}, children: [] });
        continue;
      }
      const blockTag = _RAW_BLOCK_TAGS.has(name);
      if (!blockTag && !_RAW_INLINE_TAGS.has(name)) {
        // An HTML tag we don't rebuild keeps only its text; anything else (`List<String>`, `<T>`) was
        // prose that only looked like a tag, so it stays literal as before.
        if (!_KNOWN_HTML_TAGS.has(name)) pushText(match[0]);
        continue;
      }
      const tagName = blockTag && phrasing ? (name.startsWith("h") ? "strong" : null) : name;
      if (closing) {
        const at = stack.map((entry) => entry.tag).lastIndexOf(name);
        if (at >= 0) stack.length = at;
        continue;
      }
      if (!tagName) continue;
      const element = { type: "element", tagName, properties: {}, children: [] };
      append(element, blockTag && !phrasing);
      stack.push({ tag: name, node: element });
    }
    pushText(value.slice(last));
  }
  parent.children = out;
}

function rehypeSafeRawHtml() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (tree: any) => {
    rebuildRawHtmlChildren(tree);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    visit(tree, "element", (node: any) => {
      rebuildRawHtmlChildren(node);
    });
  };
}

// `[N]` citations in cards (insight narrative and other Markdown paths): narrative PM editor uses
// citation-plugin for clickable references, but Markdown bypasses it → raw "[1]" inert citation (BUG-4).
// Rehype plugin: HAST text `[N]` → <sup data-citation-kind="numbered" data-source-idx>,
// reusing report-view CitationHoverManager (root=article covers cards) hover/click delegation → open sources.
const _CARD_CITE_RE = /\[(\d{1,3})\]/g;
/**
 * Stop bare-URL autolinks at first Chinese punctuation.
 *
 * GFM autolink literals strip only trailing ASCII punctuation; Chinese answer "（来源: https://a.com/x.html）"
 * includes fullwidth "）" in link, giving href `…x.html%EF%BC%89` — all 3 source links in 116 1.3.49 web search returned 404.
 * Stop only **punctuation**, not Han characters: URLs like `https://zh.wikipedia.org/wiki/中文` remain valid. Affect only
 * autolinks whose display text is URL itself; author-written `[text](url)` remains unchanged.
 */
const _CJK_URL_STOP = /[\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65\u2014\u2018-\u201f\u2026]/;

function rehypeTrimCjkAutolinks() {
  return (tree: any) => {
    visit(tree, "element", (node: any, index: number | undefined, parent: any) => {
      if (node.tagName !== "a" || !parent || index === undefined) return;
      const kids = node.children;
      if (!Array.isArray(kids) || kids.length !== 1 || kids[0].type !== "text") return;
      const text: string = kids[0].value;
      const cut = text.search(_CJK_URL_STOP);
      if (cut <= 0) return;
      const href = String(node.properties?.href ?? "");
      let decoded = href;
      try {
        decoded = decodeURI(href);
      } catch {
        return;
      }
      if (!decoded.endsWith(text)) return;
      const tail = text.slice(cut);
      const encodedTail = encodeURI(tail);
      node.properties.href = href.endsWith(encodedTail)
        ? href.slice(0, href.length - encodedTail.length)
        : decoded.slice(0, decoded.length - tail.length);
      kids[0].value = text.slice(0, cut);
      parent.children.splice(index + 1, 0, { type: "text", value: tail });
      return index + 2;
    });
  };
}

function rehypeCardCitations() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (tree: any) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    visit(tree, "text", (node: any, index: number | undefined, parent: any) => {
      if (index == null || !parent || typeof node.value !== "string") return;
      // citation-audit M3 (following impl review security P1-1): within code blocks/inline code,
      // `a[1]` is array indexing, not citation. Without filtering, observed ```python\nx = a[1]``` rendered
      // `x = a<sup class="rc-cardcite">1</sup>` — subscript became clickable chip pointing to "来源1".
      // BE `strip_untrusted_numbered` deliberately preserves fenced `a[1]` (A-P2-3);
      // omitting FE filter decorates away BE-protected content, making both ends inconsistent.
      if (parent.tagName === "code" || parent.tagName === "pre") return;
      if (node.value.indexOf("[") === -1) return;
      const value: string = node.value;
      _CARD_CITE_RE.lastIndex = 0;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const newNodes: any[] = [];
      let last = 0;
      let m: RegExpExecArray | null;
      while ((m = _CARD_CITE_RE.exec(value)) !== null) {
        const n = parseInt(m[1], 10);
        if (m.index > last) newNodes.push({ type: "text", value: value.slice(last, m.index) });
        newNodes.push({
          type: "element",
          tagName: "sup",
          properties: {
            className: ["rc-cardcite"],
            dataCitationKind: "numbered",
            dataSourceIdx: String(n - 1),
            role: "button",
            tabIndex: 0,
          },
          children: [{ type: "text", value: String(n) }],
        });
        last = m.index + m[0].length;
      }
      if (newNodes.length === 0) return;
      if (last < value.length) newNodes.push({ type: "text", value: value.slice(last) });
      parent.children.splice(index, 1, ...newNodes);
      return index + newNodes.length;
    });
  };
}

/**
 * chat-ux-2026-08 batch D — turn `[data:art_*]` / `[cite:N]` into **inline** custom elements.
 *
 * Why needed: LeadNarrative previously **split markdown** at every token, rendering each text part
 * through separate `<Markdown>` (block `.v2-md`), placing citation between two blocks — `[1]` intended
 * at sentence end occupied its own line, expanding 4 list items to 8 lines (116 observation).
 * Chip itself was not wrong (already inline-flex); block containers on either side were.
 *
 * Reuse existing `rehypeCardCitations` pattern (`[N]` → `<sup>`) in this file:
 * whole text enters react-markdown, preserving lists / tables / paragraphs, with chip in original inline position.
 *
 * ⚠ Leave fences untouched: brackets in `code` / `pre` are code, not citations (same established rehypeCardCitations
 * rule: BE strip_untrusted_numbered deliberately preserves fenced `a[1]`).
 */
const _ART_TOKEN_RE = /\[data:(art_[A-Za-z0-9_-]+)\]|\[cite:(\d{1,3})\]/g;
function rehypeArtifactChips() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (tree: any) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    visit(tree, "text", (node: any, index: number | undefined, parent: any) => {
      if (index == null || !parent || typeof node.value !== "string") return;
      if (parent.tagName === "code" || parent.tagName === "pre") return;
      const value: string = node.value;
      if (value.indexOf("[") === -1) return;
      _ART_TOKEN_RE.lastIndex = 0;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const out: any[] = [];
      let last = 0;
      let prevContext: string | null = null;
      let m: RegExpExecArray | null;
      while ((m = _ART_TOKEN_RE.exec(value)) !== null) {
        const between = value.slice(last, m.index);
        if (m.index > last) out.push({ type: "text", value: between });
        // Text preceding citation in same text node (up to 40 characters): knowledge-source card locates original highlight from it.
        // Adjacent citations (`…text[cite:1][cite:2]`, separated by whitespace at most) cite same sentence; reuse previous context,
        // otherwise second gets only empty string.
        const citeContext: string = prevContext !== null && !between.trim()
          ? prevContext
          : value.slice(Math.max(last, m.index - 40), m.index);
        prevContext = citeContext;
        // Use <sup>, not custom tag: react-markdown **discards** unrecognized elements
        // (observed `x-art-chip` vanished entirely, along with token in text = silent citation loss).
        // Existing rehypeCardCitations already uses <sup>; reuse same verified path.
        out.push({
          type: "element",
          tagName: "sup",
          properties: {
            dataArtifactChip: m[1] ? m[1] : `cite:${m[2]}`,
            dataCiteContext: citeContext,
            className: ["bh-cite-slot"],
          },
          children: [],
        });
        last = m.index + m[0].length;
      }
      if (out.length === 0) return;
      if (last < value.length) out.push({ type: "text", value: value.slice(last) });
      parent.children.splice(index, 1, ...out);
      return index + out.length;
    });
  };
}

/**
 * chat-inline-figure-2026-08 batch 1 — `[figure:<sec_id>]` anchor line → **block** chart slot.
 *
 * Distinct from `rehypeArtifactChips` (inline `<sup>` citations): chips are inline; charts are blocks.
 * Block tags cannot live in `<p>` (`<div>` in `<p>` = invalid nesting / hydration warning), so here
 * **split `<p>` at anchor lines in hast**, producing three siblings `[p(before), figure, p(after)]`.
 *
 * ⚠ Why split by lines, instead of replacing only `<p>` containing solely an anchor:
 * Markdown **single `\n` is soft break**, not new paragraph. Measurements of 1555 production anchors on 116:
 *   Blank lines around (own <p>)   616 (39.6%)
 *   Soft break (same paragraph as caption) 918 (59.0%) ← dominant shape
 *   Mid-sentence                  21 ( 1.4%)
 * Model instruction to use a separate standalone line produces soft-break shape:
 * `\n[figure:sec_x]\n**图1**：<one-sentence conclusion>\n` (`chart-table-ux-2026-08-11/prd.md:164`
 * independently measured same shape 1281/1318 = 97%). Anchor-only paragraphs ⇒ **59% never trigger,
 * and anchors render unchanged as literals to users**.
 *
 * ⚠ Unlike retired `splitMarkdownByChartMarkers` split (see below): that split markdown
 * into N parts, each rendered with separate `<Markdown>` (each block `.v2-md`), putting citations between block
 * containers — sentence-end `[1]` occupied its own line, expanding 4 list items to 8 lines (116 observation).
 * Here whole text still **enters react-markdown only once**; only HAST nodes rearrange, preserving lists / tables
 * (`docs/sprints/chat-inline-figure-2026-08/impl-plan.md` §6 measured case by case).
 */
/**
 * Anchor-line criterion: **anchor at end of line** (body may precede it; only whitespace may follow).
 *
 * Broader than report `_ANCHOR_ONLY_LINE` (strict standalone line): a model may emit
 * `The sample trend declines.[figure:sec_example]` with the anchor at the sentence end.
 * This still expresses the intended placement beside the paragraph,
 * merely shape did not copy instruction. Per [[feedback_llm_internal_syntax_unreliable]]:
 * model internal syntax is highly unstable; **backend must provide fallback**, not expect exact formatting.
 *
 * Hence broaden to line-end anchor: preceding body joins previous paragraph, chart renders afterward.
 * Genuine mid-sentence anchors (text follows) still only strip, never render — 1.4% noise, not intent.
 */
const _FIGURE_ANCHOR_LINE = /^(.*?)[ \t]*\[figure:([A-Za-z0-9_]+)\][ \t]*$/;
/**
 * Mid-sentence (nonstandalone) anchors: strip without splitting — 1.4% of production; retaining leaks literals.
 *
 * ⚠ Deliberately **broader than `_FIGURE_ANCHOR_LINE`** (id uses `[^\]\n]*`, not `[A-Za-z0-9_]+`):
 * regexes have distinct duties — strict one decides chart rendering (malformed id must not create slot),
 * broad one ensures `[figure:` never reaches users. Observed `[figure:]` / `[figure:  ]` /
 * `[figure:sec-a!]` all leaked with strict regex.
 */
const _FIGURE_INLINE_RE = /\[figure:[^\]\n]*\]/g;

/**
 * **Incomplete anchors** during streaming: `[fig` / `[figure:` / `[figure:sec_a` (no closing bracket yet).
 *
 * Streaming rerenders per token, exposing anchors typed character by character to users.
 * Same treatment has precedent — `LeadNarrative._DANGLING_MARKER_RE` handles
 * incomplete `<headline>` tags. Match only genuine `[figure:` prefixes, anchored at line end,
 * without swallowing ordinary `[` in body.
 */
const _FIGURE_DANGLING_RE = /\[fi(?:g(?:u(?:r(?:e(?::[^\]\n]*)?)?)?)?)?$/;

/**
 * Prescan body, returning figure ids referenced by anchor lines (**deduplicated in first-appearance order**).
 *
 * Caller (ChatBubbleNew) decides which artifacts are inline and which remain to render at end.
 * ⚠ Must use **same** `_FIGURE_ANCHOR_LINE` criterion as `rehypeFigureSlots`, otherwise
 * inline rendering and trailing removal conflict (chart renders twice or disappears entirely).
 */
// Parser **identical to rendering**: react-markdown internally uses same trio (same dependency versions),
// copying Markdown component remarkGfm configuration (singleTilde:false).
const _scanProcessor = unified()
  .use(remarkParse)
  .use(remarkGfm, { singleTilde: false })
  .use(remarkRehype);

export function scanFigureAnchors(markdown: string): string[] {
  if (!markdown || markdown.indexOf("[figure:") === -1) return [];
  // ── review F1: scanner no longer interprets markdown independently ─────────────────────────────
  // Old scanner used **raw lines**, plugin **hast**; disagreement about which anchors render
  // made charts disappear entirely three times (fences/indentation → list items → list continuations/lazy continuations/
  // setext headings). Each shape fix was another patch in same class. Scanner now uses **same**
  // remark→rehype parsing and **same code** as `rehypeFigureSlots`
  // (`_collectFigureSlots`) to determine slots, making disagreement structurally impossible.
  try {
    const tree = _scanProcessor.runSync(_scanProcessor.parse(markdown));
    return [..._collectFigureSlots(tree, /*apply*/ false)];
  } catch {
    return []; // Parse failure ⇒ no references: all artifacts render at end (conserved); rendering fallback strips literals
  }
}

/**
 * Streaming-only: strip figure anchors completely (delete entire anchor lines, remove mid-sentence anchors in place).
 *
 * Why needed: `LeadNarrative` streaming path is shim (bold + paragraph splitting only), **without rehype**,
 * so anchor literals flash to users until stream ends. Artifacts already wait for
 * `!isStreaming`; hiding anchors during streaming preserves today's behavior exactly.
 *
 * ⚠ Share same regex pair with `rehypeFigureSlots` — criterion must have single source of truth.
 */
export function stripFigureAnchors(text: string): string {
  if (!text || text.indexOf("[fi") === -1) return text;
  const kept: string[] = [];
  for (const line of text.split("\n")) {
    // Line-end anchor: remove anchor only, **preserve preceding body** (dropping whole line during streaming looks like lost text).
    const m = _FIGURE_ANCHOR_LINE.exec(line);
    if (m) {
      if (m[1].trim()) kept.push(m[1].replace(_FIGURE_INLINE_RE, ""));
      continue;
    }
    kept.push(line.replace(_FIGURE_INLINE_RE, ""));
  }
  // Incomplete anchor can occur only at **end of whole text** (stream tail); remove once at end.
  return kept.join("\n").replace(_FIGURE_DANGLING_RE, "");
}

/**
 * Split one `<p>` at anchor lines into [p, figure, p, ...]. null = nothing to change in paragraph.
 *
 * Caller shares `emitted` across **whole tree** — each id renders only at first occurrence.
 * ⚠ Must be tree-level: anchors often span **different paragraphs** (blank-line-separated);
 * new Set per paragraph renders repeated ids multiple times (genuine first-version bug caught by criterion 11).
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function _splitParagraphAtAnchors(p: any, emitted: Set<string>): any[] | null {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const out: any[] = [];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let buf: any[] = [];
  let found = false;
  // Mid-sentence anchors stripped but no anchor lines ⇒ no paragraph split, but **write changed text back**,
  // otherwise literal remains on screen (another genuine first-version bug caught by criterion 9).
  let strippedInline = false;
  const flush = () => {
    // Omit whitespace-only paragraphs: if anchors have only line breaks around them, no empty <p> consuming line height.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const hasContent = buf.some((c: any) => c.type !== "text" || c.value.trim() !== "");
    if (hasContent) out.push({ ...p, children: buf });
    buf = [];
  };
  for (const child of p.children ?? []) {
    if (child.type !== "text" || typeof child.value !== "string" || child.value.indexOf("[figure:") === -1) {
      buf.push(child);
      continue;
    }
    let pending: string[] = [];
    for (const line of (child.value as string).split("\n")) {
      const m = _FIGURE_ANCHOR_LINE.exec(line);
      if (!m) {
        // Mid-sentence anchor (text follows): strip, then accumulate, without splitting paragraph.
        const cleaned = line.replace(_FIGURE_INLINE_RE, "");
        if (cleaned !== line) strippedInline = true;
        pending.push(cleaned);
        continue;
      }
      found = true;
      // Pre-anchor body (nonempty for line-end anchors) stays in preceding paragraph; never lose it in split.
      const lead = m[1];
      if (lead.trim()) pending.push(lead);
      if (pending.length) {
        buf.push({ type: "text", value: pending.join("\n") });
        pending = [];
      }
      flush();
      // Repeated id: still consume anchor (no literal remains), but **do not render another chart**.
      if (emitted.has(m[2])) continue;
      emitted.add(m[2]);
      out.push({
        type: "element",
        tagName: "figure",
        properties: { dataFigureSlot: m[2] },
        children: [],
      });
    }
    if (pending.length) buf.push({ type: "text", value: pending.join("\n") });
  }
  flush();
  return found || strippedInline ? out : null;
}

/**
 * **Single source of truth** for slots: `rehypeFigureSlots` (apply=true, mutate tree) and
 * `scanFigureAnchors` (apply=false, collect only) share this code, preventing criterion drift
 * (review F1: disagreement-class fix replaces per-shape line-scanning patches).
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function _collectFigureSlots(tree: any, apply: boolean): Set<string> {
  // Tree-level deduplication: repeated id renders only first occurrence (see `_splitParagraphAtAnchors` comments).
  const emitted = new Set<string>();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  visit(tree, "element", (node: any, index: number | undefined, parent: any) => {
    if (index == null || !parent || node.tagName !== "p") return;
    // List-item `<p>` (loose lists only) never produces block slots: tight lists have no `<p>`,
    // both list forms must give same result, rather than render charts only with one spelling of same text.
    if (parent.tagName === "li") return;
    const parts = _splitParagraphAtAnchors(node, emitted);
    if (!parts) return;
    if (!apply) return;
    parent.children.splice(index, 1, ...parts);
    return index + parts.length;
  });
  return emitted;
}

function rehypeFigureSlots() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (tree: any) => {
    _collectFigureSlots(tree, /*apply*/ true);
    // ── Fallback: strip **all remaining** `[figure:...]` from text ────────────────
    //
    // Above pass handles only `<p>`; anchors also appear elsewhere: text in tight-list `<li>` **has no `<p>` wrapper**
    // (observed `- 甲 [figure:sec_a]` rendered literal unchanged), likewise `<blockquote>` / headings / table cells.
    // Criterion 4 against visible `[figure:` literals applies to whole tree, not one tag.
    // These locations **produce no block slots** (block charts in li/tables destroy layout), only strip literals.
    // ⚠ Skip code/pre: brackets there are code, not citations (same established `rehypeArtifactChips` rule).
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    visit(tree, "text", (node: any, _i: number | undefined, parent: any) => {
      if (!parent || parent.tagName === "code" || parent.tagName === "pre") return;
      if (typeof node.value !== "string" || node.value.indexOf("[figure:") === -1) return;
      node.value = node.value.replace(_FIGURE_INLINE_RE, "");
    });
  };
}

// chat-redesign Phase 1.2 — InlineChartThumb + splitMarkdownByChartMarkers
// retired with the old chat surface. MarkdownWithCharts now falls through to
// plain Markdown (chart embeds inside chat bubbles now flow through the new
// step-list/ChatInlineChart wrapper, called from ChatBubbleNew with typed
// artifact data, not parsed from markdown text). Phase 1.5 may delete the
// MarkdownWithCharts export entirely once callers migrate.
const splitMarkdownByChartMarkers = (md: string): Array<{ kind: "text"; value: string }> => [
  { kind: "text", value: md },
];

// Sprint µ fail-safe: strip raw [num:V:U(:src:S)?] markers to plain readable text.
// Markdown component (react-markdown) doesn't run prosemirror citation_plugin,
// so without this pre-processor any unbound `[num:1.618:ratio]` would render
// literally in chat bubbles / insight_card narrative — observed in prod
// t_059e6921 disaster. Bound markers (with :src:) also strip here since the
// hover popover only works in prosemirror canvas, not in plain Markdown.
/**
 * Markdown prerender fallback (citation-audit M3-1: original private regexes +
 * `_formatNumDisplay` conventions **removed**, now call single `citation-marks.stripLegacy` module).
 *
 * react-markdown does not run prosemirror citation_plugin; without fallback, replayed historical events'
 * `[num:1.618:ratio]` would render literally in chat bubbles/insight cards (production
 * t_059e6921 incident).
 *
 * Two actual benefits of single module (beyond deduplication):
 *   1. **Matches backend bake** — original scaled CNY to `13.9亿元`, while M2 migration actually
 *      persisted `139000000元`; same data showed different numbers in chat and canvas (debt 5 symptom).
 *   2. **Adds `[ref:]`/`[chart:]`/`[fact:]/[bi:]/[memory:]` fallback** — original recognized only
 *      `[num:`, closing discovery A3's missing FE fallback for chat `[ref:]`.
 */
export function stripInlineNumMarkers(md: string): string {
  if (!md) return md;
  // [cite:N] is chat-path marker (Lead final_answer), occasionally leaking into canvas section text.
  // Canvas lacks [cite:N] resolver (uses [N] + sources), so leakage shows raw "[cite:2]".
  // → Strip (references chat turn-scoped registry, already dangling in canvas). Not a retired family,
  // so excluded from stripLegacy (§8: chat [cite:N] system out of scope).
  // Hide provenance tags too. **Cannot rely only on stripLegacy FACT_RE fallback** —
  // recognizes only fact|bi|memory (half of six legal kinds), and `[^\]]+` requires at least
  // one post-colon character, so `[llm-std:]` (empty ref, standard for generic concepts) evades both conditions.
  // 2026-08-25 116 observation: EBITDA turn actually displayed "（[llm-std:]）".
  return stripProvenanceTags(stripLegacy(md).replace(/\[cite:\d+\]/g, ""));
}

/**
 * Move GFM header glued to prose line end onto its own line (feedback-triage-2026-08-24).
 *
 * A header attached to a preceding paragraph makes an otherwise valid GFM table render as prose:
 *
 *     **Sample ranking** Example A leads. | Rank | Brand | Value | Share |
 *     |---:|---|---:|---:|
 *     | 1 | Example A | 12,345.67 | 25.00% |
 *
 * Delimiter and data rows had separate lines; **only header was glued to preceding prose line end**. GFM needs
 * standalone header, degrading entire block into paragraph — renderer was correct, model output malformed.
 *
 * Root fix belongs in Lead prompt (tables should use canvas_add_table, not prose). Per repository
 * treatment of LLM syntax (see LeadNarrative stripInternalMarkers), rendering also needs fallback.
 *
 * Deliberately narrow criterion: only next line being GFM delimiter provides signal:
 *   · Act only when **next line genuinely is** delimiter such as `|---|:--:|`;
 *   · Current line must have nonpipe content before pipe (plain `| a | b |` is already valid, leave alone);
 *   · Insert newline only at last prose → header boundary, without rearranging anything else.
 * Thus valid tables, pipes in code blocks, and occasional prose `|` remain unaffected.
 */
const _GFM_DELIMITER_ROW = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

export function repairGluedTableHeader(md: string): string {
  if (!md || md.indexOf("|") === -1) return md;
  const lines = md.split("\n");
  let changed = false;
  for (let i = 0; i + 1 < lines.length; i += 1) {
    const line = lines[i];
    if (!_GFM_DELIMITER_ROW.test(lines[i + 1])) continue;
    if (line.indexOf("|") === -1) continue;
    const firstPipe = line.indexOf("|");
    const prose = line.slice(0, firstPipe).trim();
    // Header already on own line (no prose before pipe) → valid, leave alone.
    if (!prose) continue;
    lines[i] = `${line.slice(0, firstPipe).trimEnd()}\n\n${line.slice(firstPipe).trimStart()}`;
    changed = true;
  }
  return changed ? lines.join("\n") : md;
}

interface Props {
  markdown: string;
  /** Chat opts in to visible single line breaks; report Markdown keeps normal soft breaks. */
  preserveLineBreaks?: boolean;
  className?: string;
  /**
   * Render `![](…)` images (default true, preserving chat/reports). Untrusted text such as source excerpts passes false: use alt + link,
   * without automatic requests (otherwise tracking pixels, also requesting internal addresses in intranet deployments).
   */
  allowImages?: boolean;
  /** Link rel (default "noreferrer", preserving chat/reports). Source excerpts pass "noopener noreferrer". */
  linkRel?: string;
  /**
   * report P2 fix: turn `[N]` into a clickable `<sup>` citation. OPT-IN so it
   * only runs on canvas card text (narrative/deck/callout), NOT on the SHARED
   * chat surface (LeadNarrative/TurnContainer) where `[N]` must render literally
   * — applying it to chat was a regression (LeadNarrative `9.8~10.0 亿 [4]元`
   * lost its `[4]`). Default off.
   */
  enableCardCitations?: boolean;
  /**
   * chat-ux-2026-08 batch D — supplying enables **inline** citation chips:
   * `[data:art_*]` / `[cite:N]` become inline elements during rehype, rendered as components by this callback.
   * Also skip `[cite:N]` fallback stripping — caller resolves them; stripping loses citations.
   */
  renderArtifactChip?: (artifactId: string, context?: string) => React.ReactNode;
  /**
   * chat-inline-figure-2026-08 batch 1 — supplying enables **block** figure slots:
   * Standalone body `[figure:<id>]` becomes block node in rehype, rendered as component by callback.
   *
   * **Returning `null` ⇒ anchor renders nothing** (anchor line already consumed) — unknown / hallucinated / unavailable ids
   * thus vanish silently, never exposing `[figure:` literals. Semantics match report path's
   * `report_writer_loop._strip_invalid_figure_anchors`, but at rendering layer without precomputing valid set.
   *
   * Like `enableCardCitations`, **opt-in**: enabled only in chat-answer bubbles.
   */
  renderFigureSlot?: (figureId: string) => React.ReactNode | null;
}

export function Markdown({
  markdown,
  preserveLineBreaks = false,
  className,
  enableCardCitations,
  renderArtifactChip,
  renderFigureSlot,
  allowImages = true,
  linkRel = "noreferrer",
}: Props): React.ReactElement {
  const rehype = [
    rehypeSafeRawHtml,
    rehypeTrimCjkAutolinks,
    ...(enableCardCitations ? [rehypeCardCitations] : []),
    ...(renderArtifactChip ? [rehypeArtifactChips] : []),
    ...(renderFigureSlot ? [rehypeFigureSlots] : []),
    // Typeset last, avoiding citation/figure plugins above misreading text nodes inside formulas.
    rehypeKatex,
  ];
  // ★ Component **function identity must stay stable across renders**: react-markdown treats `components.sup` as element type;
  // creating function per render **unmounts/remounts** all citations / slots on parent rerender, closing expanded source cards
  // (116 1.3.53: polling rerender closed card 5–10 seconds after opening); inline charts also rebuilt.
  // Callbacks read latest values through refs; component changes only when enabled/disabled.
  const chipRef = React.useRef(renderArtifactChip);
  chipRef.current = renderArtifactChip;
  const figureRef = React.useRef(renderFigureSlot);
  figureRef.current = renderFigureSlot;
  const hasChip = Boolean(renderArtifactChip);
  const hasFigure = Boolean(renderFigureSlot);
  const components = React.useMemo(() => {
    const out: Record<string, unknown> = {
      a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) =>
        React.createElement("a", { ...props, target: "_blank", rel: linkRel }),
    };
    if (!allowImages) {
      // No <img>: show alt text and URL (filtered by react-markdown) as clickable link, letting user decide whether to open
      out["img"] = (props: React.ImgHTMLAttributes<HTMLImageElement>) => {
        const alt = typeof props.alt === "string" ? props.alt : "";
        const src = typeof props.src === "string" ? props.src : "";
        if (!src) return alt ? React.createElement("span", { "data-md-image": "" }, alt) : null;
        return React.createElement(
          "a",
          { href: src, target: "_blank", rel: linkRel, "data-md-image": "" },
          alt || src,
        );
      };
    }
    if (hasChip) {
      // Handle only <sup> with data-artifact-chip; other <sup> (including rehypeCardCitations
      // [N] superscripts) render unchanged; plugins do not interfere.
      out["sup"] = (
        props: React.HTMLAttributes<HTMLElement> & { "data-artifact-chip"?: string; "data-cite-context"?: string },
      ) => {
        const id = props["data-artifact-chip"];
        const render = chipRef.current;
        if (!id || !render) return React.createElement("sup", props);
        return React.createElement(React.Fragment, null, render(id, props["data-cite-context"]));
      };
    }
    if (hasFigure) {
      // Handle only `<figure data-figure-slot>` **produced by this plugin**; render other `<figure>` unchanged.
      // Same as `sup` above — `<figure>` is standard tag, also used by canvas chart-block
      // (`chart-block.tsx`: chart = bare figure); handling indiscriminately swallows others' figures.
      out["figure"] = (
        props: React.HTMLAttributes<HTMLElement> & { "data-figure-slot"?: string },
      ) => {
        const id = props["data-figure-slot"];
        const render = figureRef.current;
        if (!id || !render) return React.createElement("figure", props);
        // null ⇒ unknown id: anchor line already consumed, render nothing (never fall back to literal).
        return React.createElement(React.Fragment, null, render(id));
      };
    }
    return out;
  }, [hasChip, hasFigure, allowImages, linkRel]);
  return React.createElement(
    "div",
    { className: `v2-md ${className ?? ""}`.trim() },
    React.createElement(ReactMarkdown, {
      // Agent answers emit $$...$$; disable single $ so ordinary amounts do not become inline math.
      remarkPlugins: [
        [remarkGfm, { singleTilde: false }],
        [remarkMath, { singleDollarTextMath: false }],
        ...(preserveLineBreaks ? [remarkBreaks] : []),
      ],
      rehypePlugins: rehype,
      components: components as never,
      // With inline citations enabled, **do not** strip [cite:N]: fallback serves surfaces without resolvers;
      // caller renders chips here, so stripping loses citations.
      // ⚠ Strip provenance tags in **both branches**: independent of whether artifact chips render.
      // 2026-08-26 116 lesson: first version stripped only in stripInlineNumMarkers (branch below),
      // but Lead prose used branch above (`LeadNarrative` passed renderArtifactChip) —
      // **leaking path remained uncovered**. Same mistake twice: first wrong file, then wrong branch.
      children: repairGluedTableHeader(
        stripProvenanceTags(
          renderArtifactChip ? stripLegacy(markdown || "") : stripInlineNumMarkers(markdown || ""),
        ),
      ),
    }),
  );
}

/**
 * V6 Phase A B4 + Problem 1: LLM `<!-- chart:sec_xxx -->` in chat narrative
 * is inline HTML to react-markdown, normally hidden (skipHtml), but user screenshots show
 * marker leakage (backend escaping < → &lt; somewhere, or react-markdown HTML
 * parsing missed inline comment fragment).
 *
 * Change: split markdown by markers into { text | chart_marker } before rendering; text parts
 * use react-markdown, chart parts InlineChartThumb (canvas-store resolves sec_id:
 * found → thumb + "↗ 在画布查看" link; absent → faint placeholder).
 *
 * Prevents leakage and upgrades markers to clickable chart citations (same lineage as Claude design §04
 * intermediate_chart).
 */
export function MarkdownWithCharts({ markdown, className }: Props): React.ReactElement {
  const parts = splitMarkdownByChartMarkers(stripInlineNumMarkers(markdown ?? ""));
  return React.createElement(
    "div",
    { className: `v2-md ${className ?? ""}`.trim() },
    ...parts.map((p, i) => {
      // chart embeds retired (see file header note); all parts are text.
      return React.createElement(ReactMarkdown, {
        key: `t${i}`,
        remarkPlugins: [[remarkGfm, { singleTilde: false }]],
        rehypePlugins: [rehypeSafeRawHtml],
        components: {
          a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) =>
            React.createElement("a", { ...props, target: "_blank", rel: "noreferrer" }),
        },
        children: p.value,
      });
    }),
  );
}

/** Inline: render single-line markdown as span, still supporting bold/italic/code/link. */
export function MarkdownInline({ markdown, className, enableCardCitations }: Props): React.ReactElement {
  return React.createElement(
    "span",
    { className: `v2-md-inline ${className ?? ""}`.trim() },
    React.createElement(ReactMarkdown, {
      remarkPlugins: [[remarkGfm, { singleTilde: false }]],
      rehypePlugins: enableCardCitations ? [rehypeSafeRawHtml, rehypeCardCitations] : [rehypeSafeRawHtml],
      components: {
        p: (props: { children?: React.ReactNode }) =>
          React.createElement(React.Fragment, null, props.children),
        a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) =>
          React.createElement("a", { ...props, target: "_blank", rel: "noreferrer" }),
      },
      children: stripInlineNumMarkers(markdown || ""),
    }),
  );
}
