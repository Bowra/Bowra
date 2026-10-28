"use client";

import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import {
  chatWidthBounds,
  DEFAULT_SPLIT_RATIO,
  keyboardSplitRatio,
  MIN_CANVAS_PX,
  MIN_CHAT_PX,
  readSplitRatio,
  resolveSplit,
  writeSplitRatio,
} from "@/lib/split-pane";
import { CharIcon } from "@/components/char-icon";

type PanelMode = "split" | "chat" | "canvas";

interface SplitPaneProps {
  /** Presentation changes preserve the same editor and composer slots. */
  layout?: "desktop" | "compact";
  mode: PanelMode;
  /** chat pane — the existing `.v2-ws-left` element. */
  chat: ReactNode;
  /** canvas pane — the existing `.v2-ws-right` element. */
  canvas: ReactNode;
  /** drag chat below its min → canvas takes over (parent flips to canvas-full). */
  onSnapCanvas: () => void;
  /** drag canvas below its min → chat takes over (parent flips to chat-only). */
  onSnapChat: () => void;
  /** a reopen edge was clicked → parent flips back to split. */
  onReopen: () => void;
  /** Feishu 07.06 #26 review[8]: read-only shared reports lack chat → suppress chat reopen rail,
   *  or viewers leave fullscreen canvas for broken split/chat layout by clicking it. */
  soloCanvas?: boolean;
  /** User-facing name of the right pane ("Results" when that workspace is on). */
  canvasLabel?: string;
  /** Hide the right reopen rail until a stable result actually exists. */
  showCanvasReopen?: boolean;
  /** Unseen new results while Results is collapsed → dot on reopen rail, screen-reader name announces new content. */
  canvasHasNew?: boolean;
}

/**
 * layout-redesign P3 — the desktop chat|canvas work area (tech-design §3.3, v10 shell.jsx).
 *
 * Three states, ONE persistent DOM: chat + canvas are ALWAYS mounted and sized/hidden by CSS
 * (never conditionally unmounted) so V2Composer's local draft + pending attachments survive a
 * maximize / collapse / drag-snap (the composer state is locked inside V2Composer and must not
 * be lost — review P1). Only the divider and the reopen edges are conditional (no state to lose).
 *
 * - split  → chat `flex-basis: ratio%` (min 360) + canvas `flex:1` (min 420) + draggable Divider.
 * - canvas → chat hidden + a 46px CollapsedEdge(side=chat) reopener on the left + canvas `flex:1`.
 * - chat   → chat `flex:1` + canvas hidden + a 46px CollapsedEdge(side=canvas) reopener on the right.
 *
 * Drag clamps the chat width to px mins and persists the fraction; dragging a pane past its min
 * snaps to the single-pane state via the callbacks.
 */
