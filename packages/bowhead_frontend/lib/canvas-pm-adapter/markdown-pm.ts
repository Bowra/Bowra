import EditorStarterKit from "./starter-kit";
/**
 * Bidirectional Markdown ↔ PM node conversion.
 *
 * Uses the official prosemirror-markdown library (aligned with default PM schema, compatible with Tiptap StarterKit).
 *
 * gen3 [N] **remains plain text**, unprocessed at the Markdown stage; rendering is handled by Citation
 * Plugin (PM Decoration: [N] chip + citation ticks on adjacent numbers).
 *
 * ⚠ citation-audit M3: retired [num:V:U:src:S] / [ref:S] / [chart:S] syntax **must no longer** reach
 * here; upstream canvas-to-pm::textBasedToPMNode bakes it to plain text with stripLegacy. The old comment
 * claiming Citation Plugin handles num/ref became invalid after M3 (four gen2 parsing passes removed),
 * exactly the review P1 misconception: deleting decoration passes removes raw-marker CSS masking, leaving only stripLegacy fallback.
 */

import {
  defaultMarkdownParser,
  defaultMarkdownSerializer,
  MarkdownSerializer,
} from "prosemirror-markdown";
import { Node as ProseMirrorNode, Schema } from "@tiptap/pm/model";
import type { JSONContent } from "@tiptap/react";
// #305: typography marks (static imports avoid vitest failures resolving relative require paths)
import { FontSize as _FontSize } from "./extensions/font-size";
import { FontWeight as _FontWeight } from "./extensions/font-weight";

// #305: serializer schema extends default schema with textStyle mark
// (fontFamily/fontSize/fontWeight attrs). Without this, PM deserialization strips
// textStyle marks → serializer outputs only plain text, losing typography.
const _defSchema = defaultMarkdownParser.schema;
const schemaWithTextStyle = new Schema({
  // report-engine Phase-1: register `figure_ref` so pmNodesToMarkdown's
  // ProseMirrorNode.fromJSON doesn't throw "Unknown node type" on an inline
  // figure anchor (S-FE-4 serializer pair-fix — schema half). The serializer
  // half is bowheadSerializer.nodes.figure_ref below. group "block" + atom so
  // it lives between paragraphs in a narrative flow.
  // Four table nodes: read path (markdownToPMNodes) explicitly loads Tiptap
  // Table/TableRow/TableCell/TableHeader on GFM table detection, producing these nodes; if writes do not recognize them,
  // fromJSON throws `Unknown node type: tableHeader` → entire section degrades to plain text.
  // **Readable content must also be writable**, the same paired implementation as figure_ref (S-FE-4):
  // schema half here, serializer half in bowheadSerializer.nodes.table and its three companions.
  // Use Tiptap camelCase node names; PM_TO_TIPTAP does not map tables, so renameNodeTypes passes them unchanged.
  nodes: _defSchema.spec.nodes
    .addToEnd("figure_ref", {
      // report-engine Phase-1: register `figure_ref` so pmNodesToMarkdown's
      // ProseMirrorNode.fromJSON doesn't throw "Unknown node type" on an inline
      // figure anchor (S-FE-4 serializer pair-fix — schema half). The serializer
      // half is bowheadSerializer.nodes.figure_ref below. group "block" + atom so
      // it lives between paragraphs in a narrative flow.
      group: "block",
      atom: true,
      selectable: true,
      attrs: { targetSectionId: { default: null } },
      toDOM: () => ["div", { "data-figure-ref": "" }],
    })
    .addToEnd("table", {
      group: "block",
      content: "tableRow+",
      isolating: true,
      toDOM: () => ["table", ["tbody", 0]],
    })
    .addToEnd("tableRow", {
      content: "(tableCell | tableHeader)*",
      toDOM: () => ["tr", 0],
    })
    .addToEnd("tableHeader", {
      content: "block+",
      attrs: {
        colspan: { default: 1 },
        rowspan: { default: 1 },
        colwidth: { default: null },
      },
      isolating: true,
      toDOM: () => ["th", 0],
    })
    .addToEnd("tableCell", {
      content: "block+",
      attrs: {
        colspan: { default: 1 },
        rowspan: { default: 1 },
        colwidth: { default: null },
      },
      isolating: true,
      toDOM: () => ["td", 0],
    }),
  marks: _defSchema.spec.marks
    .addToEnd("textStyle", {
      attrs: {
        fontFamily: { default: null as string | null },
        fontSize: { default: null as string | null },
        fontWeight: { default: null as string | null },
      },
      inclusive: true,
      parseDOM: [{ tag: "span" }],
      toDOM(mark: { attrs: Record<string, string | null> }) {
        const styles: string[] = [];
        if (mark.attrs.fontFamily) styles.push(`font-family: ${mark.attrs.fontFamily}`);
        if (mark.attrs.fontSize) styles.push(`font-size: ${mark.attrs.fontSize}`);
        if (mark.attrs.fontWeight) styles.push(`font-weight: ${mark.attrs.fontWeight}`);
        return ["span", { style: styles.join("; ") }, 0];
      },
    })
    // StarterKit Strike is **enabled** in the editor (report-view.tsx disables only document);
    // Cmd+Shift+X and ~~文字~~ in table-containing sections produce strike marks, but default PM schema only has
    // link/em/strong/code ⇒ fromJSON throws `There is no mark type strike in this schema`
    // ⇒ entire section degrades ⇒ **permanently unsavable** under fail-closed (observed error came from this path).
    // Paired implementation like tables: schema here, serializer in bowheadSerializer.marks.strike.
    // Both use mark name "strike", requiring no PM_TO_TIPTAP entry.
    .addToEnd("strike", {
      parseDOM: [{ tag: "s" }, { tag: "del" }, { tag: "strike" }],
      toDOM: () => ["s", 0],
    }),
});

