/**
 * CitationHoverPopover: hovering [num:..] / [ref:..] shows source details.
 *
 * Plugin writes data-citation-kind / data-source-id / data-value / data-unit DOM attrs;
 * Manager delegates hover events, useFloating computes popover position,
 * and SourceRef metadata from useCanvasContext.sourceLookup supplies the content.
 */

"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import {
  useFloating,
  autoUpdate,
  offset,
  flip,
  shift,
  FloatingPortal,
} from "@floating-ui/react";
import { useCanvasContext } from "../canvas-context";
import { useCallIdLookup } from "../call-id-lookup";
import { copyKbFields, isKbReportSource, isViewerSourceAlias, kbRefOfSource, type SourceRef } from "../citation-types";
import { useKbReportSource } from "../kb-report-source";
import { kbCitationHref } from "@/components/chat/step-list/LeadNarrative";
import { quoteLines } from "@/lib/kb-snippet";
import { sourceBlocksFor, useSourcePanel, type SourceQuality } from "@/components/canvas/source-panel";
import { codeRunForSource, formatSourceCell, type SourceBlock } from "@/lib/source-content";
import { trackInteraction as track } from "@/lib/metrics";
import { formatCnyAmount } from "../format-num";
import { useSourceAccess, useSourceAccessActions } from "@/lib/source-access-registry";
import type { FilePreview } from "@/lib/api";
import { useNarrowViewport } from "@/components/ui/sheet";
import { DialogShell } from "@/components/ui/dialog";
import {
  Database as LucideDatabase,
  File as LucideFile,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { AlertTriangle, ChevronRight } from "@/components/icons";

interface HoverState {
  el: HTMLElement;
  kind: "num" | "ref" | "numbered";
  sourceId: string | null;
  value: string | null;
  unit: string | null;
  /** Phase F: 0-based index into payload.sources[]. */
  sourceIdx: number | null;
  /** Opening method: keyboard (Enter) opening focuses the card; focus opening (Tab onto the badge) leaves focus on it;
   *  narrow-screen tap / keyboard opening uses a bottom sheet. */
  via?: "hover" | "press" | "keyboard" | "focus";
}

/** D13 (WCAG 1.4.13): hover previews open after a short dwell so sweeping over text never flashes cards, */
export const POPOVER_OPEN_DELAY_MS = 300;
/** and stay open while the pointer moves between badge and card, closing after a short grace period. */
export const POPOVER_HIDE_DELAY_MS = 200;

const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

function stateFor(target: HTMLElement, via: HoverState["via"]): HoverState | null {
  const kind = target.dataset.citationKind as "num" | "ref" | "numbered" | undefined;
  if (kind !== "num" && kind !== "ref" && kind !== "numbered") return null;
  const idxAttr = target.dataset.sourceIdx;
  const sourceIdx = idxAttr ? parseInt(idxAttr, 10) : null;
  return {
    el: target,
    kind,
    sourceId: target.dataset.sourceId ?? null,
    value: target.dataset.value ?? null,
    unit: target.dataset.unit ?? null,
    sourceIdx: Number.isFinite(sourceIdx as number) ? (sourceIdx as number) : null,
    via,
  };
}

export function sourceRefFromNumberedEntry(
  entry: Record<string, unknown> | undefined,
  sourceIdx: number,
): { sourceRef?: SourceRef; quality: SourceQuality } {
  if (!entry || typeof entry !== "object") {
    return { quality: "unavailable" };
  }
  const id = typeof entry.source_alias === "string"
    ? entry.source_alias
    : typeof entry.id === "string" ? entry.id : "";
  if (!isViewerSourceAlias(id)) {
    return { quality: "unavailable" };
  }
  const accessible = entry.access_state === "linked_accessible";
  // Knowledge-base answer citations batch 3: distinguish restricted / missing (different copy; restricted says "仅对有权限的成员可见");
  // other non-accessible states still collapse to linked_unavailable. Neither state carries safe_* fields.
  const keptState = entry.access_state === "linked_restricted" || entry.access_state === "linked_missing"
    ? entry.access_state
    : null;
  const sourceRef: SourceRef = {
    id,
    source_alias: id,
    access_state: accessible ? "linked_accessible" : keptState ?? "linked_unavailable",
    generic_type: typeof entry.generic_type === "string"
      ? entry.generic_type as SourceRef["generic_type"]
      : "other_source",
    ordinal: typeof entry.ordinal === "number" ? entry.ordinal : sourceIdx + 1,
  };
  if (accessible) {
    if (typeof entry.safe_label === "string") sourceRef.safe_label = entry.safe_label;
    if (typeof entry.safe_query === "string") sourceRef.safe_query = entry.safe_query;
    if (typeof entry.safe_preview === "string") sourceRef.safe_preview = entry.safe_preview;
    if (Array.isArray(entry.safe_preview_columns)) sourceRef.safe_preview_columns = entry.safe_preview_columns as string[];
    if (Array.isArray(entry.safe_preview_rows)) sourceRef.safe_preview_rows = entry.safe_preview_rows as Record<string, string>[];
    if (typeof entry.safe_total_rows === "number") sourceRef.safe_total_rows = entry.safe_total_rows;
    if (typeof entry.safe_url === "string") sourceRef.safe_url = entry.safe_url;
    if (typeof entry.safe_preview_url === "string") sourceRef.safe_preview_url = entry.safe_preview_url;
    copyKbFields(entry, sourceRef);
  }
  return {
    quality: accessible ? "artifact_only" : "unavailable",
    sourceRef,
  };
}

export function CitationHoverManager({
  rootRef,
}: {
  rootRef: React.RefObject<HTMLElement | null>;
}) {
  const { sourceLookup, sections, numberedSources } = useCanvasContext();
  const { state: panelState, open: openPanel } = useSourcePanel();
  const [hover, setHover] = useState<HoverState | null>(null);
  // Source cards use layer tokens, keyboard opening focuses the card, narrow-screen tap uses a bottom sheet.
  const narrow = useNarrowViewport(true);
  // Sprint ι (PRD §10.1, §3.3): call_id → code_block section_id lookup.
  const callIdLookup = useCallIdLookup(sections);

  // Sprint γ PRD §11.4: panel open → suppress popover (avoid double UI)
  const panelOpen = panelState.citation !== null;
  useEffect(() => {
    if (panelOpen) setHover(null);
  }, [panelOpen]);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const focusedCitationRef = useRef<HTMLElement | null>(null);
  const hoverRef = useRef<HoverState | null>(null);
  hoverRef.current = hover;
  const openTrustedRef = useRef<((pill: HTMLElement) => boolean) | null>(null);
  const cancelHide = () => {
    if (hideTimer.current) {
      clearTimeout(hideTimer.current);
      hideTimer.current = null;
    }
  };
  const scheduleHide = () => {
    cancelHide();
    hideTimer.current = setTimeout(() => {
      hideTimer.current = null;
      const current = hoverRef.current;
      // Pinned cards (click / Enter) stay until Esc, an outside click or another badge.
      if (current && (current.via === "hover" || current.via === "focus")) setHover(null);
    }, POPOVER_HIDE_DELAY_MS);
  };

  const { refs, floatingStyles } = useFloating({
    open: !!hover,
    placement: "top",
    whileElementsMounted: autoUpdate,
    middleware: [offset(8), flip(), shift({ padding: 8 })],
    elements: { reference: hover?.el ?? null },
  });

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    const cancelOpen = () => {
      if (openTimer.current) {
        clearTimeout(openTimer.current);
        openTimer.current = null;
      }
    };
    const noHover = () => typeof window !== "undefined" && window.matchMedia?.("(hover: none)").matches === true;

    const onMouseOver = (e: MouseEvent) => {
      const target = (e.target as HTMLElement | null)?.closest(
        "[data-citation-kind]",
      ) as HTMLElement | null;
      if (!target || noHover()) return;
      cancelHide();
      const current = hoverRef.current;
      if (current?.el === target) return;
      // Pinned cards are replaced only by a click on another badge.
      if (current && current.via !== "hover" && current.via !== "focus") return;
      const next = stateFor(target, "hover");
      if (!next) return;
      cancelOpen();
      openTimer.current = setTimeout(() => {
        openTimer.current = null;
        if (target.isConnected) setHover(next);
      }, POPOVER_OPEN_DELAY_MS);
    };
    const onMouseOut = (e: MouseEvent) => {
      const target = (e.target as HTMLElement | null)?.closest(
        "[data-citation-kind]",
      );
      if (!target) return;
      const related = e.relatedTarget as Node | null;
      if (related && target.contains(related)) return;
      cancelOpen();
      // Give moving onto the popover itself a grace period.
      scheduleHide();
    };
    root.addEventListener("mouseover", onMouseOver);
    root.addEventListener("mouseout", onMouseOut);
    return () => {
      root.removeEventListener("mouseover", onMouseOver);
      root.removeEventListener("mouseout", onMouseOut);
      cancelHide();
      cancelOpen();
    };
    // Timers and the current card are read from refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rootRef]);

  // Chart thumbnail click → scroll into chart section
  // W2-5: mobile (no hover) tap on [data-citation-kind] also toggles the popover
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const isMobile = () =>
      typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(hover: none), (max-width: 1023px)").matches;

    const openTrustedCitationPanel = (pill: HTMLElement, via: "press" | "keyboard" = "press"): boolean => {
      if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null; }
      const kind = pill.dataset.citationKind;
      if (kind === "numbered") {
        const idxAttr = pill.dataset.sourceIdx;
        const sourceIdx = idxAttr ? parseInt(idxAttr, 10) : NaN;
        if (Number.isFinite(sourceIdx)) {
          const { sourceRef, quality } = sourceRefFromNumberedEntry(
            numberedSources[sourceIdx],
            sourceIdx,
          );
          if (isKbReportSource(sourceRef)) {
            // KB sources have no artifact byte exit, so SourcePanel can only show "不可用"; clicking instead expands the same KB overlay
            // (its opening action uses the knowledge base permission gate).
            track("bowhead_canvas_citation_total", { action: "open_kb_popover", kind: "numbered", quality });
            setHover({
              el: pill,
              kind: "numbered",
              sourceId: null,
              value: null,
              unit: null,
              sourceIdx,
              via,
            });
            return true;
          }
          setHover(null);
          track("bowhead_canvas_citation_total", {
            action: "open_panel",
            kind: "numbered",
            quality,
          });
          openPanel({
            sourceRef,
            sourceQuality: quality,
            sourceIndex: sourceIdx,
            display: pill.dataset.display ?? `[${sourceIdx + 1}]`,
            value: "",
            unit: "",
            highlight: "",
          }, pill);
        }
        return true;
      }

      if (kind !== "num") return false;
      const sourceId = pill.dataset.sourceId;
      if (sourceId) {
        setHover(null);
        track("bowhead_canvas_citation_total", {
          action: "open_panel",
          kind: "legacy_num",
          quality: "artifact_only",
        });
        openPanel({
          sourceId,
          sourceQuality: "artifact_only",
          value: pill.dataset.value ?? "",
          unit: pill.dataset.unit ?? "",
          display: pill.dataset.display ?? "",
          highlight: pill.dataset.highlight ?? "",
        }, pill);
        return true;
      }
      // Legacy no-source num is intentionally handled as a no-op: it is not a
      // trusted SourcePanel entry.
      track("bowhead_canvas_citation_total", {
        action: "legacy_no_source",
        kind: "legacy_num",
        quality: "never_linked",
      });
      return true;
    };

    openTrustedRef.current = (pill) => openTrustedCitationPanel(pill);
    const onClick = (e: Event) => {
      // 1. chart thumbnail → scroll
      const chartThumb = (e.target as HTMLElement | null)?.closest(
        ".citation-chart-thumbnail",
      ) as HTMLElement | null;
      if (chartThumb) {
        const sectionId = chartThumb.dataset.chartSectionId;
        if (sectionId) {
          const target2 = root.querySelector(
            `[data-section-id='${sectionId}']`,
          );
          if (target2) {
            target2.scrollIntoView({ behavior: "smooth", block: "center" });
          }
        }
        return;
      }
      // 1b. c2 (source-accessibility consumer half): resolved [cite:N] chip with a section anchor
      // (kind=chart_section citation) → scroll to that section, same semantics as a chart thumbnail;
      // v1 cite chips without anchors (data_ref) have only bh-tip hover, no click.
      const citePill = (e.target as HTMLElement | null)?.closest(
        "[data-citation-kind='cite'][data-cite-anchor]",
      ) as HTMLElement | null;
      if (citePill) {
        const anchorId = citePill.dataset.citeAnchor;
        if (anchorId) {
          // review F1: charts absorbed by insight_card render no top-level data-section-id,
          // only chart-block data-export-section-id; try both selectors,
          // or citation clicks targeting absorbed charts are inert.
          const target3 =
            root.querySelector(`[data-section-id='${anchorId}']`) ??
            root.querySelector(`[data-export-section-id='${anchorId}']`);
          if (target3) {
            target3.scrollIntoView({ behavior: "smooth", block: "center" });
          }
        }
        return;
      }
      // 2. Trusted citation click → SourcePanel.
      const pillForPanel = (e.target as HTMLElement | null)?.closest(
        "[data-citation-kind='num'], [data-citation-kind='numbered']",
      ) as HTMLElement | null;
      if (pillForPanel) {
        if (openTrustedCitationPanel(pillForPanel)) e.preventDefault();
        return;
      }

      // 3. (legacy) mobile tap on non-num citation (e.g. [ref:S]) → popover toggle
      if (!isMobile()) return;
      const pill = (e.target as HTMLElement | null)?.closest(
        "[data-citation-kind]",
      ) as HTMLElement | null;
      if (!pill) {
        // tap blank → close popover
        setHover(null);
        return;
      }
      const kind = pill.dataset.citationKind as
        | "num" | "ref" | "numbered" | undefined;
      if (kind !== "num" && kind !== "ref" && kind !== "numbered") return;
      const idxAttr = pill.dataset.sourceIdx;
      const sourceIdx = idxAttr ? parseInt(idxAttr, 10) : null;
      // toggle: this pill already shown → close; otherwise → open
      setHover((prev) =>
        prev?.el === pill
          ? null
          : {
              el: pill,
              kind,
              sourceId: pill.dataset.sourceId ?? null,
              value: pill.dataset.value ?? null,
              unit: pill.dataset.unit ?? null,
              sourceIdx: Number.isFinite(sourceIdx as number) ? (sourceIdx as number) : null,
              via: "press",
            },
      );
    };
    const onFocusIn = (e: FocusEvent) => {
      const pill = (e.target as HTMLElement | null)?.closest(
        "[data-citation-kind='num'], [data-citation-kind='numbered']",
      ) as HTMLElement | null;
      if (pill) focusedCitationRef.current = pill;
      // D13: keyboard focus on a badge opens its preview without moving focus (pinned cards stay).
      const badge = (e.target as HTMLElement | null)?.closest("[data-citation-kind]") as HTMLElement | null;
      if (!badge || isMobile()) return;
      const current = hoverRef.current;
      if (current?.el === badge || (current && current.via !== "hover" && current.via !== "focus")) return;
      const next = stateFor(badge, "focus");
      if (next) { cancelHide(); setHover(next); }
    };
    // Feishu 07.06 #14: clear ref when focus leaves the citation badge, or it lingers and later
    // body typing / Enter in the AI rewrite input mistakenly opens the previous citation "数据来源" dialog.
    const onFocusOut = (e: FocusEvent) => {
      focusedCitationRef.current = null;
      const related = e.relatedTarget as Node | null;
      if (related && popoverRef.current?.contains(related)) return;
      if (hoverRef.current?.via === "focus") scheduleHide();
    };
    // Tab from a previewed badge enters the card's buttons (the card is portaled outside the text).
    const onTabKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab" || e.shiftKey || e.defaultPrevented) return;
      const badge = (e.target as HTMLElement | null)?.closest("[data-citation-kind]") as HTMLElement | null;
      const current = hoverRef.current;
      if (!badge || current?.el !== badge) return;
      const first = popoverRef.current?.querySelector<HTMLElement>(FOCUSABLE);
      if (!first) return;
      e.preventDefault();
      cancelHide();
      first.focus();
    };
    root.addEventListener("keydown", onTabKey);
    // While editing text (body contenteditable / input / textarea), Enter edits;
    // citation dialogs must not intercept it.
    const isEditableTarget = (el: HTMLElement | null): boolean =>
      !!el && (
        el.isContentEditable ||
        el.tagName === "INPUT" ||
        el.tagName === "TEXTAREA" ||
        !!el.closest("input, textarea, [contenteditable='true']")
      );
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
      const pill = (e.target as HTMLElement | null)?.closest(
        "[data-citation-kind='num'], [data-citation-kind='numbered']",
      ) as HTMLElement | null;
      if (!pill) return;
      if (openTrustedCitationPanel(pill, "keyboard")) e.preventDefault();
    };
    const onDocumentKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
      // Feishu 07.06 #14: allow Enter in editing and require current focus actually on the citation badge;
      // remove focusedCitationRef fallback, which caused stale accidental triggers.
      if (isEditableTarget(e.target as HTMLElement | null)) return;
      if (isEditableTarget(document.activeElement as HTMLElement | null)) return;
      const selector = "[data-citation-kind='num'], [data-citation-kind='numbered']";
      const pill = (
        (e.target as HTMLElement | null)?.closest(selector) ??
        (document.activeElement as HTMLElement | null)?.closest(selector)
      ) as HTMLElement | null;
      if (!pill || !root.contains(pill)) return;
      if (openTrustedCitationPanel(pill, "keyboard")) e.preventDefault();
    };
    root.addEventListener("focusin", onFocusIn);
    root.addEventListener("focusout", onFocusOut);
    root.addEventListener("click", onClick);
    root.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("keydown", onDocumentKeyDown, true);
    return () => {
      root.removeEventListener("focusin", onFocusIn);
      root.removeEventListener("focusout", onFocusOut);
      root.removeEventListener("click", onClick);
      root.removeEventListener("keydown", onKeyDown, true);
      root.removeEventListener("keydown", onTabKey);
      document.removeEventListener("keydown", onDocumentKeyDown, true);
    };
    // Timers and the current card are read from refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rootRef, openPanel, numberedSources]);

  // Keyboard (Enter / Space) opening focuses the source card; Esc closes and returns to the badge.
  // Card lives in a portal mounted next render: record pending focus, then apply it in the card ref callback on mount.
  const focusOnMountRef = useRef(false);
  useEffect(() => {
    if (!hover || hover.via !== "keyboard" || narrow) return;
    if (popoverRef.current) popoverRef.current.focus({ preventScroll: true });
    else focusOnMountRef.current = true;
  }, [hover, narrow]);
  useEffect(() => {
    if (!hover) return;
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      const el = hover.el;
      const focusInside = popoverRef.current?.contains(document.activeElement) ?? false;
      setHover(null);
      if (hover.via === "keyboard" || hover.via === "press" || focusInside) el.focus({ preventScroll: true });
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [hover]);

  if (!hover) return null;

  // A numbered "Run Code" citation opens the source drawer, which shows the run result first and the
  // code below it (the drawer finds the run through the code block's viewer output aliases).
  const openNumberedRun = (current: HoverState) => {
    if (current.sourceIdx === null) return;
    const { sourceRef, quality } = sourceRefFromNumberedEntry(numberedSources[current.sourceIdx], current.sourceIdx);
    const pill = current.el;
    setHover(null);
    track("bowhead_canvas_citation_total", { action: "open_run", kind: "numbered", quality });
    openPanel({
      sourceRef,
      sourceQuality: quality,
      sourceIndex: current.sourceIdx,
      display: pill.dataset.display ?? `[${current.sourceIdx + 1}]`,
      value: "",
      unit: "",
      highlight: "",
    }, pill);
  };

  const renderPopoverContent = (current: HoverState) => {
    let ref: SourceRef | undefined;
    if (current.kind === "numbered" && current.sourceIdx !== null) {
      ref = sourceRefFromNumberedEntry(
        numberedSources[current.sourceIdx],
        current.sourceIdx,
      ).sourceRef;
    } else if (current.sourceId) {
      ref = sourceLookup.get(current.sourceId);
    }
    return ref && isKbReportSource(ref) ? (
      <KbChunkPopoverContent
        ref={ref}
        valueDisplay={current.kind === "num" && current.value ? formatValue(current.value, current.unit) : null}
        context={citeContextBefore(current.el)}
      />
    ) : (
      <CitationPopoverContent
        hover={current}
        ref={ref}
        callIdLookup={callIdLookup}
        rootRef={rootRef}
        onOpenRun={current.kind === "numbered" && current.sourceIdx !== null
          ? () => openNumberedRun(current)
          : undefined}
        onViewAll={current.kind !== "ref" ? () => { setHover(null); openTrustedRef.current?.(current.el); } : undefined}
      />
    );
  };

  if (narrow && hover.via !== "hover") {
    // Narrow-screen (<640) tap / keyboard source cards become bottom sheets (handle, scrim, swipe-down dismissal);
    // closing returns focus to the badge. Hover cards remain overlays (a hover modal would interrupt reading).
    return (
      <DialogShell
        onClose={() => setHover(null)}
        presentation="sheet"
        label={hover.el.textContent?.trim() || undefined}
        backdropClassName="settings-dialog-backdrop"
        panelClassName="citation-popover rc-prov rc-prov--sheet"
        returnFocusRef={{ current: hover.el }}
      >
        {renderPopoverContent(hover)}
      </DialogShell>
    );
  }

  return (
    <FloatingPortal>
      <div
        className="citation-popover rc-prov"
        ref={(el) => {
          refs.setFloating(el);
          popoverRef.current = el;
          if (el && focusOnMountRef.current) {
            focusOnMountRef.current = false;
            el.focus({ preventScroll: true });
          }
        }}
        style={{
          ...floatingStyles,
          // Body-mounted overlays use only layer tokens
          zIndex: "var(--z-menu)",
        }}
        role="dialog"
        aria-label={hover.el.textContent?.trim() || undefined}
        tabIndex={-1}
        onMouseEnter={cancelHide}
        onMouseLeave={scheduleHide}
        onBlur={(event) => {
          const related = event.relatedTarget as Node | null;
          if (related && (event.currentTarget.contains(related) || related === hover.el)) return;
          scheduleHide();
        }}
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const focusables = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(FOCUSABLE));
          if (!focusables.length) return;
          if (event.shiftKey && document.activeElement === focusables[0]) {
            event.preventDefault();
            hover.el.focus({ preventScroll: true });
          } else if (!event.shiftKey && document.activeElement === focusables[focusables.length - 1]) {
            // Continue after the badge in reading order.
            event.preventDefault();
            const order = Array.from(document.querySelectorAll<HTMLElement>(FOCUSABLE))
              .filter((element) => !event.currentTarget.contains(element));
            const next = order[order.indexOf(hover.el) + 1];
            setHover(null);
            (next ?? hover.el).focus({ preventScroll: true });
          }
        }}
      >
        {renderPopoverContent(hover)}
      </div>
    </FloatingPortal>
  );
}

