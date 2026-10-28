/**
 * report-engine Phase-1 (WP1) — editor-schema acceptance of figure_ref.
 *
 * The load-bearing bet (review-confirmed): a `group:"block"` atom node IS
 * accepted inside a text-based section's `content:"block+"`. This asserts it
 * against OUR actual extensions (CanvasDocument + narrative + figure_ref),
 * and that an invalid placement (figure_ref as a paragraph child) is rejected
 * — which informs the setContent error boundary (S-FE-5).
 */

import { describe, expect, it } from "vitest";
import { getSchema } from "@tiptap/core";
import { Node as PMNode } from "@tiptap/pm/model";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import { CanvasDocument, makeTextBasedSectionNode } from "../schema";
import { FigureRefExtension } from "../extensions/figure-ref";

const NarrativeNode = makeTextBasedSectionNode({ kind: "narrative" });
const schema = getSchema([
  CanvasDocument,
  StarterKit.configure({ document: false }),
  NarrativeNode,
  FigureRefExtension,
]);

const narrativeWith = (children: unknown[]) => ({
  type: "doc",
  content: [
    {
      type: "section_narrative",
      attrs: { sectionId: "nar_1", meta: {} },
      content: children,
    },
  ],
});

describe("editor schema — figure_ref placement", () => {
  it("figure_ref is a valid block child of a narrative (the bet)", () => {
    const doc = narrativeWith([
      { type: "paragraph", content: [{ type: "text", text: "论点。" }] },
      { type: "figure_ref", attrs: { targetSectionId: "chart_1" } },
      { type: "paragraph", content: [{ type: "text", text: "图1：完成率。" }] },
    ]);
    const node = PMNode.fromJSON(schema, doc);
    expect(() => node.check()).not.toThrow();
    // figure_ref survived inside the narrative with its attr intact
    let found: string | null = null;
    node.descendants((n) => {
      if (n.type.name === "figure_ref") found = n.attrs.targetSectionId as string;
    });
    expect(found).toBe("chart_1");
  });

  it("figure_ref is NOT a valid inline child of a paragraph (informs error boundary)", () => {
    const bad = narrativeWith([
      {
        type: "paragraph",
        content: [{ type: "figure_ref", attrs: { targetSectionId: "chart_1" } }],
      },
    ]);
    // PM coerces invalid placement: fromJSON throws OR check() throws OR the
    // figure_ref is dropped. Any of these is the "must be caught" signal.
    let rejected = false;
    try {
      const node = PMNode.fromJSON(schema, bad);
      node.check();
      let hasFig = false;
      node.descendants((n) => {
        if (n.type.name === "figure_ref") hasFig = true;
      });
      rejected = !hasFig; // dropped during parse
    } catch {
      rejected = true; // threw
    }
    expect(rejected).toBe(true);
  });
});
