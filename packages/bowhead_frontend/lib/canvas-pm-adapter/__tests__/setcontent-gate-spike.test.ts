/**
 * SPIKE: why the report-view.tsx content gate is always true (root cause of ~13 setContent calls per full-page load on 116).
 *
 * Background and measurements: docs/canvas-report-optimization/metrics-transport-2026-08-08.md §2.
 * The effect has two gates: ① `prevInitialDocRef.current === initialDoc` (object identity)
 * ② `JSON.stringify(editor.getJSON()) !== JSON.stringify(initialDoc)` (content).
 * The document is already full on the first setContent, implicating ② being always true.
 *
 * This is a **diagnostic experiment**, not a behavior assertion: it fixes the conclusion and fails if it changes.
 */

import { Editor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import Link from "@tiptap/extension-link";
import { Table } from "@tiptap/extension-table";
import TableRow from "@tiptap/extension-table-row";
import TableCell from "@tiptap/extension-table-cell";
import TableHeader from "@tiptap/extension-table-header";
import { TextStyle } from "@tiptap/extension-text-style";
import FontFamily from "@tiptap/extension-font-family";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

import { CanvasDocument, canvasToPMDoc, getAllCanvasExtensions, normalizeDocForEditor } from "..";
import { pmDocToSections } from "../pm-to-canvas";
import { FontSize } from "../extensions/font-size";
import { FontWeight } from "../extensions/font-weight";
import type { ReportSection } from "@/lib/types";
import { FIXTURE_deck } from "./fixtures";

/** Same extensions as report-view.tsx:401-432 (omit decorative/interactive extensions, absent from schema serialization). */
function makeEditor(content: unknown): Editor {
  return new Editor({
    extensions: [
      CanvasDocument,
      StarterKit.configure({ document: false }),
      TextStyle,
      FontFamily,
      FontSize,
      FontWeight,
      Link.configure({ openOnClick: false }),
      Table.configure({ resizable: false }),
      TableRow,
      TableCell,
      TableHeader,
      ...getAllCanvasExtensions(),
    ],
    content: content as never,
  });
}

/** Narrative with a Markdown bullet list: present in 183 of 288 canvases (64%) on 116. */
const LIST_SECTION = {
  id: "s_list",
  kind: "narrative",
  // ⚠ Do not leave `\n` at the end: the serializer normalizes it away, introducing a diff unrelated to this spike.
  markdown: "结论如下:\n\n- 第一条\n- 第二条",
  rev: 3,
  turn_id: "t_spike",
} as unknown as ReportSection;

describe("SPIKE 1：内容闸恒真的根因 = 列表的 `tight` attr 两边 schema 不一致", () => {
  test("canvasToPMDoc 产出的 bulletList 带 `tight`", () => {
    const doc = canvasToPMDoc([LIST_SECTION]);
    const json = JSON.stringify(doc);
    expect(json).toContain('"type":"bulletList"');
    // Supplied by prosemirror-markdown defaultMarkdownParser
    expect(json).toContain('"tight":true');
  });

  test("★ 灌进 editor 再取回来，`tight` 被 schema 丢掉 ⇒ 两边永不相等", () => {
    const doc = canvasToPMDoc([LIST_SECTION]);
    const editor = makeEditor(doc);
    const back = editor.getJSON();
    editor.destroy();

    // Tiptap StarterKit bulletList does not declare tight, so ProseMirror parsing drops it.
    expect(JSON.stringify(back)).not.toContain('"tight"');
    // ⇒ report-view content gate `currentJson !== nextJson` is always true for **any report containing lists**.
    expect(JSON.stringify(back)).not.toBe(JSON.stringify(doc));
  });

  test("对照：不含列表的报告，往返是相等的（闸能正常关上）", () => {
    const plain = {
      id: "s_plain",
      kind: "narrative",
      markdown: "只有一段话，没有列表。",
      rev: 3,
      turn_id: "t_spike",
    } as unknown as ReportSection;
    const doc = canvasToPMDoc([plain]);
    const editor = makeEditor(doc);
    const back = editor.getJSON();
    editor.destroy();
    expect(JSON.stringify(back)).toBe(JSON.stringify(doc));
  });
});

describe("★ 回归守卫：修好之后，闸对「同一份 doc」必须关得上", () => {
  /** Reproduce the report-view.tsx criterion (fixed version). */
  function gateWouldFire(editor: Editor, nextDoc: unknown): boolean {
    const currentJson = JSON.stringify(editor.getJSON());
    const nextJson = JSON.stringify(
      normalizeDocForEditor(editor.schema, nextDoc as never),
    );
    return currentJson !== nextJson;
  }

  test("含列表的报告：套用一次之后，再拿同一份 doc 来闸不再开", () => {
    const doc = canvasToPMDoc([LIST_SECTION]);
    const editor = makeEditor(doc);
    // Previously true (`tight` kept the sides unequal) → unnecessarily rebuilt the full document on every rerender.
    expect(gateWouldFire(editor, doc)).toBe(false);
    editor.destroy();
  });

  test("内容真的变了，闸照常开（没有把该套用的更新吞掉）", () => {
    const doc = canvasToPMDoc([LIST_SECTION]);
    const editor = makeEditor(doc);
    const changed = canvasToPMDoc([
      { ...LIST_SECTION, markdown: "结论如下:\n\n- 第一条\n- 第二条\n- 第三条" } as never,
    ]);
    expect(gateWouldFire(editor, changed)).toBe(true);
    editor.destroy();
  });

  test("normalizeDocForEditor 与 setContent 同源：归一化结果 == 灌进去再取回来", () => {
    const doc = canvasToPMDoc([LIST_SECTION]);
    const editor = makeEditor(doc);
    expect(JSON.stringify(normalizeDocForEditor(editor.schema, doc))).toBe(
      JSON.stringify(editor.getJSON()),
    );
    editor.destroy();
  });

  test("坏 doc 不抛异常，退回原样（宁可多做一次也不吞更新）", () => {
    const editor = makeEditor(canvasToPMDoc([LIST_SECTION]));
    const bad = { type: "doc", content: [{ type: "no_such_node_type" }] } as never;
    expect(() => normalizeDocForEditor(editor.schema, bad)).not.toThrow();
    expect(normalizeDocForEditor(editor.schema, bad)).toBe(bad);
    editor.destroy();
  });
});

describe("★ 结构守卫：report-view 的真实调用点", () => {
  // Assertions above reproduce gate logic **without report-view**; alone, they would not fail if the call site reverted
  // to comparing against the original initialDoc (a repeated repository pitfall:
  // [[feedback_test_fixture_supplies_what_caller_wont]]). This enforces the actual call site.
  // vitest root = packages/bowhead_frontend
  const src = () => readFileSync(resolve(process.cwd(), "components/canvas/report-view.tsx"), "utf-8");

  test("内容闸必须与归一化后的 doc 比", () => {
    expect(src()).toContain("normalizeDocForEditor(editor.schema, initialDoc)");
  });

  test("不得退回与原始产出比", () => {
    expect(src()).not.toMatch(/const nextJson = JSON\.stringify\(initialDoc\)/);
  });
});

describe("SPIKE 2：`tight` 不是死数据——摘掉会改 markdown（naive 修法被这条挡住）", () => {
  // ⚠ Conclusion **partly changed** on 2026-08-12: the write path now fills `tight` with `true` only when the **key is absent**
  // (`markdown-pm::fillMissingListTight`), so removing tight no longer visibly affects **tight** lists.
  // This SPIKE warning still holds, but the criterion needs **loose** lists to demonstrate it: removing `tight: false`
  // gets it filled with true ⇒ loose lists become tight ⇒ Markdown still changes.
  // The naive fix of aligning producer output with editor schema by removing tight remains invalid.
  const LOOSE_SECTION = {
    ...LIST_SECTION,
    id: "s_loose",
    markdown: "结论如下:\n\n- 第一条\n\n- 第二条",
  } as unknown as ReportSection;

  test("serializer 的 renderList 读 tight：摘掉它会把松散列表压成紧凑", () => {
    const doc = canvasToPMDoc([LOOSE_SECTION]) as { content: unknown[] };
    const withTight = pmDocToSections(doc as never, [LOOSE_SECTION]);

    // Manually remove tight to simulate the naive fix of aligning producer output with editor schema
    const stripped = JSON.parse(JSON.stringify(doc));
    const walk = (n: { type?: string; attrs?: Record<string, unknown>; content?: unknown[] }) => {
      if ((n.type === "bulletList" || n.type === "orderedList") && n.attrs) delete n.attrs.tight;
      if (Array.isArray(n.content)) n.content.forEach((c) => walk(c as never));
    };
    walk(stripped);
    const withoutTight = pmDocToSections(stripped, [LOOSE_SECTION]);

    expect(withTight[0].changed).toBe(false);
    expect(withoutTight[0].changed).toBe(true);
  });

  test("回填只认「键不存在」，显式 tight 一律照办（补过头会压掉真实的松散）", () => {
    for (const section of [LIST_SECTION, LOOSE_SECTION]) {
      const doc = canvasToPMDoc([section as never]);
      expect(pmDocToSections(doc, [section as never])[0].changed).toBe(false);
    }
  });
});

describe("SPIKE 3：生产保存路径过编辑器——幽灵 changed 已修（2026-08-12）", () => {
  // Original conclusion (retained as diagnostic history): editor bulletList does not declare `tight`, so `editor.getJSON()`
  // lacks the key ⇒ write-path `fromJSON` fills schema default `false` ⇒ tight lists serialize as loose
  // ⇒ narrative containing lists is marked changed with **zero user edits**. On 116, among 288 canvases,
  // 183 (64%) contain Markdown lists, so most reports sent phantom PATCH requests on every flush.
  //
  // Fix: write path fills `tight` with `true` only when the **key is absent** (`fillMissingListTight`).
  // Existing round-trip tests miss this because they use canvasToPMDoc without the editor;
  // these two **must keep using the real editor**, the only production-shaped coverage of this chain.
  test("★ 走真编辑器的保存路径，含列表的 narrative 不再被判 changed", () => {
    const doc = canvasToPMDoc([LIST_SECTION]);
    expect(pmDocToSections(doc, [LIST_SECTION])[0].changed).toBe(false);

    const editor = makeEditor(doc);
    const viaEditor = editor.getJSON();
    editor.destroy();

    expect(pmDocToSections(viaEditor as never, [LIST_SECTION])[0].changed).toBe(false);
  });

  test("对照：deck fixture 同样不再受影响（它也含列表）", () => {
    const deck = { ...FIXTURE_deck, rev: 4, turn_id: "t_spike" } as unknown as ReportSection;
    const doc = canvasToPMDoc([deck]);
    const editor = makeEditor(doc);
    const viaEditor = editor.getJSON();
    editor.destroy();
    expect(pmDocToSections(viaEditor as never, [deck])[0].changed).toBe(false);
  });

  test("残留（已知、刻意）：真·松散列表过一次编辑器会被压成紧凑", () => {
    // The editor **entirely drops** tight/loose distinctions; both return with the key absent,
    // indistinguishable at this seam. Choose true because LLM body lists overwhelmingly use tight layout:
    // loose lists tighten once, but 64% of reports stop sending phantom PATCH requests on every flush.
    const loose = { ...LIST_SECTION, id: "s_loose2", markdown: "结论如下:\n\n- 第一条\n\n- 第二条" } as unknown as ReportSection;
    const editor = makeEditor(canvasToPMDoc([loose]));
    const viaEditor = editor.getJSON();
    editor.destroy();
    expect(pmDocToSections(viaEditor as never, [loose])[0].changed).toBe(true);
  });
});