function ProvHead({
  toolIcon,
  name,
  sub,
  state,
}: {
  toolIcon: React.ReactNode;
  name: string;
  sub?: string | null;
  state: "checking" | "linked_accessible" | "linked_unavailable";
}) {
  const t = useTranslations("provenance");
  return (
    <div className="rc-prov-head">
      <span className="rc-prov-tool" aria-hidden>{toolIcon}</span>
      <div className="legacy-rc-prov-main">
        <div className="t-name">{name}</div>
        {sub ? <div className="t-sub">{sub}</div> : null}
      </div>
      <span className={state === "linked_unavailable" ? "rc-prov-badge warn" : "rc-prov-badge"}>
        {state === "checking" ? t("source_checking") : state === "linked_accessible" ? t("source_linked") : t("source_unavailable")}
      </span>
    </div>
  );
}

function CitationPopoverContent({
  hover,
  ref,
  callIdLookup,
  rootRef,
  onOpenRun,
  onViewAll,
}: {
  hover: HoverState;
  ref: SourceRef | undefined;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  callIdLookup: Map<string, any>;
  rootRef: React.RefObject<HTMLElement | null>;
  onOpenRun?: () => void;
  /** “View all in source panel”: the same action as clicking the badge. */
  onViewAll?: () => void;
}) {
  const t = useTranslations("provenance");
  const access = useSourceAccess(ref);
  const { check } = useSourceAccessActions();
  const accessStatus = access?.generation === 0 ? "checking" : access?.status;
  useEffect(() => {
    if (!ref) return;
    void check(ref);
  }, [ref, check]);
  const valueDisplay =
    hover.kind === "num" && hover.value
      ? formatValue(hover.value, hover.unit)
      : null;

  if (!ref) {
    // Missing source / no source → warn card variant (design: amber affirm)
    return (
      <div>
        <ProvHead
          toolIcon={<AlertTriangle size={14} />}
          name={t("no_source")}
          state="linked_unavailable"
        />
        {valueDisplay && <div className="rc-prov-value">{valueDisplay}</div>}
        <div className={valueDisplay ? "rc-prov-affirm warn legacy-rc-prov-affirm--gap" : "rc-prov-affirm warn legacy-rc-prov-affirm--flush"}>
          {t("no_source_detail")}
        </div>
      </div>
    );
  }

  if (accessStatus === "checking") {
    return <ProvHead toolIcon={<DbIcon />} name={t("source_checking")} state="checking" />;
  }
  const accessibleRecord = accessStatus === "linked_accessible" && access ? access : null;
  if (!accessibleRecord) {
    return (
      <div>
        <ProvHead toolIcon={<AlertTriangle size={14} />} name={t("source_unavailable")} state="linked_unavailable" />
        {valueDisplay ? <div className="rc-prov-value">{valueDisplay}</div> : null}
        <div className="rc-prov-affirm warn">{t("source_link_definition")}</div>
      </div>
    );
  }
  return (
    <BiSourceCard
      ref_={accessibleRecord.source}
      preview={accessibleRecord.preview}
      valueDisplay={valueDisplay}
      sourceId={hover.sourceId}
      callIdLookup={callIdLookup}
      rootRef={rootRef}
      onOpenRun={onOpenRun}
      onViewAll={onViewAll}
    />
  );
}