/**
 * Detect GFM table syntax in Markdown (`| col |` + `|---|`).
 * With tables, use generateJSON(HTML) (handled by Tiptap @tiptap/extension-table);
 * otherwise use prosemirror-markdown defaultParser (existing path, 30/30 tests).
 */
function hasMarkdownTable(md: string): boolean {
  return /^\s*\|.*\|\s*\n\s*\|[\s\-:|]+\|\s*$/m.test(md);
}

/**
 * #305: detect inline <span style="..."> with typography (font-size/family/weight).
 * When present, use markdown-it html:true so Tiptap textStyle/fontFamily/FontSize/FontWeight
 * parses back into marks. Otherwise prosemirror-markdown treats it as literal plain text, breaking visuals.
 */
function hasInlineTypography(md: string): boolean {
  return /<span\s+[^>]*style\s*=/i.test(md);
}

/**
 * Detect GFM strikethrough `~~文字~~`.
 *
 * The write path now emits `~~`(see bowheadSerializer.marks.strike), but the read-path
 * `defaultMarkdownParser` uses the **commonmark preset**, which does not recognize strikethrough; fixing only writes means
 * saved strikethrough returns as two literal tilde sequences after refresh, and another save lets esc turn them into `\~\~`，
 * worsening each cycle. Route strikethrough through the default markdown-it preset, like tables/typography.
 *
 * `[^~\n]+` does not cross lines ⇒ `~~~lang` tilde fences are not misclassified; serializer emits literal tildes as `\~`
 * ⇒ ordinary text cannot produce two consecutive `~`。
 */
function hasMarkdownStrike(md: string): boolean {
  return /~~[^~\n]+~~/.test(md);
}

/**
 * Custom serializer:
 *   - bullet_list uses "- " instead of default "* "
 *   - #305: textStyle mark (fontFamily/fontSize/fontWeight) emits the original <span style="...">
 *     PM textStyle is also named textStyle in Tiptap; default PM schema lacks this mark. Use
 *     pmNodesToMarkdown path (TIPTAP_TO_PM does not map textStyle), directly falling back to "textStyle".
 */
