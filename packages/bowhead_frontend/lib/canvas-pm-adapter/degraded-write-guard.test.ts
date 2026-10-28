/**
 * Fail-closed guard: degraded Markdown must not be written back as a faithful result.
 *
 * Background (observed on 116, cv_d5986c1367d4): the read path explicitly loads Tiptap Table for GFM tables,
 * producing nodes such as `tableHeader`; the write path schemaWithTextStyle has no table,
 * so `fromJSON` throws `Unknown node type: tableHeader` → falls back to `extractPlainText`.
 * Typing one character in a narrative containing tables reduced its `[figure:]` anchors 2→0,
 * `**` 30→2, and removed all subheadings/tables, irreversible after refresh.
 *
 * This enforces two things:
 * ① Degradation must call `onDegrade` and set `SectionPatch.degraded` to true;
 * ② Normal, non-degraded content must not be mislabeled (which would block valid saves).
 */

import { Editor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import { describe, expect, it } from "vitest";

import { pmNodesToMarkdown } from "./markdown-pm";
import { pmDocToSections } from "./pm-to-canvas";
import type { ReportSection } from "@/lib/types";

/** Degraded node type used here; see the comment on `narrativeWithUnknownNode`. */
const UNREGISTERED_NODE = "taskList";

/**
 * Includes a node unknown to the write-path schema, guaranteeing the `fromJSON` degradation exit.
 *
 * ⚠ **This fixture is synthetic, not a genuinely reachable shape**; the test below explicitly confirms
 * the editor cannot produce it. The old comment vaguely said it represented that category, hiding a real issue:
 * **Reachable** strike (StarterKit Strike was always enabled) was untested right beside it,
 * permanently preventing the whole section from saving while these guard tests remained green.
 *
 * Responsibilities are now explicit:
 *   - The **inventory gate** in `write-fidelity.test.ts` checks that every editor output can be written back,
 *     enumerating the actual editor schema in reverse and missing none;
 *   - This file checks **degradation exits themselves** (callbacks, markers, propagation, per-section rejection),
 *     requiring an input guaranteed not to round-trip faithfully; a synthetic node is appropriate.
 */
function narrativeWithUnknownNode(sectionId: string) {
  return {
    type: "section_narrative",
    attrs: { sectionId, meta: {} },
    content: [
      {
        type: "paragraph",
        content: [
          { type: "text", marks: [{ type: "bold" }], text: "负责人" },
          { type: "text", text: "：品类运营" },
        ],
      },
      {
        type: UNREGISTERED_NODE,
        content: [
          {
            type: "taskItem",
            attrs: { checked: false },
            content: [{ type: "paragraph", content: [{ type: "text", text: "待办" }] }],
          },
        ],
      },
    ],
  };
}

/**
 * Schema recognizes the node but serializer lacks a rule / attrs are malformed: take the **second** catch (serialization throws).
 * Review found this exit had zero coverage: removing it would not fail tests.
 * `image` exists in the default schema, but the serializer internal `.replace` throws when src is not a string.
 */
function narrativeWithBrokenImage(sectionId: string) {
  return {
    type: "section_narrative",
    attrs: { sectionId, meta: {} },
    content: [
      {
        type: "paragraph",
        content: [
          { type: "text", marks: [{ type: "bold" }], text: "标题" },
          { type: "image", attrs: { src: 123, alt: null, title: null } },
        ],
      },
    ],
  };
}

function narrativePlain(sectionId: string) {
  return {
    type: "section_narrative",
    attrs: { sectionId, meta: {} },
    content: [
      {
        type: "paragraph",
        content: [
          { type: "text", marks: [{ type: "bold" }], text: "负责人" },
          { type: "text", text: "：品类运营" },
        ],
      },
    ],
  };
}

describe("降级写回守卫", () => {
  it("fixture 自述：它是合成节点，编辑器产不出 —— 别把它当覆盖率", () => {
    // Turn the fixture being **synthetic** into a failing assertion rather than merely a comment.
    // If taskList is ever added to the editor, this immediately fails to remind the next author:
    // ① Choose another synthetic node; ② add taskList to the write schema too (otherwise it becomes the next strike).
    const schema = new Editor({
      extensions: [StarterKit],
      content: { type: "doc", content: [] } as never,
    }).schema;

    expect(Object.keys(schema.nodes)).not.toContain(UNREGISTERED_NODE);
  });

  it("序列化降级时 onDegrade 被调用，且产物确实丢了格式", () => {
    const reasons: string[] = [];
    const md = pmNodesToMarkdown(
      narrativeWithUnknownNode("sec_x").content as never,
      { onDegrade: (r) => reasons.push(r) },
    );

    expect(reasons.length).toBeGreaterThan(0);
    // Degraded output is plain text: bold markers are gone, and former mark boundaries become newlines
    expect(md).not.toContain("**");
    expect(md).toContain("负责人\n：品类运营");
  });

  it("正常内容不触发降级，且加粗被保真序列化", () => {
    const reasons: string[] = [];
    const md = pmNodesToMarkdown(
      narrativePlain("sec_y").content as never,
      { onDegrade: (r) => reasons.push(r) },
    );

    expect(reasons).toEqual([]);
    expect(md).toContain("**负责人**");
  });

  it("pmDocToSections 把降级段标成 degraded，正常段不标", () => {
    const original: ReportSection[] = [
      { id: "sec_bad", kind: "narrative", markdown: "原文" } as ReportSection,
      { id: "sec_ok", kind: "narrative", markdown: "原文" } as ReportSection,
    ];
    const doc = {
      type: "doc",
      content: [narrativeWithUnknownNode("sec_bad"), narrativePlain("sec_ok")],
    };

    const patches = pmDocToSections(doc as never, original);
    const bad = patches.find((p) => p.sectionId === "sec_bad");
    const ok = patches.find((p) => p.sectionId === "sec_ok");

    expect(bad?.degraded).toBe(true);
    expect(ok?.degraded).toBeUndefined();
  });

  it("降级段仍然会被算作 changed —— 守卫必须靠 degraded 拦，不能指望 diff 为空", () => {
    // Reverse safeguard: degraded output differs from the original, so its diff must be nonempty;
    // without a degraded marker, the write path would PATCH over it normally.
    const original: ReportSection[] = [
      {
        id: "sec_bad",
        kind: "narrative",
        markdown: "**负责人**：品类运营\n\n| 品牌 |\n|---|\n",
      } as ReportSection,
    ];
    const doc = { type: "doc", content: [narrativeWithUnknownNode("sec_bad")] };

    const patch = pmDocToSections(doc as never, original)[0];
    expect(patch.changed).toBe(true);
    expect(patch.degraded).toBe(true);
  });

  it("serializer 抛（而非 fromJSON 抛）也算降级 —— 第二条 catch 出口", () => {
    // Review found this exit had zero coverage. The schema recognizes `image`, so `fromJSON` allows it,
    // but a non-string src makes the serializer throw. Combinations recognized by the schema but unwritable by the serializer
    // must also count as degraded, or another silent corrupting write occurs.
    const reasons: string[] = [];
    const md = pmNodesToMarkdown(
      narrativeWithBrokenImage("sec_img").content as never,
      { onDegrade: (r) => reasons.push(r) },
    );

    expect(reasons.length).toBeGreaterThan(0);
    // Degradation exit taken ⇒ bold must be gone
    expect(md).not.toContain("**");
  });

  it("表格已不再降级 —— 根治之后 fail-closed 不该再拦含表格的段", () => {
    // Regression gate: removing the table schema + serializer pair fails here,
    // reminding the next author that fail-closed is a fallback, not the normal path for tables.
    const reasons: string[] = [];
    const withTable = {
      type: "section_narrative",
      attrs: { sectionId: "sec_tbl", meta: {} },
      content: [
        {
          type: "table",
          content: [
            {
              type: "tableRow",
              content: [
                {
                  type: "tableHeader",
                  content: [{ type: "paragraph", content: [{ type: "text", text: "品牌" }] }],
                },
              ],
            },
          ],
        },
      ],
    };

    const md = pmNodesToMarkdown(withTable.content as never, {
      onDegrade: (r) => reasons.push(r),
    });

    expect(reasons).toEqual([]);
    expect(md).toContain("品牌");
  });
});

describe("降级标记的传播（review 指出的两个洞）", () => {
  it("嵌套 section 里子段降级，必须冒泡到外层 wrapper", () => {
    // Originally the recursive branch did not pass opts down, so nested degradation never reached the guard;
    // logs said "拒绝落库" while persistence proceeded, more dangerous than no guard because it falsely appeared fixed.
    const doc = {
      type: "doc",
      content: [
        {
          type: "section_section",
          attrs: { sectionId: "sec_wrap", label: "", h2: "", meta: {} },
          content: [narrativeWithUnknownNode("sec_inner")],
        },
      ],
    };
    const original: ReportSection[] = [
      { id: "sec_wrap", kind: "section", body: [] } as unknown as ReportSection,
    ];

    const patches = pmDocToSections(doc as never, original);

    expect(patches[0]?.sectionId).toBe("sec_wrap");
    expect(patches[0]?.degraded).toBe(true);
  });

  it("doc 里出现重复 sectionId 时，干净段不被误标", () => {
    // Originally lookup by id let degraded sections contaminate clean sections sharing that id (created by copying/pasting an entire section).
    const doc = {
      type: "doc",
      content: [narrativeWithUnknownNode("dup"), narrativePlain("dup")],
    };
    const original: ReportSection[] = [
      { id: "dup", kind: "narrative", markdown: "原文" } as ReportSection,
    ];

    const patches = pmDocToSections(doc as never, original);

    expect(patches).toHaveLength(2);
    expect(patches[0].degraded).toBe(true);
    expect(patches[1].degraded).toBeUndefined();
  });
});