/** Render only detail released by the current viewer authorization generation. */
export function BiSourceCard({
  ref_,
  preview = null,
  valueDisplay,
  sourceId,
  callIdLookup,
  rootRef,
  onOpenRun,
  onViewAll,
}: {
  ref_: SourceRef;
  preview?: FilePreview | null;
  valueDisplay: string | null;
  sourceId: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  callIdLookup: Map<string, any>;
  rootRef: React.RefObject<HTMLElement | null>;
  /** Opens the source drawer on the run result; offered only when this source is a run's output. */
  onOpenRun?: () => void;
  /** “View all in source panel” after the excerpt. */
  onViewAll?: () => void;
}) {
  const t = useTranslations("provenance");
  const tPopover = useTranslations("popover");
  const { sections } = useCanvasContext();
  const glimpse = useMemo(() => sourceGlimpse(sourceBlocksFor(ref_, preview)), [ref_, preview]);
  const run = useMemo(() => codeRunForSource(sections, ref_.source_alias ?? ref_.id), [sections, ref_.source_alias, ref_.id]);

  return (
    <div>
      <ProvHead
        toolIcon={<DbIcon />}
        name={ref_.safe_label || t("source_linked")}
        sub={distinctSub(ref_.safe_label, ref_.safe_query)}
        state="linked_accessible"
      />
      {valueDisplay && <div className="rc-prov-value">{valueDisplay}</div>}
      {glimpse?.kind === "table" ? (
        <ProvDataTable columns={glimpse.columns} rows={glimpse.rows} total={glimpse.total} />
      ) : glimpse?.kind === "text" ? (
        <div className="rc-prov-quote rc-prov-excerpt" data-testid="prov-excerpt">{glimpse.text}</div>
      ) : null}
      {/* The linkage caveat is said once per card (it used to repeat as the empty-data filler). */}
      <div className="rc-prov-affirm">{t("source_link_definition")}</div>
      {onViewAll ? (
        <button type="button" className="rc-prov-view-all" onClick={onViewAll}>
          {t("view_all_in_panel")}<ChevronRight size={14} aria-hidden />
        </button>
      ) : null}
      {/* Sprint ι (PRD §10.1.A): 2-hop popover footer-button row.
          P0-A keeps only same-turn code focus. Data focus is hidden until P1-C
          restores it with a non-404 workspace route. */}
      {run && onOpenRun ? (
        <div className="rc-prov-footer" data-testid="citation-popover-run-footer">
          <button type="button" onClick={onOpenRun}>
            {tPopover("view_run_result")}
          </button>
        </div>
      ) : (
        <CitationFooterButtons
          sourceId={sourceId}
          callIdLookup={callIdLookup}
          rootRef={rootRef}
        />
      )}
    </div>
  );
}

