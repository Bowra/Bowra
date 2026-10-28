"use client";

import {
  FloatingFocusManager,
  useDismiss,
  useFloating,
  useInteractions,
} from "@floating-ui/react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";

import {
  Bolt,
  Database,
  Link,
  Maximize,
  Minimize,
  RefreshCw,
  Spark,
  X,
} from "@/components/icons";
import { cycleTabWithin, focusFirstWithin } from "@/components/ui/dialog";
import { useArtifactHref } from "@/lib/canvas-pm-adapter/canvas-context";
import { projectEmbeddedDataProvenance } from "@/lib/embedded-data-provenance";
import type { ReportSection } from "@/lib/types";
import { useMediaQuery } from "@/lib/use-media-query";

import { SafeHtmlFrame, useSafeHtmlDocument } from "../safe-html-preview";
import {
  TriangleAlert as LucideTriangleAlert,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

/**
 * Render model-produced Report HTML as a static, network-free document.
 *
 * `data` and `sources` remain generation-input provenance. They are never
 * injected into the iframe, and artifact JavaScript is never executed.
 */

type HtmlArtifact = Extract<ReportSection, { kind: "html_artifact" }>;

function WarnGlyph({ size = 9 }: { size?: number }) {
  return (
    <LucideGlyph icon={LucideTriangleAlert} size={size} aria-hidden />
  );
}

/**
 * Mark <html> when fullscreen is open. Card becomes fixed in place (moving to body reloads iframe);
 * styles temporarily remove ancestor stacking contexts (.v2-ws-right-body / .canvas-report-shell
 * isolation etc.; see styles/report-canvas.css and design section "图层") so fullscreen covers sidebar/topbar.
 */
export const EMBED_FULLSCREEN_ATTR = "data-rc-embed-fullscreen";
let fullscreenCount = 0;

function useFullscreenMarker(active: boolean) {
  useLayoutEffect(() => {
    if (!active) return;
    const root = document.documentElement;
    fullscreenCount += 1;
    root.setAttribute(EMBED_FULLSCREEN_ATTR, "");
    return () => {
      fullscreenCount = Math.max(0, fullscreenCount - 1);
      if (fullscreenCount === 0) root.removeAttribute(EMBED_FULLSCREEN_ATTR);
    };
  }, [active]);
}

export function HtmlArtifactSection({ data }: { data: HtmlArtifact }) {
  const t = useTranslations("html_artifact");
  const [expanded, setExpanded] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const isMobile = useMediaQuery("(max-width: 640px)");
  const provenance = projectEmbeddedDataProvenance(data);
  const rawHref = useArtifactHref(data.artifact_id);
  const safeHtml = useSafeHtmlDocument({
    enabled: true,
    href: rawHref,
    reloadToken,
  });

  // Fullscreen is an in-place modal: style the same card node without a new parent (which would remount/reload iframe).
  // Match DialogShell: Esc closes, focus enters, Tab stays inside, background inert, closing returns to expand button.
  const toggleRef = useRef<HTMLButtonElement | null>(null);
  const scrimRef = useRef<HTMLDivElement | null>(null);
  const [card, setCard] = useState<HTMLDivElement | null>(null);
  const { refs, context } = useFloating({
    open: expanded,
    onOpenChange: (open) => {
      if (!open) setExpanded(false);
    },
  });
  const dismiss = useDismiss(context, {
    enabled: expanded,
    outsidePress: false,
    bubbles: { escapeKey: false },
  });
  const { getFloatingProps } = useInteractions([dismiss]);
  const setCardRef = useCallback(
    (node: HTMLDivElement | null) => {
      refs.setFloating(node);
      setCard(node);
    },
    [refs],
  );
  // Scrim is the card's sibling; it must not become inert, or clicks cannot close it.
  const getInsideElements = useCallback(() => (scrimRef.current ? [scrimRef.current] : []), []);
  useFullscreenMarker(expanded);
  useEffect(() => {
    if (!expanded || !card) return;
    // Chromium button clicks keep focus on button (already inside); WebKit clicks do not focus, so focus the card instead.
    if (!card.contains(document.activeElement)) card.focus({ preventScroll: true });
  }, [expanded, card]);
  const onCardKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!expanded || event.key !== "Tab" || !card) return;
    cycleTabWithin(card, event);
  };

  const bodyHeight = expanded ? "100%" : data.height ?? 480;
  const sources = provenance.sourceIndexes.map((index) => `[${index}]`).join(" ");

  const frame = (
    <div
      key="card"
      ref={setCardRef}
      className={`rc-embed${expanded ? " rc-embed--expanded" : ""}`}
      {...getFloatingProps({ onKeyDown: onCardKeyDown })}
      role={expanded ? "dialog" : undefined}
      aria-modal={expanded ? true : undefined}
      aria-label={expanded ? data.title : undefined}
      tabIndex={expanded ? -1 : undefined}
    >
      <div className="rc-embed-head">
        <Bolt size={14} className="rc-embed-head-icon" />
        <span className="rc-embed-title">{data.title}</span>
        <span className="rc-badge ai">
          <Spark size={10} />
          {t("safe_preview_badge")}
        </span>
        {provenance.status === "computed_unchecked" ? (
          <span className="rc-badge amber">
            <WarnGlyph size={9} /> {t("computed_badge")}
          </span>
        ) : (
          <span className="rc-badge neutral">
            <Database size={9} /> {t("injected_badge")}
          </span>
        )}
        <span className="rc-embed-spacer" />
        <button
          type="button"
          className="rc-action-btn"
          onClick={() => setReloadToken((value) => value + 1)}
          title={t("reload")}
          aria-label={t("reload")}
        >
          <RefreshCw size={13} />
        </button>
        <a
          className="rc-action-btn"
          href={rawHref}
          target="_blank"
          rel="noreferrer"
          title={t("open_raw")}
          aria-label={t("open_raw")}
        >
          <Link size={13} />
        </a>
        <button
          ref={toggleRef}
          type="button"
          className="rc-action-btn"
          onClick={() => setExpanded((value) => !value)}
          title={expanded ? t("collapse") : t("expand")}
          aria-label={expanded ? t("collapse") : t("expand")}
        >
          {expanded ? <Minimize size={14} /> : <Maximize size={14} />}
        </button>
        {expanded ? (
          <button
            type="button"
            className="rc-action-btn"
            onClick={() => setExpanded(false)}
            title={t("collapse")}
            aria-label={t("collapse")}
          >
            <X size={14} />
          </button>
        ) : null}
      </div>

      {isMobile && !expanded ? (
        <div className="rc-embed-mobile-cta">
          <p>{t("mobile_safe_hint")}</p>
          <button type="button" className="rc-pill-btn accept" onClick={() => setExpanded(true)}>
            <Maximize size={14} />
            {t("open_safe_preview")}
          </button>
        </div>
      ) : safeHtml.error ? (
        <div className="rc-embed-error">{t(`safe_${safeHtml.error}`)}</div>
      ) : safeHtml.loading || safeHtml.document === null ? (
        <div className="rc-embed-loading" style={{ height: expanded ? undefined : bodyHeight }}>
          {t("loading")}
        </div>
      ) : (
        <SafeHtmlFrame
          document={safeHtml.document}
          title={data.title}
          className="rc-embed-frame"
          height={bodyHeight}
        />
      )}

      <div className="rc-embed-foot">
        <span>{t("footer_safe_static")}</span>
        <span className="rc-embed-foot-right">
          {provenance.status === "injected"
            ? provenance.hasSources
              ? t("footer_injected_sources", { sources })
              : t("footer_injected_no_sources")
            : provenance.hasSources
              ? t("footer_computed_sources", { sources })
              : t("footer_computed_no_sources")}
        </span>
      </div>
      {expanded ? (
        // Tab inside iframe never sends keyboard events here; browser moves focus directly after iframe:
        // catch it with this sentinel and return to the first control inside. aria-hidden excludes it from the cycling list.
        <span
          tabIndex={0}
          aria-hidden="true"
          className="rc-embed-focus-sentinel"
          onFocus={() => {
            if (card) focusFirstWithin(card);
          }}
        />
      ) : null}
    </div>
  );

  return (
    <>
      {expanded ? (
        <div
          key="scrim"
          ref={scrimRef}
          className="rc-embed-overlay"
          aria-hidden
          onClick={() => setExpanded(false)}
        />
      ) : null}
      {expanded ? (
        <div key="placeholder" className="rc-embed rc-embed--placeholder" aria-hidden />
      ) : null}
      {/* Always retain the same FloatingFocusManager wrapper (disabled when collapsed), preserving the card's tree position. */}
      <FloatingFocusManager
        context={context}
        disabled={!expanded}
        modal
        guards={false}
        outsideElementsInert
        initialFocus={-1}
        returnFocus={toggleRef}
        getInsideElements={getInsideElements}
      >
        {frame}
      </FloatingFocusManager>
    </>
  );
}
