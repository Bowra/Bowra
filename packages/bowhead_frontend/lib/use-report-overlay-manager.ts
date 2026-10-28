"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type RefCallback } from "react";

export type ReportEditorOverlayKind = "selection-toolbar" | "block-ai";

type ReportEditorOverlay =
  | { kind: null }
  | { kind: "selection-toolbar" }
  | { kind: "block-ai"; sectionId: string };

type OverlayNodeMap = Record<ReportEditorOverlayKind, HTMLElement | null>;

interface UseReportOverlayManagerArgs {
  enabled: boolean;
  isStreaming: boolean;
  isAIRewriting: boolean;
  getBlockAIAnchor?: (sectionId: string) => Element | null;
}

function escapeAttr(value: string): string {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return CSS.escape(value);
  return value.replace(/["\\]/g, "\\$&");
}

function defaultGetBlockAIAnchor(sectionId: string): Element | null {
  if (typeof document === "undefined") return null;
  return document.querySelector(`[data-section-id="${escapeAttr(sectionId)}"]`);
}

export function useReportOverlayManager({
  enabled,
  isStreaming,
  isAIRewriting,
  getBlockAIAnchor = defaultGetBlockAIAnchor,
}: UseReportOverlayManagerArgs) {
  const [active, setActive] = useState<ReportEditorOverlay>({ kind: null });
  const activeRef = useRef(active);
  const floatingRefs = useRef<OverlayNodeMap>({
    "selection-toolbar": null,
    "block-ai": null,
  });

  useEffect(() => {
    activeRef.current = active;
  }, [active]);

  const closeAll = useCallback(() => {
    setActive({ kind: null });
  }, []);

  const close = useCallback((kind?: ReportEditorOverlayKind) => {
    setActive((current) => {
      if (!kind || current.kind === kind) return { kind: null };
      return current;
    });
  }, []);

  const openSelectionToolbar = useCallback(() => {
    if (!enabled || isStreaming || isAIRewriting) return;
    setActive({ kind: "selection-toolbar" });
  }, [enabled, isAIRewriting, isStreaming]);

  const openBlockAI = useCallback((sectionId: string) => {
    if (!enabled || isStreaming || isAIRewriting) return;
    setActive({ kind: "block-ai", sectionId });
  }, [enabled, isAIRewriting, isStreaming]);

  const registerFloating = useCallback(
    (kind: ReportEditorOverlayKind): RefCallback<HTMLElement> =>
      (node) => {
        floatingRefs.current[kind] = node;
      },
    [],
  );

  useEffect(() => {
    if (!enabled || isStreaming || isAIRewriting) closeAll();
  }, [closeAll, enabled, isAIRewriting, isStreaming]);

  useEffect(() => {
    if (typeof document === "undefined") return;

    const isInsideActiveOverlay = (target: Node, overlay: ReportEditorOverlay) => {
      if (!overlay.kind) return false;
      const floating = floatingRefs.current[overlay.kind];
      if (floating?.contains(target)) return true;
      if (overlay.kind === "block-ai") {
        const anchor = getBlockAIAnchor(overlay.sectionId);
        if (anchor?.contains(target)) return true;
      }
      return false;
    };

    const onPointerDown = (event: PointerEvent) => {
      const overlay = activeRef.current;
      if (!overlay.kind) return;
      const target = event.target as Node | null;
      if (!target || isInsideActiveOverlay(target, overlay)) return;
      closeAll();
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (!activeRef.current.kind) return;
      event.preventDefault();
      event.stopPropagation();
      closeAll();
    };

    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [closeAll, getBlockAIAnchor]);

  return useMemo(() => ({
    activeKind: active.kind,
    blockAISectionId: active.kind === "block-ai" ? active.sectionId : null,
    isSelectionToolbarOpen: active.kind === "selection-toolbar",
    isBlockAIOpen: active.kind === "block-ai",
    openSelectionToolbar,
    openBlockAI,
    close,
    closeAll,
    registerFloating,
  }), [
    active,
    close,
    closeAll,
    openBlockAI,
    openSelectionToolbar,
    registerFloating,
  ]);
}