const GLIMPSE_ROWS = 5;
const GLIMPSE_COLUMNS = 6;
const GLIMPSE_TEXT_LINES = 6;
const GLIMPSE_TEXT_CHARS = 280;

type SourceGlimpse =
  | { kind: "table"; columns: string[]; rows: Record<string, string>[]; total: number }
  | { kind: "text"; text: string };

/** Hover cards show a glance of the source: the first table, else the first lines of text. */
export function sourceGlimpse(blocks: SourceBlock[]): SourceGlimpse | null {
  const table = blocks.find((block): block is Extract<SourceBlock, { kind: "table" }> => block.kind === "table");
  if (table) {
    const columns = table.table.columns.slice(0, GLIMPSE_COLUMNS);
    return {
      kind: "table",
      columns,
      rows: table.table.rows.slice(0, GLIMPSE_ROWS).map((row) => Object.fromEntries(
        columns.map((column) => [column, formatSourceCell(table.table, column, row[column] ?? "")]),
      )),
      total: table.table.totalRows ?? table.table.rows.length,
    };
  }
  const text = blocks.find((block): block is Exclude<SourceBlock, { kind: "table" }> => block.kind !== "table");
  if (!text) return null;
  const lines = text.text.split("\n").slice(0, GLIMPSE_TEXT_LINES).join("\n");
  const clipped = lines.length > GLIMPSE_TEXT_CHARS ? `${lines.slice(0, GLIMPSE_TEXT_CHARS)}…` : lines;
  return clipped.trim() ? { kind: "text", text: clipped } : null;
}