/**
 * Table cell content → one line of inline Markdown.
 *
 * GFM table cells cannot contain blocks; serialize all cell block content, then flatten to one line:
 * collapse newlines to spaces and escape pipes as `\|`(otherwise one cell splits into two).
 * Reuse bowheadSerializer to preserve bold/link/citation placeholder marks;
 * a hand-written simplification reintroduces a formatting-loss path.
 * (Function declarations are hoisted; bowheadSerializer is initialized at runtime, avoiding a TDZ issue.)
 */
function _serializeTableCell(cell: ProseMirrorNode): string {
  const doc = schemaWithTextStyle.topNodeType.create(null, cell.content);
  const md = bowheadSerializer.serialize(doc, { tightLists: true });
  return md
    .split("\n")
    .map(_unescapeBlockStart)
    .join("\n")
    .replace(/\s*\n+\s*/g, " ")
    .replace(/\|/g, "\\|")
    .trim();
}

/**
 * Remove escapes for **leading block-level markers**.
 *
 * `_serializeTableCell` serializes each cell as an independent document, so prosemirror-markdown
 * `esc(str, startOfLine=true)` adds backslashes to leading `- * + > : #` and `N. `;
 * observed: `-37.5%` became `\-37.5%`、`1. 第一` became `1\. 第一`、`> 注意` became `\> 注意`。
 * GFM **parses cell content inline**, so block constructs do not apply and these escapes are unnecessary,
 * yet persist unchanged and render as **literal backslashes** through Word / PDF / Feishu exports.
 *
 * Remove only the leading occurrence:`\\` `\*` `\~` these inline escapes must remain(`*` actually produces
 * emphasis in cells). User-entered backslashes were converted in the prior step to `\\`; their second character is not a block marker, avoiding collateral changes.
 */