export function SplitPane({ mode, chat, canvas, onSnapCanvas, onSnapChat, onReopen, soloCanvas = false, canvasLabel, showCanvasReopen = true, canvasHasNew = false, layout = "desktop" }: SplitPaneProps) {
  const t = useTranslations("split_pane");
  const compact = layout === "compact";
  const areaRef = useRef<HTMLDivElement>(null);
  const [ratio, setRatio] = useState(DEFAULT_SPLIT_RATIO);
  const [dragging, setDragging] = useState(false);
  const [hover, setHover] = useState(false);

  // restore persisted ratio after mount (SSR has no localStorage → default first)
  useEffect(() => {
    setRatio(readSplitRatio());
  }, []);

  // dogfood s_3e854 ③: track the split area's pixel width so the panes get EXPLICIT
  // widths (area − chat) instead of `flex:1`. The flex:1 canvas was being dragged wider
  // than its share by the report's content-size (content-sized pane), pushing the whole
  // split past the viewport and clipping the report. An explicit width can't overflow,
  // and the report (width:100% of the pane) then fills the genuinely-available space.
  const [areaW, setAreaW] = useState(0);
  useEffect(() => {
    const el = areaRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setAreaW(el.clientWidth));
    ro.observe(el);
    setAreaW(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const onMove = (clientX: number) => {
    const el = areaRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const res = resolveSplit(clientX, r.left, r.width);
    if (res.kind === "snap") {
      setDragging(false);
      if (res.to === "canvas") onSnapCanvas();
      else onSnapChat();
    } else {
      setRatio(res.ratio);
    }
  };

  const leftPct = ratio * 100;
  const active = hover || dragging;

  // split-mode EXPLICIT pixel widths (dogfood s_3e854 ③). chatPx = clamp(ratio·area, min);
  // canvasPx = the genuine leftover. Before areaW is measured, fall back to the old
  // flex sizing for the very first paint.
  const chatPx = Math.max((leftPct / 100) * areaW, MIN_CHAT_PX);
  const canvasPx = Math.max(areaW - chatPx, MIN_CANVAS_PX);
  const haveArea = areaW > 0;

  // Workbench Spec §9: divider supports keyboard/double-click reset.
  const applyRatio = (next: number) => {
    setRatio(next);
    writeSplitRatio(next);
  };
  const separatorA11y = (() => {
    const bounds = chatWidthBounds(areaW);
    const pct = (px: number) => (haveArea ? Math.round((px / areaW) * 100) : Math.round(leftPct));
    // Narrower area may leave chatPx above allowed maximum (render falls back to minimum width): clamp reading to same bounds, never above valuemax.
    const now = haveArea ? pct(Math.min(bounds.max, Math.max(bounds.min, chatPx))) : Math.round(leftPct);
    return {
      tabIndex: 0,
      "aria-valuenow": now,
      "aria-valuemin": haveArea ? pct(bounds.min) : now,
      "aria-valuemax": haveArea ? pct(bounds.max) : now,
      "aria-valuetext": t("divider_valuetext", { percent: now }),
      "aria-keyshortcuts": "ArrowLeft ArrowRight Home End",
      onKeyDown: (e: ReactKeyboardEvent<HTMLDivElement>) => {
        if (e.altKey || e.metaKey || e.ctrlKey || e.shiftKey) return;
        const next = keyboardSplitRatio(e.key, chatPx, areaW);
        if (next === null) return;
        e.preventDefault();
        applyRatio(next);
      },
      onDoubleClick: () => applyRatio(DEFAULT_SPLIT_RATIO),
    };
  })();

  // chat/canvas kept mounted in every mode — only their box sizing changes.
  const chatStyle: CSSProperties =
    mode === "canvas"
      ? { display: "none" }
      : mode === "chat"
        ? { display: "flex", flex: 1, minWidth: 0, minHeight: 0 }
        : haveArea
          ? { display: "flex", width: chatPx, flexGrow: 0, flexShrink: 0, minWidth: 0, minHeight: 0 }
          : { display: "flex", flexBasis: `${leftPct}%`, flexGrow: 0, flexShrink: 0, minWidth: MIN_CHAT_PX, minHeight: 0 };
  const canvasStyle: CSSProperties =
    mode === "chat"
      ? { display: "none" }
      : mode === "canvas"
        ? { display: "flex", flex: 1, minWidth: 0, minHeight: 0 }
        : haveArea
          ? { display: "flex", width: canvasPx, flexGrow: 0, flexShrink: 0, minWidth: 0, minHeight: 0 }
          : { display: "flex", flex: 1, minWidth: MIN_CANVAS_PX, minHeight: 0 };

  const divider = (
    <div
      role="separator"
      aria-orientation="vertical"
      // The divider also supports keyboard, so its name does not say only Drag.
      aria-label={t("divider_aria_adjust")}
      className={`v2-split-divider${active ? " is-active" : ""}`}
      onPointerDown={(e) => {
        (e.target as Element).setPointerCapture(e.pointerId);
        setDragging(true);
      }}
      onPointerMove={(e) => {
        if (e.buttons) onMove(e.clientX);
      }}
      onPointerUp={() => {
        if (dragging) {
          setDragging(false);
          writeSplitRatio(ratio);
        }
      }}
      onPointerCancel={() => setDragging(false)}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      // Track the chat pane's ACTUAL right edge: the pane is
      // `flex-basis:${leftPct}%` but `min-width:MIN_CHAT_PX` + `flex-shrink:0`,
      // so its width is max(leftPct%, MIN_CHAT_PX). Positioning the divider at
      // the raw `leftPct%` detaches it into the chat content whenever
      // leftPct% of the area falls below MIN_CHAT_PX (narrow laptop + a small
      // saved ratio — dogfood s_3e854 @1280px). Mirror the clamp here.
      style={{ left: `calc(max(${leftPct}%, ${MIN_CHAT_PX}px) - 7px)` }}
      {...separatorA11y}
    >
      <span className="v2-split-divider-line" aria-hidden />
      <span className="v2-split-divider-handle" aria-hidden />
    </div>
  );

  // Keep child slots stable across split, Chat-only and Canvas-only modes.
  return (
    <div
      ref={areaRef}
      className="v2-split-area"
      style={{ flex: 1, display: "flex", minWidth: 0, minHeight: 0, position: "relative" }}
    >
      {!compact && mode === "canvas" && !soloCanvas ? <CollapsedEdge side="chat" onClick={onReopen} /> : null}
      <div className="v2-split-chat" style={chatStyle}>
        {chat}
      </div>
      {/* Divider sits between panes in DOM; Tab order Chat → Divider → Results; absolute positioning preserves visuals. */}
      {!compact && mode === "split" ? divider : null}
      <div className="v2-split-canvas" style={canvasStyle}>
        {canvas}
      </div>
      {!compact && mode === "chat" && showCanvasReopen
        ? <CollapsedEdge side="canvas" onClick={onReopen} label={canvasLabel} hasNew={canvasHasNew} />
        : null}
    </div>
  );
}

/**
 * The 46px collapsed reopener rail (v10 CollapsedChatRail treatment).
 * side="chat" → reopen chat (shown LEFT in canvas-full); side="canvas" → reopen report
 * (shown RIGHT in chat-only) — the shell-owned mirror (tech-design §3.3 P1#5).
 */
function CollapsedEdge({
  side,
  onClick,
  label: labelOverride,
  hasNew = false,
}: {
  side: "chat" | "canvas";
  onClick: () => void;
  label?: string;
  /** Unseen new results behind this rail. */
  hasNew?: boolean;
}) {
  const t = useTranslations("split_pane");
  const label = labelOverride ?? (side === "chat" ? t("label_chat") : t("label_canvas"));
  const chevron = side === "chat" ? "›" : "‹"; // points toward the pane that will expand
  const reopenLabel = hasNew ? t("reopen_has_new", { label }) : t("reopen", { label });
  return (
    <button
      type="button"
      className={`v2-ws-edge ${side === "chat" ? "v2-ws-edge--chat" : "v2-ws-edge--canvas"}`}
      onClick={onClick}
      title={reopenLabel}
      aria-label={reopenLabel}
      {...(hasNew ? { "data-has-new": "1" } : {})}
    >
      <span className="v2-ws-edge-chevron" aria-hidden>
        <CharIcon ch={chevron} aria-hidden />
      </span>
      <span className="v2-ws-edge-label">{label}</span>
      {hasNew ? <span className="v2-ws-new-dot" aria-hidden /> : null}
    </button>
  );
}