/** Source labels are usually "<tool>: <query>"; repeating the query as the sub line adds nothing. */
export function distinctSub(label: string | undefined, query: string | undefined): string | null {
  if (!query) return null;
  if (label && label.includes(query.trim())) return null;
  return query;
}

/** Inline original data table for a BI citation hover — a compact glimpse (≤5 rows,
 *  ≤6 cols). Full data lives in the SourcePanel (click) / Data tab. */
function ProvDataTable({
  columns,
  rows,
  total,
}: {
  columns: string[];
  rows: Record<string, string>[];
  total: number | null;
}) {
  const t = useTranslations("provenance");
  const shown = rows.length;
  return (
    <div className="rc-prov-data" data-testid="prov-data-table">
      <div className="rc-prov-data-scroll">
        <table>
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c}>{r[c] ?? ""}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {total != null && total > shown ? (
        <div className="rc-prov-data-more">{t("rows_shown", { shown, total })}</div>
      ) : null}
    </div>
  );
}

function DbIcon() {
  return (
    <LucideGlyph icon={LucideDatabase} size={13} />
  );
}

/** Sprint ι (PRD §10.1.A): 2-hop popover footer-button row.
 *  - View code: scrolls canvas to matching code_block (same-turn only).
 *  - View input data: hidden in P0-A; P1-C will restore a valid focus route.
 *  - Cross-turn entry or no entry (numbered citations carry only an opaque alias) → no footer at all. */
