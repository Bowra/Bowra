/**
 * report-streaming B — boundary between streaming sections and editor documents.
 *
 * Measured (discovery §8.3, real Chromium + real SSE): feeding frequent chunks into TipTap makes
 * a 96-section document produce **one 1833ms blocking task** (freezing the whole page for nearly two seconds), with main-thread occupancy
 * at 58–86%. Streaming content must therefore use read-only preview blocks, outside the ProseMirror doc.
 *
 * Two things establish this boundary, each locked by a test here:
 *  ① report-view excludes sections in `streamingSectionIds` from `filtered` (→ outside PM doc);
 *  ② arriving `section_patched` **removes** that section from the streaming set (→ back into the editor, with one setContent).
 * Missing ② is especially easy: the section would stay in preview forever, never admitting the final version into the editor—users
 * see content but cannot edit it, and export/reconciliation cannot access it either.
 */

import { describe, expect, it, beforeEach } from "vitest";

import {
  getCanvasSnapshot,
  onSectionAdded,
  onSectionChunk,
  onSectionPatched,
  onSectionStreaming,
  resetCanvas,
  selectStreamingSectionIdKey,
} from "./canvas-store";
import type { ReportSection } from "./types";

const SEC = "sec_narr";

function narrative(id: string, markdown: string): ReportSection {
  return { id, kind: "narrative", markdown, turn_id: "t1" } as unknown as ReportSection;
}

beforeEach(() => resetCanvas());

describe("流式段的编辑器边界", () => {
  it("① section_streaming 之后，该段出现在流式集合里（据此被排除出 PM doc）", () => {
    onSectionAdded({ section: narrative(SEC, "") });
    expect(selectStreamingSectionIdKey(getCanvasSnapshot())).toBe("");

    onSectionStreaming({ section_id: SEC, kind: "narrative" });

    expect(selectStreamingSectionIdKey(getCanvasSnapshot())).toBe(SEC);
  });

  it("chunk 累积进该段，但它仍在流式集合里（内容有了，位置还在预览块）", () => {
    onSectionAdded({ section: narrative(SEC, "") });
    onSectionStreaming({ section_id: SEC, kind: "narrative" });
    onSectionChunk({ section_id: SEC, delta: "华东区 GMV " });
    onSectionChunk({ section_id: SEC, delta: "环比增长 8.3%。" });

    const s = getCanvasSnapshot();
    const sec = s.sections.find((x) => (x as unknown as { id: string }).id === SEC);
    expect((sec as unknown as { markdown: string }).markdown).toBe("华东区 GMV 环比增长 8.3%。");
    expect(selectStreamingSectionIdKey(s)).toBe(SEC);
  });

  it("★ ② section_patched 把该段移出流式集合——终稿回到编辑器", () => {
    onSectionAdded({ section: narrative(SEC, "") });
    onSectionStreaming({ section_id: SEC, kind: "narrative" });
    onSectionChunk({ section_id: SEC, delta: "毛坯正文" });

    // Backend finishes whole-document hygiene → write_report update → patch (authoritative signal that content is finalized).
    onSectionPatched({
      section_id: SEC,
      patch: { kind: "narrative", markdown: "终稿正文 [1]" },
      rev: 2,
    });

    const s = getCanvasSnapshot();
    expect(selectStreamingSectionIdKey(s)).toBe("");
    const sec = s.sections.find((x) => (x as unknown as { id: string }).id === SEC);
    expect((sec as unknown as { markdown: string }).markdown).toBe("终稿正文 [1]");
  });

  it("多段并流互不串台（多写手路：各域一个槽）", () => {
    onSectionAdded({ section: narrative("sec_a", "") });
    onSectionAdded({ section: narrative("sec_b", "") });
    onSectionStreaming({ section_id: "sec_a", kind: "narrative" });
    onSectionStreaming({ section_id: "sec_b", kind: "narrative" });
    onSectionChunk({ section_id: "sec_a", delta: "甲章" });
    onSectionChunk({ section_id: "sec_b", delta: "乙章" });

    const s = getCanvasSnapshot();
    const get = (id: string) =>
      (s.sections.find((x) => (x as unknown as { id: string }).id === id) as unknown as {
        markdown: string;
      }).markdown;
    expect(get("sec_a")).toBe("甲章");
    expect(get("sec_b")).toBe("乙章");
    expect(selectStreamingSectionIdKey(s)).toBe("sec_a,sec_b");

    // Chapter A is finalized while chapter B is still streaming.
    onSectionPatched({ section_id: "sec_a", patch: { kind: "narrative", markdown: "甲章终稿" } });
    expect(selectStreamingSectionIdKey(getCanvasSnapshot())).toBe("sec_b");
  });

  it("选择器返回稳定字符串（新 Set 会让 useSyncExternalStore 无限重渲）", () => {
    onSectionAdded({ section: narrative(SEC, "") });
    onSectionStreaming({ section_id: SEC, kind: "narrative" });

    const a = selectStreamingSectionIdKey(getCanvasSnapshot());
    const b = selectStreamingSectionIdKey(getCanvasSnapshot());
    expect(a).toBe(b);
    expect(typeof a).toBe("string");
  });
});