function _unescapeBlockStart(line: string): string {
  return line
    .replace(/^(\s*)\\([:#\-*+>])/, "$1$2")
    .replace(/^(\s*\d+)\\\.(\s)/, "$1.$2");
}

const bowheadSerializer = new MarkdownSerializer(
  {
    ...defaultMarkdownSerializer.nodes,
    bullet_list(state: { renderList: (n: ProseMirrorNode, indent: string, fn: (i: number) => string) => void }, node: ProseMirrorNode) {
      state.renderList(node, "  ", () => "- ");
    },
    // report-engine Phase-1: figure_ref → `[figure:<id>]` on its own line
    // (closeBlock) so the forward parser re-detects it as a block anchor.
    // Without this, MarkdownSerializer.render() throws on the unknown node and
    // pmNodesToMarkdown silently drops the anchor (S-FE-4, the confirmed P0).
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    figure_ref(state: any, node: any) {
      state.write(`[figure:${node.attrs?.targetSectionId ?? ""}]`);
      state.closeBlock(node);
    },
    // Tables → GFM. Schema half: schemaWithTextStyle table/tableRow/tableHeader/tableCell above.
    // Render the entire table here without descending to rows/cells: GFM tables use **row-oriented** syntax,
    // and per-node writes cannot insert separator rows in the right positions.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    table(state: any, node: any) {
      const rows: string[][] = [];
      node.forEach((row: ProseMirrorNode) => {
        const cells: string[] = [];
        row.forEach((cell: ProseMirrorNode) => cells.push(_serializeTableCell(cell)));
        rows.push(cells);
      });
      if (rows.length === 0) {
        state.closeBlock(node);
        return;
      }
      // Use the widest row column count and pad shorter rows; uneven columns render misaligned in GFM.
      const width = Math.max(...rows.map((r) => r.length));
      const pad = (r: string[]): string[] => {
        const c = r.slice();
        while (c.length < width) c.push("");
        return c;
      };
      const lines = [
        `| ${pad(rows[0]).join(" | ")} |`,
        `|${" --- |".repeat(width)}`,
        ...rows.slice(1).map((r) => `| ${pad(r).join(" | ")} |`),
      ];
      // text(str, false) writes unchanged without escaping (same as code_block); this is assembled
      // Markdown syntax; escaping again would turn pipes into `\|`。
      state.text(lines.join("\n"), false);
      state.closeBlock(node);
    },
    // These three are not called normally (table renders in full); retain them to avoid
    // "Token type not supported" for orphan rows/cells, which would degrade the entire section.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    tableRow(state: any, node: any) {
      state.renderContent(node);
    },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    tableHeader(state: any, node: any) {
      state.renderContent(node);
    },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    tableCell(state: any, node: any) {
      state.renderContent(node);
    },
  },
  {
    ...defaultMarkdownSerializer.marks,
    // Strikethrough → GFM ~~. Schema half: schemaWithTextStyle.marks.strike.
    // Without this half, schema recognizes the mark but serializer throws "Mark type strike not supported",
    // still degradation; both halves of the paired implementation are required.
    strike: {
      open: "~~",
      close: "~~",
      mixable: true,
      expelEnclosingWhitespace: true,
    },
    // #305: textStyle mark serializer emits raw HTML <span style="...">
    textStyle: {
      open(_state: unknown, mark: { attrs: Record<string, string | null> }) {
        const styles: string[] = [];
        if (mark.attrs.fontFamily) styles.push(`font-family: ${mark.attrs.fontFamily}`);
        if (mark.attrs.fontSize) styles.push(`font-size: ${mark.attrs.fontSize}`);
        if (mark.attrs.fontWeight) styles.push(`font-weight: ${mark.attrs.fontWeight}`);
        if (!styles.length) return "";
        return `<span style="${styles.join("; ")}">`;
      },
      close(_state: unknown, mark: { attrs: Record<string, string | null> }) {
        const hasAttrs = mark.attrs.fontFamily || mark.attrs.fontSize || mark.attrs.fontWeight;
        return hasAttrs ? "</span>" : "";
      },
      mixable: false,
      expelEnclosingWhitespace: false,
    },
  },
);

/**
 * Restore `[num:..]` `[ref:..]` `[chart:..]` placeholder backslashes added by Markdown serializer escaping.
 * Default serializer treats `[` `]` as link syntax, escaping them to `\[` `\]`,
 * Bowhead placeholder syntax is critical data and must remain intact.
 *
 * Safety: Bowhead narrative LLM output does not contain legitimate `\[` (escaped brackets are rare),
 * Globally restore `\[` → `[` / `\]` → `]` does not cause collateral changes.
 */
function unescapeBowheadPlaceholders(md: string): string {
  return md.replace(/\\(\[|\])/g, "$1");
}

/**
 * Map standard PM (snake_case + old mark names) ↔ Tiptap StarterKit (camelCase + new mark names).
 *
 * Node name differences:
 *   - bullet_list / ordered_list / list_item / code_block / hard_break / horizontal_rule
 *
 * Mark name differences:
 *   - strong → bold (Tiptap StarterKit Bold extension, mark name "bold")
 *   - em     → italic (Tiptap StarterKit Italic extension, mark name "italic")
 *
 * Round-trip failure: prosemirror-markdown emits strong/em marks unrecognized by Tiptap schema.
 * Found during actual P3.1.7 spike integration; handle uniformly here.
 */
const PM_TO_TIPTAP: Record<string, string> = {
  // nodes
  bullet_list: "bulletList",
  ordered_list: "orderedList",
  list_item: "listItem",
  code_block: "codeBlock",
  hard_break: "hardBreak",
  horizontal_rule: "horizontalRule",
  // marks
  strong: "bold",
  em: "italic",
};
const TIPTAP_TO_PM: Record<string, string> = Object.fromEntries(
  Object.entries(PM_TO_TIPTAP).map(([k, v]) => [v, k]),
);

/**
 * Recursively rename node.type and node.marks[].type, handling nested content.
 * Map strong/em mark names through PM_TO_TIPTAP too.
 * link mark: @tiptap/extension-link registers schema support, so do not strip here.
 */
/**
 * code_block language uses **different attr keys**: default PM schema params (fence info string),
 * versus Tiptap CodeBlock language. renameNodeTypes previously changed only type, not attrs, so both
 * directions erased language: Tiptap dropped unknown params on reads, and PM dropped language on writes,
 * observed ```python becoming bare ``` after one save.
 * Determine direction by the type name **after renaming**, sharing one implementation both ways.
 */
function remapCodeBlockAttrs(newType: string | undefined, attrs: JSONContent["attrs"]) {
  if (!attrs) return attrs;
  if (newType === "codeBlock" && "params" in attrs) {
    const { params, ...rest } = attrs as Record<string, unknown>;
    return { ...rest, language: params || null };
  }
  if (newType === "code_block" && "language" in attrs) {
    const { language, ...rest } = attrs as Record<string, unknown>;
    return { ...rest, params: language ?? "" };
  }
  return attrs;
}

/**
 * List tight: default PM schema bullet_list/ordered_list has this attr
 * (serializer renderList uses it for tight/loose layout); **Tiptap bulletList lacks it**.
 *
 * Thus list JSON from generateJSON (tables / typography / strikethrough) lacks this key ⇒
 * fromJSON fills schema default false ⇒ **tight lists are written back as loose lists**.
 * Observed: merely adding `~~删除线~~`，`- 一\n- 二` became `- 一\n\n- 二` in the same section; rendering is equivalent,
 * but invents a diff and rev increase (phantom changed, like table-separator normalization).
 *
 * ★ Fill only when the **key is absent**, without changing schema defaults; Markdown-parsed lists carry explicit
 * `tight: true/false`，real information that must remain unchanged (seen in the canvas setcontent incident:
 * removing `tight` on the producer side was immediately blocked by existing round-trip tests).
 * Key **presence** cleanly distinguishes both paths, so the criterion belongs here rather than in schema.
 */
function fillMissingListTight(newType: string | undefined, attrs: JSONContent["attrs"]) {
  if (newType !== "bullet_list" && newType !== "ordered_list") return attrs;
  if (attrs && "tight" in attrs) return attrs;
  return { ...(attrs ?? {}), tight: true };
}

function renameNodeTypes(
  nodes: JSONContent[],
  map: Record<string, string>,
): JSONContent[] {
  return nodes.map((n) => {
    const newType = n.type ? map[n.type] ?? n.type : n.type;
    const out: JSONContent = { ...n, type: newType };
    const attrs = fillMissingListTight(newType, remapCodeBlockAttrs(newType, n.attrs));
    if (attrs !== n.attrs) out.attrs = attrs;
    if (Array.isArray(n.marks)) {
      out.marks = n.marks.map((m) => ({
        ...m,
        type: m.type ? map[m.type] ?? m.type : m.type,
      }));
    }
    if (Array.isArray(n.content)) {
      out.content = renameNodeTypes(n.content, map);
    }
    return out;
  });
}

// report-engine Phase-1: a paragraph whose ENTIRE text is a single
// `[figure:<section_id>]` anchor → lift to a block-level `figure_ref` node
// (deterministic block/inline disambiguation, not LLM-whitespace-dependent —
// tech-design-c §1 / phase1-tech-design §3). Inline `[figure:..]` mid-sentence
// is left as text for the citation-plugin pill path. Done post-parse so it
// works on both the prosemirror-markdown and the generateJSON paths.
const _FIGURE_ANCHOR_RE = /^\[figure:([a-zA-Z0-9_]+)\]$/;
// Anchor at the START of a paragraph's first text node, tolerating trailing
// whitespace. Real LLM output writes the anchor and its `**图N**:` caption on
// consecutive lines with NO blank line between, so CommonMark merges them into
// ONE paragraph ([anchor, softbreak→space/hardBreak, caption…]). The strict
// "paragraph is ONLY the anchor" check missed that → anchors leaked as literal
// `[figure:id]` text (dogfood F7). We instead split: lift the leading anchor to
// a `figure_ref` block and keep the remainder (the caption) as its own paragraph.
const _FIGURE_ANCHOR_LEAD_RE = /^\s*\[figure:([a-zA-Z0-9_]+)\]\s*/;
function liftFigureAnchors(nodes: JSONContent[]): JSONContent[] {
  const out: JSONContent[] = [];
  for (const n of nodes) {
    if (
      n.type === "paragraph" &&
      Array.isArray(n.content) &&
      n.content.length >= 1 &&
      n.content[0]?.type === "text" &&
      typeof n.content[0].text === "string"
    ) {
      const m = _FIGURE_ANCHOR_LEAD_RE.exec(n.content[0].text);
      if (m) {
        out.push({ type: "figure_ref", attrs: { targetSectionId: m[1] } });
        // remainder of the merged paragraph (the caption, if any) → own paragraph
        const rest: JSONContent[] = [];
        const head = n.content[0].text.slice(m[0].length);
        if (head.length) rest.push({ ...n.content[0], text: head });
        for (let i = 1; i < n.content.length; i++) rest.push(n.content[i]!);
        // drop a leading hardBreak / whitespace-only text node left by the split
        while (
          rest.length &&
          (rest[0]?.type === "hardBreak" ||
            (rest[0]?.type === "text" &&
              typeof rest[0].text === "string" &&
              rest[0].text.trim() === ""))
        ) {
          rest.shift();
        }
        if (rest.length) out.push({ ...n, content: rest });
        continue;
      }
    }
    out.push(n);
  }
  return out;
}

/**
 * Fix two codeBlock distortions on the generateJSON path. Traverse in doc order, consuming langs in that order.
 *
 * ① **Unbounded trailing-newline growth**: prosemirror-markdown fence parsing uses
 *    withoutTrailingNewline(tok.content), while markdown-it → HTML → generateJSON retains
 *    <pre> newlines (markdown-it always adds one) unchanged in codeBlock text. On write-back,
 *    serializer calls ensureNewLine again, so **each save adds a blank line**; observed in a table-containing section,
 *    ```python grew 38 → 39 → 40 → 41 bytes over four generations. Remove one newline to make both directions fixed points.
 *
 * ② **Language loss**: Tiptap CodeBlock reads language from element.firstElementChild.classList,
 *    but `@tiptap/html` DOM uses zeed-dom with children / classList / querySelector,
 *    **without firstElementChild** ⇒ undefined ⇒ language always null (observed ```python becoming
 *    bare ``` after one save). Bypass this API gap: read languages from the **same HTML string**, in <pre> order.
 *
 *    ★ Do not revert to markdown-it tokens: the token stream has only fence / code_block, but
 *    with html:true (#305 typography), raw body <pre> uses html_block tokens; these also
 *    become codeBlock through generateJSON, causing **different counts and globally misaligned languages**.
 *    Self-test observed raw <pre> taking the following ```python language, while ```python became null,
 *    worse than loss: assigning language **to the wrong code block**.
 *    HTML is generateJSON **sole input**; counting its <pre> elements guarantees alignment.
 */
function normalizeCodeBlocks(nodes: JSONContent[], langs: (string | null)[]): JSONContent[] {
  let cursor = 0;
  const walk = (list: JSONContent[]): JSONContent[] =>
    list.map((n) => {
      if (n.type === "codeBlock") {
        const language = langs[cursor++] ?? null;
        const out: JSONContent = { ...n, attrs: { ...(n.attrs ?? {}), language } };
        if (Array.isArray(n.content)) {
          const last = n.content[n.content.length - 1];
          if (last?.type === "text" && typeof last.text === "string" && last.text.endsWith("\n")) {
            const text = last.text.slice(0, -1);
            const content = n.content.slice(0, -1);
            if (text.length) content.push({ ...last, text });
            out.content = content;
          }
        }
        return out;
      }
      if (Array.isArray(n.content)) return { ...n, content: walk(n.content) };
      return n;
    });
  return walk(nodes);
}

/**
 * Markdown string → array of top-level PM child nodes.
 * Empty input returns [{ type: "paragraph" }] (PM schema requires block+).
 *
 * Two paths:
 *   - GFM table present: markdown-it → HTML → generateJSON (Tiptap Table extension)
 *   - Otherwise: prosemirror-markdown defaultParser (existing path, 30/30 tests)
 */
export function markdownToPMNodes(md: string): JSONContent[] {
  if (!md || !md.trim()) {
    return [{ type: "paragraph" }];
  }
  // ⚠ Previously stripped [cite:N] directly here (reason: canvas lacks a cite resolver,
  // otherwise raw [cite:2] leaks), putting **display-time cleanup** in the **round-trip read side**.
  // Silent data loss: round-trip truncates text without marking degraded, making it
  // savable in edit mode. Typing one character anywhere makes flush PATCH the section without [cite:1],
  // permanently erasing its anchor (observed in sec_f828f0c2af72 on 116 dogfood, 2026-08-15).
  // Also makes read-only loads always differ, fueling the P0 where read-only flush blocked exports.
  //
  // Text now enters the PM doc **unchanged**; citation-plugin decorations hide it
  // (like figure-caption prefixes: hide display, retain original text). For round-trip fidelity guards, see
  // write-fidelity.test.ts。
  // GFM table / strikethrough / #305 inline typography path: client-side only (generateJSON needs DOM)
  const hasTable = hasMarkdownTable(md);
  const hasTypo = hasInlineTypography(md);
  const hasStrike = hasMarkdownStrike(md);
  if (typeof window !== "undefined" && (hasTable || hasTypo || hasStrike)) {
    try {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const MarkdownIt = require("markdown-it");
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const { generateJSON } = require("@tiptap/core");
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const StarterKit = EditorStarterKit;
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const Table = require("@tiptap/extension-table").Table;
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const TableRow = require("@tiptap/extension-table-row").default;
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const TableCell = require("@tiptap/extension-table-cell").default;
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const TableHeader = require("@tiptap/extension-table-header").default;
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const Link = require("@tiptap/extension-link").default;
      // #305: enable html:true with typography so markdown-it parses inline spans
      // (table-only path retains html:false for safety)
      //
      // Disable linkify: it turns **bare URLs** into <a>, unlike the commonmark preset
      // defaultMarkdownParser path; both produce different docs for the same text, the defect class
      // addressed here. Once a bare URL becomes a link mark, writes emit <https://…> autolink syntax,
      // inventing a diff on first editing of table-containing sections. Explicit [text](url) links are unaffected.
      const mdIt = MarkdownIt({ html: hasTypo, linkify: false });
      const html = mdIt.render(md);
      // Read languages in HTML <pre> order (see normalizeCodeBlocks ②).
      // Count generateJSON **input itself**: each <pre> produces exactly one codeBlock,
      // so the array always matches tree codeBlock order and count, whether <pre> came from a fence
      // or raw body HTML.
      const codeLangs: (string | null)[] = [];
      const preRe = /<pre\b[^>]*>\s*(?:<code\b([^>]*)>)?/g;
      let preM: RegExpExecArray | null;
      while ((preM = preRe.exec(html)) !== null) {
        const cls = /class\s*=\s*["']([^"']*)["']/.exec(preM[1] ?? "")?.[1] ?? "";
        codeLangs.push(/(?:^|\s)language-(\S+)/.exec(cls)?.[1] ?? null);
      }
      const baseExt = [
        StarterKit,
        Table.configure({ resizable: false }),
        TableRow,
        TableCell,
        TableHeader,
        // ⚠ Link was **never registered**: StarterKit (v2) lacks it, while report-view editor separately
        // installs Link. Merely adding a table silently parsed every hyperlink in the section into
        // plain text; before table schema was fixed, degradation blocked persistence, afterward it became **silent writes**.
        // Observed: [官网](https://…) remains intact without tables; adding one table leaves only "官网".
        Link.configure({ openOnClick: false }),
      ];
      // #305: add textStyle/font extensions only with typography; otherwise retain table behavior.
      if (hasTypo) {
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        const TextStyle = require("@tiptap/extension-text-style").TextStyle;
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        const FontFamily = require("@tiptap/extension-font-family").default;
        baseExt.push(TextStyle, FontFamily, _FontSize, _FontWeight);
      }
      const doc = generateJSON(html, baseExt);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const content = (doc as any).content ?? [{ type: "paragraph" }];
      return liftFigureAnchors(normalizeCodeBlocks(content, codeLangs));
    } catch (err) {
      console.warn("[markdown-pm] html path failed, fallback:", err);
    }
  }
  const parsed = defaultMarkdownParser.parse(md);
  if (!parsed) {
    return [
      {
        type: "paragraph",
        content: [{ type: "text", text: md }],
      },
    ];
  }
  const json = parsed.toJSON() as JSONContent;
  const raw = (json.content as JSONContent[]) ?? [{ type: "paragraph" }];
  return liftFigureAnchors(renameNodeTypes(raw, PM_TO_TIPTAP));
}

export interface PmNodesToMarkdownOptions {
  /**
   * Callback on **degraded** serialization; argument is the degradation reason.
   *
   * Degradation means no faithful Markdown output, falling back to extractPlainText:
   * headings/bold/tables/figure anchors all lost, original mark boundaries become newlines.
   *
   * Read path (markdownToPMNodes) explicitly loads Tiptap Table for GFM tables,
   * producing tableHeader etc.; schemaWithTextStyle here lacks table,
   * making content readable but unwritable: table-containing bodies necessarily degrade.
   *
   * Contract: **write path must reject persistence on this signal** (saving degraded text as Markdown silently loses data);
   * read-only comparisons (dirty calculation / unsaved-change checks before export) may tolerate it because they persist nothing.
   */
  onDegrade?: (reason: string) => void;
}

/**
 * Array of top-level PM child nodes → Markdown string.
 * prosemirror-markdown serializer takes PM Node, so deserialize JSONContent first.
 *
 * ⚠ Result may be **degraded**; pass opts.onDegrade to assess fidelity.
 */
export function pmNodesToMarkdown(
  nodes: JSONContent[],
  opts?: PmNodesToMarkdownOptions,
): string {
  if (!nodes || nodes.length === 0) return "";
  // Convert Tiptap (camelCase) → PM (snake_case) names (reverse)
  const pmNodes = renameNodeTypes(nodes, TIPTAP_TO_PM);
  // Wrap in doc shape for serialization
  const docJson = { type: "doc", content: pmNodes };
  // #305: deserialize with schemaWithTextStyle (default + textStyle mark), preserving typography
  const schema = schemaWithTextStyle;
  let pmNode: ProseMirrorNode;
  try {
    pmNode = ProseMirrorNode.fromJSON(schema, docJson);
  } catch (e) {
    // Incompatible schema (e.g. custom Tiptap mark absent from default schema) → fallback
    console.warn("[pmNodesToMarkdown] schema fromJSON failed:", e);
    opts?.onDegrade?.(e instanceof Error ? e.message : String(e));
    return extractPlainText(nodes);
  }
  try {
    const serialized = bowheadSerializer.serialize(pmNode);
    return unescapeBowheadPlaceholders(serialized);
  } catch (e) {
    // Schema recognizes the node but serializer lacks its rule (only half the paired implementation exists) →
    // also degradation, not success.
    console.warn("[pmNodesToMarkdown] serialize failed:", e);
    opts?.onDegrade?.(e instanceof Error ? e.message : String(e));
    return extractPlainText(nodes);
  }
}

/**
 * Fallback: extract only plain text from nodes.
 */
function extractPlainText(nodes: JSONContent[]): string {
  const out: string[] = [];
  function walk(n: JSONContent | undefined) {
    if (!n) return;
    if (n.type === "text" && typeof n.text === "string") {
      out.push(n.text);
      return;
    }
    if (Array.isArray(n.content)) {
      for (const c of n.content) walk(c);
    }
  }
  for (const n of nodes) walk(n);
  return out.join("\n");
}