function CitationFooterButtons({
  sourceId,
  callIdLookup,
  rootRef,
}: {
  sourceId: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  callIdLookup: Map<string, any>;
  rootRef: React.RefObject<HTMLElement | null>;
}) {
  const t = useTranslations();
  const { currentTurnId } = useCanvasContext();
  const entry = sourceId ? callIdLookup.get(sourceId) : null;
  const isCrossTurn =
    !!entry && !!currentTurnId && !!entry.turnId && entry.turnId !== currentTurnId;
  const codeSectionId: string | undefined = !isCrossTurn ? entry?.sectionId : undefined;

  const onViewCode = () => {
    if (!codeSectionId) return;
    const root = rootRef.current;
    if (!root) return;
    const target = root.querySelector(
      `[data-section-id='${codeSectionId}']`,
    );
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  };
  // Never show a dead button: without a same-turn code block on this canvas there is nothing to open.
  if (!codeSectionId) return null;
  return (
    <div className="rc-prov-footer" data-testid="citation-popover-2hop-footer">
      <button type="button" onClick={onViewCode}>
        {t("popover.view_code")}
      </button>
    </div>
  );
}

/** Report knowledge-base citation overlay (design 08 · D, same content contract as chat source cards).
 *
 *  Backend determines states using the viewer knowledge-base read permission:
 *    · Accessible (KB location present) → current filename, location, original passage (freshly fetched under viewer permissions), "在知识库中打开";
 *    · Restricted → "来源受限 / 该来源仅对有权限的成员可见", backend omits filename and location;
 *    · Missing → "该来源已失效".
 *  Legacy shapes without KB location (historical fixture safe_preview) still show only backend-approved fields. */
