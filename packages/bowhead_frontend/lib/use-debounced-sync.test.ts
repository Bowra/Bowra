/**
 * W1-5 reorder helper tests (extractTopLevelSectionOrder / ordersEqual).
 * Test hook save fallback with renderHook; test pure sorting helpers independently.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pmDocToSections } from "@/lib/canvas-pm-adapter";
import { patchCanvasSection, type PatchResult } from "@/lib/canvas-pm-adapter/canvas-patch";
import { reorderCanvas } from "@/lib/api";
import deMessages from "@/messages/de-DE.json";
import { _clearAllToasts, useToasts } from "./use-toast";
import {
  _clearPendingSaveRetries,
  _getPendingSaveConflictCount,
  _getPendingSaveRetryCount,
  flushFailureBlocksReadOnlyAction,
  retryPendingCanvasSaves,
  useDebouncedSync,
  type FlushFailureReason,
} from "./use-debounced-sync";
import { extractTopLevelSectionOrder, ordersEqual } from "./section-order";

vi.mock("@/lib/canvas-pm-adapter", () => ({
  pmDocToSections: vi.fn(),
}));

// The adapter is mocked above; the projection only feeds the mocked pmDocToSections here.
// use-debounced-sync.projection.test.ts covers the real adapter end to end.
vi.mock("@/lib/canvas-pm-adapter/projected-baseline", () => ({
  projectSectionsThroughEditor: vi.fn(() => new Map()),
}));

vi.mock("@/lib/canvas-pm-adapter/canvas-patch", () => ({
  patchCanvasSection: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  reorderCanvas: vi.fn(),
}));

function createEditorMock(doc: unknown) {
  const handlers = new Map<string, Set<() => void>>();
  return {
    getJSON: vi.fn(() => doc),
    on: vi.fn((event: string, handler: () => void) => {
      const set = handlers.get(event) ?? new Set<() => void>();
      set.add(handler);
      handlers.set(event, set);
    }),
    off: vi.fn((event: string, handler: () => void) => {
      handlers.get(event)?.delete(handler);
    }),
    emitUpdate: () => {
      handlers.get("update")?.forEach((handler) => handler());
    },
  };
}

beforeEach(() => {
  vi.mocked(pmDocToSections).mockReset();
  vi.mocked(patchCanvasSection).mockReset();
  vi.mocked(reorderCanvas).mockReset();
  _clearPendingSaveRetries();
  _clearAllToasts();
});

afterEach(() => {
  _clearPendingSaveRetries();
  _clearAllToasts();
});

describe("flushFailureBlocksReadOnlyAction", () => {
  // List each reason individually, without loop — new reasons first trigger TS exhaustive-switch errors,
  // then force an explicit entry stating whether read-only actions should be blocked.
  const EXPECTED: Record<FlushFailureReason, boolean> = {
    degraded_not_savable: false,
    save_failed: true,
    revision_conflict: true,
    reorder_failed: true,
    rewrite_in_progress: true,
  };

  it("只有『永久写不回但库里完好』这一种放行只读操作", () => {
    for (const [reason, blocks] of Object.entries(EXPECTED)) {
      expect(flushFailureBlocksReadOnlyAction(reason as FlushFailureReason)).toBe(blocks);
    }
    // Positive control: at least one allowed and one blocked. All true/all false means
    // criterion has degraded to constant.
    const values = Object.values(EXPECTED);
    expect(values).toContain(true);
    expect(values).toContain(false);
  });
});

describe("useDebouncedSync", () => {
  it("seeds expected rev from baseline and does not resend an acknowledged patch", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockImplementation((_doc, baseline) => {
      const current = baseline[0] as { markdown?: string } | undefined;
      return [
        {
          sectionId: "sec_1",
          kind: "narrative",
          changed: current?.markdown !== "changed",
          patch: { markdown: "changed" },
        },
      ];
    });
    vi.mocked(patchCanvasSection).mockResolvedValueOnce({ ok: true, rev: 8 });
    const onSaved = vi.fn();

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
        onSaved,
      }),
    );

    let result;
    await act(async () => { result = await syncHook.result.current.flushNow(); });
    expect(result).toEqual({ ok: true, saved_revisions: { sec_1: 8 } });
    expect(patchCanvasSection).toHaveBeenLastCalledWith(
      "cv_1",
      "sec_1",
      { markdown: "changed" },
      7,
    );
    // #3: onSaved third argument now returns patch (caller writes it into store)
    expect(onSaved).toHaveBeenLastCalledWith("sec_1", 8, { markdown: "changed" });
    expect(syncHook.result.current.getSectionRev("sec_1")).toBe(8);

    await act(async () => { await syncHook.result.current.flushNow(); });
    expect(patchCanvasSection).toHaveBeenCalledTimes(1);
    expect(onSaved).toHaveBeenCalledTimes(1);
    expect(syncHook.result.current.getSectionRev("sec_1")).toBe(8);
  });

  it("accepts a server-returned section and advances baseline plus rev", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_design", attrs: { sectionId: "sec_design" } }],
    });
    vi.mocked(pmDocToSections).mockImplementation((_doc, baseline) => {
      const current = baseline[0] as { flattened_artifact_id?: string | null } | undefined;
      return [
        {
          sectionId: "sec_design",
          kind: "design",
          changed: current?.flattened_artifact_id !== "art_flattened001",
          patch: { flattened_artifact_id: "art_flattened001" },
        },
      ];
    });
    const onSaved = vi.fn();

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{
          id: "sec_design",
          kind: "design",
          title: "设计稿",
          size_preset: "portrait_3_4",
          canvas_width: 1080,
          canvas_height: 1440,
          background: { type: "artifact", artifact_id: "art_bg", fit: "cover", opacity: 1 },
          layers: [],
          flattened_artifact_id: null,
          flattened_source_rev: null,
          flattened_at: null,
          rev: 3,
        }],
        canvasId: "cv_1",
        debounceMs: 100,
        onSaved,
      }),
    );

    act(() => {
      syncHook.result.current.acceptServerSection({
        id: "sec_design",
        kind: "design",
        title: "设计稿",
        size_preset: "portrait_3_4",
        canvas_width: 1080,
        canvas_height: 1440,
        background: { type: "artifact", artifact_id: "art_bg", fit: "cover", opacity: 1 },
        layers: [],
        flattened_artifact_id: "art_flattened001",
        flattened_source_rev: 3,
        flattened_at: "2026-06-13T00:00:00Z",
        rev: 4,
      });
    });

    expect(syncHook.result.current.getSectionRev("sec_design")).toBe(4);
    expect(onSaved).toHaveBeenLastCalledWith("sec_design", 4, expect.objectContaining({
      flattened_artifact_id: "art_flattened001",
      flattened_source_rev: 3,
    }));
    await act(async () => { await syncHook.result.current.flushNow(); });
    expect(patchCanvasSection).not.toHaveBeenCalled();
  });

  it("does not cleanup-flush again after the debounce timer already fired", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      { sectionId: "sec_1", kind: "narrative", changed: true, patch: { markdown: "changed" } },
    ]);
    vi.mocked(patchCanvasSection).mockResolvedValue({ ok: true, rev: 8 });

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 0,
      }),
    );

    act(() => editor.emitUpdate());
    await waitFor(() => expect(patchCanvasSection).toHaveBeenCalledTimes(1));

    syncHook.unmount();
    await act(async () => { await Promise.resolve(); });

    expect(patchCanvasSection).toHaveBeenCalledTimes(1);
  });

  it("serializes overlapping flush requests and skips duplicate acknowledged diffs", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockImplementation((_doc, baseline) => {
      const current = baseline[0] as { markdown?: string } | undefined;
      return [
        {
          sectionId: "sec_1",
          kind: "narrative",
          changed: current?.markdown !== "changed",
          patch: { markdown: "changed" },
        },
      ];
    });
    let resolveFirst: (value: PatchResult) => void = () => {};
    vi.mocked(patchCanvasSection).mockImplementationOnce(() => new Promise<PatchResult>((resolve) => {
      resolveFirst = resolve;
    }));

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
      }),
    );

    let firstFlush: Promise<unknown> = Promise.resolve();
    act(() => {
      firstFlush = syncHook.result.current.flushNow();
    });
    await waitFor(() => expect(patchCanvasSection).toHaveBeenCalledTimes(1));
    expect(patchCanvasSection).toHaveBeenLastCalledWith(
      "cv_1",
      "sec_1",
      { markdown: "changed" },
      7,
    );

    let secondFlush: Promise<unknown> = Promise.resolve();
    act(() => {
      secondFlush = syncHook.result.current.flushNow();
    });
    await act(async () => { await Promise.resolve(); });
    expect(patchCanvasSection).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirst({ ok: true, rev: 8 });
      await firstFlush;
      await secondFlush;
    });

    expect(patchCanvasSection).toHaveBeenCalledTimes(1);
  });

  it("silent cleanup flush surfaces save failures via global toast", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      { sectionId: "sec_1", kind: "narrative", changed: true, patch: { markdown: "changed" } },
    ]);
    vi.mocked(patchCanvasSection).mockResolvedValue({
      ok: false,
      status: "error",
      message: "network down",
    });

    const toastsHook = renderHook(() => useToasts());
    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
      }),
    );

    act(() => editor.emitUpdate());
    syncHook.unmount();

    await waitFor(() => expect(toastsHook.result.current.length).toBeGreaterThan(0));
    expect(_getPendingSaveRetryCount()).toBe(1);
    expect(patchCanvasSection).toHaveBeenLastCalledWith(
      "cv_1",
      "sec_1",
      { markdown: "changed" },
      7,
    );
    expect(toastsHook.result.current[0].kind).toBe("error");
    // Already queued for auto-retry: say it will save automatically, without adding contradictory manual-save failure guidance.
    expect(toastsHook.result.current[0].text).toContain("自动再存");
    expect(toastsHook.result.current[0].text).not.toContain("network down");
  });

  describe("out-of-tree save failures: one toast per report, cleared on success, one language", () => {
    function failSilently(canvasId = "cv_1") {
      const editor = createEditorMock({
        type: "doc",
        content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
      });
      const syncHook = renderHook(() =>
        useDebouncedSync({
          editor: editor as never,
          baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
          canvasId,
          debounceMs: 100,
        }),
      );
      act(() => editor.emitUpdate());
      syncHook.unmount();
    }

    beforeEach(() => {
      vi.mocked(pmDocToSections).mockReturnValue([
        { sectionId: "sec_1", kind: "narrative", changed: true, patch: { markdown: "changed" } },
      ]);
    });

    afterEach(() => {
      document.documentElement.lang = "";
    });

    it("repeated failures on the same report keep a single toast", async () => {
      // Diagnostics (status code present) ⇒ Toast includes "复制诊断"; use-toast does not merge by text, so deduplicate here.
      vi.mocked(patchCanvasSection).mockResolvedValue({ ok: false, status: "error", message: "network down", error: { status: 503 } });
      const toastsHook = renderHook(() => useToasts());

      failSilently();
      await waitFor(() => expect(toastsHook.result.current).toHaveLength(1));
      failSilently();
      await waitFor(() => expect(patchCanvasSection).toHaveBeenCalledTimes(2));
      await act(async () => { await Promise.resolve(); });

      expect(toastsHook.result.current.filter((item) => item.kind === "error")).toHaveLength(1);
    });

    it("a successful retry closes the stale failure toast", async () => {
      vi.mocked(patchCanvasSection)
        .mockResolvedValueOnce({ ok: false, status: "error", message: "network down" })
        .mockResolvedValueOnce({ ok: true, rev: 8 });
      const toastsHook = renderHook(() => useToasts());

      failSilently();
      await waitFor(() => expect(toastsHook.result.current.filter((item) => item.kind === "error")).toHaveLength(1));
      await act(async () => { await retryPendingCanvasSaves(); });

      expect(toastsHook.result.current.filter((item) => item.kind === "error")).toHaveLength(0);
      expect(toastsHook.result.current.some((item) => item.kind === "success")).toBe(true);
    });

    it.each([
      ["zh-CN", "报告自动重试没有成功：section 不存在"],
      ["en-US", "Report auto-save retry didn't succeed: Section not found"],
    ])("a failed auto-retry says so in %s", async (locale, expected) => {
      document.documentElement.lang = locale;
      // First failure queues auto-retry; block gone during retry (nonretryable) ⇒ say "自动重试没有成功".
      vi.mocked(patchCanvasSection)
        .mockResolvedValueOnce({ ok: false, status: "error", message: "network down" })
        .mockResolvedValueOnce({ ok: false, status: "not_found" });
      const toastsHook = renderHook(() => useToasts());

      failSilently();
      await waitFor(() => expect(toastsHook.result.current.filter((item) => item.kind === "error")).toHaveLength(1));
      await act(async () => { await retryPendingCanvasSaves(); });

      const errors = toastsHook.result.current.filter((item) => item.kind === "error");
      expect(errors).toHaveLength(1);
      expect(String(errors[0].text)).toBe(expected);
    });

    it("German UI gets one language (English placeholder), not a Chinese/English mix", async () => {
      document.documentElement.lang = "de-DE";
      vi.mocked(patchCanvasSection).mockResolvedValue({ ok: false, status: "error", message: "This change wasn't saved." });
      const toastsHook = renderHook(() => useToasts());

      failSilently();
      await waitFor(() => expect(toastsHook.result.current).toHaveLength(1));

      expect(String(toastsHook.result.current[0].text)).not.toMatch(/[\u4e00-\u9fff]/);
    });

    it("German UI: a save conflict is also one language", async () => {
      document.documentElement.lang = "de-DE";
      vi.mocked(patchCanvasSection).mockResolvedValue({ ok: false, status: "conflict", currentRev: 9 });
      const toastsHook = renderHook(() => useToasts());

      failSilently();
      await waitFor(() => expect(toastsHook.result.current).toHaveLength(1));

      expect(String(toastsHook.result.current[0].text)).toBe(deMessages.report_autosave.save_conflict);
      expect(String(toastsHook.result.current[0].text)).not.toMatch(/[\u4e00-\u9fff]/);
    });
  });

  it("pending silent save failures can retry and clear the global queue", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      { sectionId: "sec_1", kind: "narrative", changed: true, patch: { markdown: "changed" } },
    ]);
    vi.mocked(patchCanvasSection)
      .mockResolvedValueOnce({
        ok: false,
        status: "error",
        message: "network down",
      })
      .mockResolvedValueOnce({ ok: true, rev: 2 });

    const toastsHook = renderHook(() => useToasts());
    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
      }),
    );

    act(() => editor.emitUpdate());
    syncHook.unmount();

    await waitFor(() => expect(_getPendingSaveRetryCount()).toBe(1));
    await retryPendingCanvasSaves();

    expect(_getPendingSaveRetryCount()).toBe(0);
    expect(_getPendingSaveConflictCount()).toBe(0);
    expect(patchCanvasSection).toHaveBeenLastCalledWith(
      "cv_1",
      "sec_1",
      { markdown: "changed" },
      7,
    );
    await waitFor(() => {
      expect(toastsHook.result.current.some((t) => t.kind === "success")).toBe(true);
    });
  });

  it("silent cleanup conflict is preserved outside the retry queue", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      { sectionId: "sec_1", kind: "narrative", changed: true, patch: { markdown: "changed" } },
    ]);
    vi.mocked(patchCanvasSection).mockResolvedValue({
      ok: false,
      status: "conflict",
      currentRev: 3,
    });

    const toastsHook = renderHook(() => useToasts());
    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
      }),
    );

    act(() => editor.emitUpdate());
    syncHook.unmount();

    await waitFor(() => expect(toastsHook.result.current.length).toBeGreaterThan(0));
    expect(_getPendingSaveRetryCount()).toBe(0);
    expect(_getPendingSaveConflictCount()).toBe(1);
    expect(toastsHook.result.current[0].text).toContain("报告保存冲突");
    expect(toastsHook.result.current[0].text).toContain("已保留本地修改");

    const recoveryHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
      }),
    );
    await waitFor(() => {
      expect(recoveryHook.result.current.conflict).toMatchObject({
        sectionId: "sec_1",
        currentRev: 3,
        localPatch: { markdown: "changed" },
      });
    });
  });

  it("retry conflict is not retried again and keeps the local patch recoverable", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      { sectionId: "sec_1", kind: "narrative", changed: true, patch: { markdown: "changed" } },
    ]);
    vi.mocked(patchCanvasSection)
      .mockResolvedValueOnce({
        ok: false,
        status: "error",
        message: "network down",
      })
      .mockResolvedValueOnce({
        ok: false,
        status: "conflict",
        currentRev: 9,
      });

    const toastsHook = renderHook(() => useToasts());
    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
      }),
    );

    act(() => editor.emitUpdate());
    syncHook.unmount();

    await waitFor(() => expect(_getPendingSaveRetryCount()).toBe(1));
    await retryPendingCanvasSaves();

    expect(_getPendingSaveRetryCount()).toBe(0);
    expect(_getPendingSaveConflictCount()).toBe(1);
    expect(patchCanvasSection).toHaveBeenLastCalledWith(
      "cv_1",
      "sec_1",
      { markdown: "changed" },
      7,
    );
    await waitFor(() => {
      expect(toastsHook.result.current.some((t) => String(t.text).includes("手动合并"))).toBe(true);
    });
  });

  it("keeps the conflict patch when force override fails", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      { sectionId: "sec_1", kind: "narrative", changed: true, patch: { markdown: "changed" } },
    ]);
    vi.mocked(patchCanvasSection)
      .mockResolvedValueOnce({
        ok: false,
        status: "conflict",
        currentRev: 9,
      })
      .mockResolvedValueOnce({
        ok: false,
        status: "error",
        message: "network down",
      });

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
      }),
    );

    await act(async () => { await syncHook.result.current.flushNow(); });
    await waitFor(() => {
      expect(syncHook.result.current.conflict).toMatchObject({
        sectionId: "sec_1",
        currentRev: 9,
        localPatch: { markdown: "changed" },
      });
    });

    await act(async () => { await syncHook.result.current.resolveConflictByOverride(); });

    expect(syncHook.result.current.conflict).toMatchObject({
      sectionId: "sec_1",
      currentRev: 9,
      localPatch: { markdown: "changed" },
    });
    expect(syncHook.result.current.lastError?.message).toContain("强制覆盖失败");
    expect(syncHook.result.current.lastError?.message).toContain("network down");
    // #5: force overwrite now sends currentRev seen at conflict (=9), rather than blind no-rev
    expect(patchCanvasSection).toHaveBeenLastCalledWith(
      "cv_1",
      "sec_1",
      { markdown: "changed" },
      9,
    );
  });

  it("returns save_failed instead of treating a queued retry as a successful flush", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      { sectionId: "sec_1", kind: "narrative", changed: true, patch: { markdown: "changed" } },
    ]);
    vi.mocked(patchCanvasSection).mockResolvedValue({
      ok: false,
      status: "error",
      message: "network down",
    });

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
      }),
    );

    let result;
    await act(async () => { result = await syncHook.result.current.flushNow(); });
    expect(result).toEqual({
      ok: false,
      reason: "save_failed",
      section_id: "sec_1",
      message: "network down",
    });
  });

  it("只读态 flush 是 no-op —— 投影产生的 diff 不是「未保存编辑」，不许拿它挡导出", async () => {
    // ⚠ This **overturns** prior enabled:false + diff ⇒ save_failed case.
    //
    // Read-only editor is `editable:false`; users cannot edit, so diffs can only come from
    // canonical → PM projection. Actual shape (116 v1.1.0 `cv_51ac5cfb5a5a`):
    // narrative trailing `[cite:1]` is stripped by `markdownToPMNodes`
    // (canvas has no cite resolver), naturally shortening round-trip, **without degraded
    // marker** ⇒ read-only opening always has diff.
    //
    // Old cost: `prepareForExport` first flush returns !ok ⇒ even
    // `GET /api/v1/canvas/{id}` never runs ⇒ all V2 exports downgrade to legacy (no PDF charts).
    // Yet blocked nothing — chat-workbench still exports same stored content, just rendered worse.
    const roundTripLoss = [
      {
        sectionId: "sec_1",
        kind: "narrative" as const,
        changed: true,
        patch: { markdown: "结论。" },
      },
    ];
    const baseline = [
      { id: "sec_1", kind: "narrative" as const, markdown: "结论。 [cite:1]", rev: 7 },
    ];
    const doc = {
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    };

    vi.mocked(pmDocToSections).mockReturnValue(roundTripLoss);
    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: createEditorMock(doc) as never,
        baseline,
        canvasId: "cv_1",
        enabled: false,
      }),
    );

    let result;
    await act(async () => {
      result = await syncHook.result.current.flushNow();
    });

    expect(result).toEqual({ ok: true, saved_revisions: {} });
    expect(patchCanvasSection).not.toHaveBeenCalled();

    // Positive control: same diff in **edit mode** must persist normally. Without this,
    // no-op above could merely mean hook never saves anything.
    vi.mocked(patchCanvasSection).mockResolvedValue({
      ok: true,
      status: "ok",
      rev: 8,
    } as PatchResult);
    const editing = renderHook(() =>
      useDebouncedSync({
        editor: createEditorMock(doc) as never,
        baseline,
        canvasId: "cv_1",
        enabled: true,
      }),
    );
    await act(async () => { await editing.result.current.flushNow(); });
    expect(patchCanvasSection).toHaveBeenCalledWith(
      "cv_1",
      "sec_1",
      { markdown: "结论。" },
      7,
    );
  });

  it("改动全落降级段 ⇒ degraded_not_savable（不是 save_failed）—— 只读操作据此可以继续", async () => {
    // Write path **permanently** rejects these sections (retry useless), while stored content is intact. Dedicated
    // reason distinguishes unsavable from unreadable: `prepareForExport` permits only former,
    // otherwise one unsavable report section permanently downgrades exports to legacy.
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      {
        sectionId: "sec_1",
        kind: "narrative",
        changed: true,
        degraded: true,
        patch: { markdown: "纯文本兜底" },
      },
    ]);

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "| a | b |", rev: 3 }],
        canvasId: "cv_1",
      }),
    );

    let result;
    await act(async () => { result = await syncHook.result.current.flushNow(); });

    expect(result).toMatchObject({
      ok: false,
      reason: "degraded_not_savable",
      section_id: "sec_1",
    });
    expect(patchCanvasSection).not.toHaveBeenCalled();
  });

  it("returns revision_conflict with the server revision", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      { sectionId: "sec_1", kind: "narrative", changed: true, patch: { markdown: "changed" } },
    ]);
    vi.mocked(patchCanvasSection).mockResolvedValue({
      ok: false,
      status: "conflict",
      currentRev: 9,
    });

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "old", rev: 7 }],
        canvasId: "cv_1",
      }),
    );

    let result;
    await act(async () => { result = await syncHook.result.current.flushNow(); });
    expect(result).toEqual({
      ok: false,
      reason: "revision_conflict",
      section_id: "sec_1",
      current_rev: 9,
      message: expect.stringContaining("远端版本"),
    });
  });

  it("returns reorder_failed and does not patch content after reorder fails", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [
        { type: "section_narrative", attrs: { sectionId: "sec_2" } },
        { type: "section_narrative", attrs: { sectionId: "sec_1" } },
      ],
    });
    vi.mocked(pmDocToSections).mockReturnValue([]);
    vi.mocked(reorderCanvas).mockRejectedValue(new Error("reorder unavailable"));

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [
          { id: "sec_1", kind: "narrative", markdown: "one", rev: 1 },
          { id: "sec_2", kind: "narrative", markdown: "two", rev: 1 },
        ],
        reorderBaseline: ["sec_1", "sec_2"],
        canvasId: "cv_1",
      }),
    );

    let result;
    await act(async () => { result = await syncHook.result.current.flushNow(); });
    expect(result).toEqual({
      ok: false,
      reason: "reorder_failed",
      section_id: "(reorder)",
      // Users see localized explanation, not raw API text.
      message: expect.stringContaining("这次修改没有保存"),
    });
    expect(syncHook.result.current.lastError?.message).not.toContain("reorder unavailable");
    expect(patchCanvasSection).not.toHaveBeenCalled();
  });

  it("全部降级也照发 reorder —— 内容闸不该连坐段落顺序", async () => {
    // ⚠ This **overturns** prior no-reorder-when-all-degraded case.
    // Original reason was all-or-nothing batch semantics, but that atomicity never held
    // (mixed-batch clean-section-save case below saves partially); cost was severe:
    // one unsavable report section makes changed-but-nothing-savable a **permanent state**,
    // preventing even drag-only reorder (measured 0 calls / formerly 1),
    // and newly wired split/merge permanently unusable too. Gate only content persistence.
    const editor = createEditorMock({
      type: "doc",
      content: [
        { type: "section_narrative", attrs: { sectionId: "sec_2" } },
        { type: "section_narrative", attrs: { sectionId: "sec_1" } },
      ],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      {
        sectionId: "sec_1",
        kind: "narrative",
        changed: true,
        degraded: true,
        patch: { markdown: "降级成纯文本" },
      },
    ]);

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [
          { id: "sec_1", kind: "narrative", markdown: "one", rev: 1 },
          { id: "sec_2", kind: "narrative", markdown: "two", rev: 1 },
        ],
        reorderBaseline: ["sec_1", "sec_2"],
        canvasId: "cv_1",
      }),
    );

    let result;
    await act(async () => { result = await syncHook.result.current.flushNow(); });

    expect(result).toMatchObject({ ok: false, reason: "degraded_not_savable", section_id: "sec_1" });
    // Still send order (sec_2 dragged before sec_1)
    expect(reorderCanvas).toHaveBeenCalledWith("cv_1", ["sec_2", "sec_1"]);
    // Still persist no content — narrow gate scope, not strength
    expect(patchCanvasSection).not.toHaveBeenCalled();
    // With no saved sections, **must not** say "其余段落已正常保存"
    expect((result as unknown as { message: string }).message).not.toContain("其余段落已正常保存");
  });

  it("混合批：干净段照常落库，文案才说得上「其余段落已正常保存」", async () => {
    // Positive control of prior case. Distinguish both messages by **what actually happened**; otherwise all-degraded
    // "其余段落已正常保存" falsely reports success to users.
    const editor = createEditorMock({
      type: "doc",
      content: [
        { type: "section_narrative", attrs: { sectionId: "sec_1" } },
        { type: "section_narrative", attrs: { sectionId: "sec_2" } },
      ],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      { sectionId: "sec_1", kind: "narrative", changed: true, degraded: true, patch: { markdown: "降级" } },
      { sectionId: "sec_2", kind: "narrative", changed: true, patch: { markdown: "干净" } },
    ]);
    vi.mocked(patchCanvasSection).mockResolvedValue({ ok: true, rev: 9 });

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [
          { id: "sec_1", kind: "narrative", markdown: "one", rev: 1 },
          { id: "sec_2", kind: "narrative", markdown: "two", rev: 1 },
        ],
        reorderBaseline: ["sec_1", "sec_2"],
        canvasId: "cv_1",
      }),
    );

    let result;
    await act(async () => { result = await syncHook.result.current.flushNow(); });

    expect(patchCanvasSection).toHaveBeenCalledTimes(1);
    expect(vi.mocked(patchCanvasSection).mock.calls[0][1]).toBe("sec_2");
    expect(result).toMatchObject({ ok: false, reason: "degraded_not_savable", section_id: "sec_1" });
    expect((result as unknown as { message: string }).message).toContain("其余段落已正常保存");
    // Persistent badge reason must match (users see that, not return value)
    expect(syncHook.result.current.lastError?.message).toContain("其余段落已正常保存");
  });

  it("returns rewrite_in_progress while sync is suspended", async () => {
    const editor = createEditorMock({ type: "doc", content: [] });
    vi.mocked(pmDocToSections).mockReturnValue([]);
    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [],
        canvasId: "cv_1",
        suspended: true,
      }),
    );

    let result;
    await act(async () => { result = await syncHook.result.current.flushNow(); });
    expect(result).toEqual({
      ok: false,
      reason: "rewrite_in_progress",
      message: "请先接受或撤销改写，再保存、完成编辑或离开报告。",
    });
  });

  it("降级 patch 绝不落库（fail-closed）", async () => {
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      {
        sectionId: "sec_1",
        kind: "narrative",
        changed: true,
        degraded: true,
        patch: { markdown: "被降级成纯文本的内容" },
      },
    ]);

    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "原文", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
      }),
    );

    let result;
    await act(async () => { result = await syncHook.result.current.flushNow(); });

    expect(result).toMatchObject({ ok: false, reason: "degraded_not_savable" });
    // Critical assertion: not a single PATCH request may be sent
    expect(patchCanvasSection).not.toHaveBeenCalled();
  });

  it("后台降级失败弹**常驻** toast —— 不可重试的失败不能 3 秒后自己消失", async () => {
    // 116 measurement (cv_72a32d6a0330): rejected background save toast appeared +23.1s, auto-dismissed +26.4s,
    // leaving only "未保存" badge without reason; users keep editing without successful saves.
    // After unmount no UI can render status; only global toast remains — keep it permanent (durationMs=0).
    const editor = createEditorMock({
      type: "doc",
      content: [{ type: "section_narrative", attrs: { sectionId: "sec_1" } }],
    });
    vi.mocked(pmDocToSections).mockReturnValue([
      {
        sectionId: "sec_1",
        kind: "narrative",
        changed: true,
        degraded: true,
        patch: { markdown: "被降级成纯文本的内容" },
      },
    ]);

    const toastsHook = renderHook(() => useToasts());
    const syncHook = renderHook(() =>
      useDebouncedSync({
        editor: editor as never,
        baseline: [{ id: "sec_1", kind: "narrative", markdown: "原文", rev: 7 }],
        canvasId: "cv_1",
        debounceMs: 100,
      }),
    );

    // Use **background** path (cleanup flush, updateState=false)
    act(() => editor.emitUpdate());
    syncHook.unmount();

    await waitFor(() => expect(toastsHook.result.current.length).toBeGreaterThan(0));
    const t = toastsHook.result.current[0];
    expect(t.kind).toBe("error");
    expect(t.text).toContain("编辑器暂不能写回");
    // Batch has one degraded section ⇒ none saved; toast cannot claim "其余段落已正常保存"
    expect(t.text).not.toContain("其余段落已正常保存");
    // Criterion: no auto-dismiss timer
    expect(t.durationMs).toBe(0);
    expect(patchCanvasSection).not.toHaveBeenCalled();
    // Nonretryable ⇒ never enter retry queue (would infinitely retry same inevitably failing patch)
    expect(_getPendingSaveRetryCount()).toBe(0);
  });
});

describe("extractTopLevelSectionOrder", () => {
  it("空 doc 返空数组", () => {
    expect(extractTopLevelSectionOrder({ type: "doc" })).toEqual([]);
  });

  it("无 attrs.sectionId 节点跳过", () => {
    const doc = {
      type: "doc",
      content: [
        { type: "paragraph", content: [] },
        { type: "section_h1", attrs: { sectionId: "s1" } },
      ],
    };
    expect(extractTopLevelSectionOrder(doc)).toEqual(["s1"]);
  });

  it("按顶层顺序提取 sectionId", () => {
    const doc = {
      type: "doc",
      content: [
        { type: "section_h1", attrs: { sectionId: "h1" } },
        { type: "section_narrative", attrs: { sectionId: "narr1" } },
        { type: "section_chart", attrs: { sectionId: "chart1" } },
      ],
    };
    expect(extractTopLevelSectionOrder(doc)).toEqual(["h1", "narr1", "chart1"]);
  });

  it("不递归子节点 (recursive section.body 内 sectionId 不抽出)", () => {
    const doc = {
      type: "doc",
      content: [
        {
          type: "section_section",
          attrs: { sectionId: "outer" },
          content: [{ type: "section_h1", attrs: { sectionId: "inner_h1" } }],
        },
        { type: "section_h1", attrs: { sectionId: "h1" } },
      ],
    };
    expect(extractTopLevelSectionOrder(doc)).toEqual(["outer", "h1"]);
  });
});

describe("ordersEqual", () => {
  it("同顺序 → true", () => {
    expect(ordersEqual(["a", "b", "c"], ["a", "b", "c"])).toBe(true);
  });

  it("不同顺序 → false", () => {
    expect(ordersEqual(["a", "b", "c"], ["a", "c", "b"])).toBe(false);
  });

  it("不同长度 → false", () => {
    expect(ordersEqual(["a", "b"], ["a", "b", "c"])).toBe(false);
  });

  it("空数组 → true", () => {
    expect(ordersEqual([], [])).toBe(true);
  });

  it("一空一非空 → false", () => {
    expect(ordersEqual(["a"], [])).toBe(false);
  });
});
