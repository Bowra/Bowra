import { act, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useReportOverlayManager } from "./use-report-overlay-manager";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("useReportOverlayManager", () => {
  it("keeps editor overlays mutually exclusive", () => {
    const { result } = renderHook(() =>
      useReportOverlayManager({
        enabled: true,
        isStreaming: false,
        isAIRewriting: false,
      }),
    );

    act(() => result.current.openBlockAI("sec_1"));
    expect(result.current.activeKind).toBe("block-ai");
    expect(result.current.blockAISectionId).toBe("sec_1");

    act(() => result.current.openSelectionToolbar());
    expect(result.current.activeKind).toBe("selection-toolbar");
    expect(result.current.blockAISectionId).toBeNull();
  });

  it("closes the active editor overlay on Escape", () => {
    const { result } = renderHook(() =>
      useReportOverlayManager({
        enabled: true,
        isStreaming: false,
        isAIRewriting: false,
      }),
    );

    act(() => result.current.openSelectionToolbar());
    expect(result.current.activeKind).toBe("selection-toolbar");

    fireEvent.keyDown(document, { key: "Escape" });
    expect(result.current.activeKind).toBeNull();
  });

  it("closes block AI on outside pointerdown while keeping its bar and anchor interactive", () => {
    const section = document.createElement("section");
    section.dataset.sectionId = "sec_1";
    const bar = document.createElement("div");
    const insideBar = document.createElement("button");
    const outside = document.createElement("button");
    bar.appendChild(insideBar);
    document.body.append(section, bar, outside);

    const { result } = renderHook(() =>
      useReportOverlayManager({
        enabled: true,
        isStreaming: false,
        isAIRewriting: false,
      }),
    );

    act(() => result.current.registerFloating("block-ai")(bar));
    act(() => result.current.openBlockAI("sec_1"));

    fireEvent.pointerDown(insideBar);
    expect(result.current.activeKind).toBe("block-ai");

    fireEvent.pointerDown(section);
    expect(result.current.activeKind).toBe("block-ai");

    fireEvent.pointerDown(outside);
    expect(result.current.activeKind).toBeNull();
  });

  it("closes the selection toolbar on outside pointerdown while keeping toolbar clicks interactive", () => {
    const toolbar = document.createElement("div");
    const insideToolbar = document.createElement("button");
    const outside = document.createElement("button");
    toolbar.appendChild(insideToolbar);
    document.body.append(toolbar, outside);

    const { result } = renderHook(() =>
      useReportOverlayManager({
        enabled: true,
        isStreaming: false,
        isAIRewriting: false,
      }),
    );

    act(() => result.current.registerFloating("selection-toolbar")(toolbar));
    act(() => result.current.openSelectionToolbar());

    fireEvent.pointerDown(insideToolbar);
    expect(result.current.activeKind).toBe("selection-toolbar");

    fireEvent.pointerDown(outside);
    expect(result.current.activeKind).toBeNull();
  });

  it("tears down editor overlays when editability, streaming, or rewrite state changes", () => {
    const { result, rerender } = renderHook(
      ({ enabled, isStreaming, isAIRewriting }) =>
        useReportOverlayManager({ enabled, isStreaming, isAIRewriting }),
      {
        initialProps: {
          enabled: true,
          isStreaming: false,
          isAIRewriting: false,
        },
      },
    );

    act(() => result.current.openBlockAI("sec_1"));
    rerender({ enabled: true, isStreaming: true, isAIRewriting: false });
    expect(result.current.activeKind).toBeNull();

    act(() => result.current.openSelectionToolbar());
    expect(result.current.activeKind).toBeNull();

    rerender({ enabled: true, isStreaming: false, isAIRewriting: false });
    act(() => result.current.openSelectionToolbar());
    rerender({ enabled: true, isStreaming: false, isAIRewriting: true });
    expect(result.current.activeKind).toBeNull();

    rerender({ enabled: true, isStreaming: false, isAIRewriting: false });
    act(() => result.current.openBlockAI("sec_1"));
    rerender({ enabled: false, isStreaming: false, isAIRewriting: false });
    expect(result.current.activeKind).toBeNull();
  });
});