export function KbChunkPopoverContent({
  ref,
  valueDisplay,
  context = "",
}: {
  ref: SourceRef;
  valueDisplay: string | null;
  /** Body text before the badge (up to 40 characters within the same text node), locating highlights in the original passage. */
  context?: string;
}) {
  const t = useTranslations("provenance");
  const tStep = useTranslations("step_list");
  const kb = kbRefOfSource(ref);
  const { state, location, warning } = useKbReportSource(kb);
  if (ref.access_state === "linked_restricted" || ref.access_state === "linked_missing") {
    const restricted = ref.access_state === "linked_restricted";
    return (
      <div data-testid="kb-cite-popover" data-kb-state={ref.access_state}>
        <div className="rc-prov-head">
          <span className="rc-prov-tool" aria-hidden><DocIcon /></span>
          <div className="legacy-rc-prov-main">
            <div className="t-name">{restricted ? tStep("cite_kb_restricted_tip") : tStep("cite_state_missing")}</div>
            {restricted ? <div className="t-sub">{tStep("cite_kb_restricted")}</div> : null}
          </div>
        </div>
        {valueDisplay && <div className="rc-prov-value">{valueDisplay}</div>}
      </div>
    );
  }
  if (kb) {
    const lines = state.status === "ready" ? quoteLines(state.content, context, 4) : [];
    return (
      <div data-testid="kb-cite-popover" data-kb-state="linked_accessible">
        <div className="rc-prov-head">
          <span className="rc-prov-tool" aria-hidden><DocIcon /></span>
          <div className="legacy-rc-prov-main">
            <div className="t-name">{ref.safe_label || t("tool_kb")}</div>
            <div className="t-sub rc-prov-kb-loc">{location}</div>
          </div>
        </div>
        {valueDisplay && <div className="rc-prov-value">{valueDisplay}</div>}
        {warning && <span role="note">{warning}</span>}
        {state.status === "ready" ? (
          <div className="rc-prov-quote rc-prov-kb-quote">
            {lines.map((line, i) => (
              <span key={i} className="rc-prov-kb-line">
                {line.before}
                {line.match ? <mark>{line.match}</mark> : null}
                {line.after}
              </span>
            ))}
          </div>
        ) : (
          <div className="rc-prov-quote rc-prov-quote-note">
            {state.status === "empty"
              ? tStep("cite_kb_updated")
              : state.status === "error"
                ? tStep("cite_kb_unavailable")
                : tStep("cite_kb_loading")}
          </div>
        )}
        <div className="rc-prov-kb-acts">
          <span>{t("kb_passage_affirm")}</span>
          <a href={kbCitationHref(kb)}>{tStep("cite_kb_open")}</a>
        </div>
      </div>
    );
  }
  const accessible = ref.access_state === "linked_accessible";
  return (
    <div data-testid="kb-cite-popover">
      <ProvHead
        toolIcon={<DocIcon />}
        name={accessible ? ref.safe_label || t("tool_kb") : t("source_unavailable")}
        sub={accessible ? distinctSub(ref.safe_label, ref.safe_query) : null}
        state={accessible ? "linked_accessible" : "linked_unavailable"}
      />
      {valueDisplay && <div className="rc-prov-value">{valueDisplay}</div>}
      {accessible && ref.safe_preview ? <div className="rc-prov-quote rc-prov-passage">{ref.safe_preview}</div> : null}
      <div className="rc-prov-affirm legacy-rc-prov-affirm--gap">{t("source_link_definition")}</div>
    </div>
  );
}

function DocIcon() {
  return (
    <LucideGlyph icon={LucideFile} size={13} />
  );
}


/** Up to 40 body characters before the badge in the same paragraph, locating KB passage highlights (same contract as chat `data-cite-context`). */
export function citeContextBefore(el: HTMLElement): string {
  let text = "";
  let node: Node | null = el.previousSibling;
  while (node && text.length < 40) {
    if (node instanceof HTMLElement && node.dataset.citationKind) break;
    text = (node.textContent ?? "") + text;
    node = node.previousSibling;
  }
  return text.slice(-40);
}

function formatValue(value: string, unit: string | null): string {
  const n = Number(value);
  if (isNaN(n)) return value;
  if (unit === "CNY") return formatCnyAmount(n);
  if (unit === "percent") {
    return `${(n * 100).toFixed(1)}%`;
  }
  if (unit === "int" || !unit) {
    return n.toLocaleString();
  }
  return `${n} ${unit}`;
}
