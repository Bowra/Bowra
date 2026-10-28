/**
 * SourceLinkageStrip — report source-linkage facts, not a numerical trust score.
 *
 * Legacy section statistics remain unchanged, but their user-facing projection is
 * three independent facts: citation occurrences, numbers to check, and sources.
 */

"use client";

import {
  Fragment,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslations } from "next-intl";
import { FileText, Info, Link } from "@/components/icons";
import type { ReportSection } from "@/lib/types";
import { computeReportTrust, scrollToFirstUnverified } from "@/lib/report-trust";
import { projectSourceLinkage } from "@/lib/source-linkage-presentation";

function SourceLinkageInfo() {
  const t = useTranslations("trust_strip");
  const noteId = `source-linkage-info-${useId()}`;
  const rootRef = useRef<HTMLSpanElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const pointerInteraction = useRef(false);
  const restoringFocus = useRef(false);
  const [transientOpen, setTransientOpen] = useState(false);
  const [pinnedOpen, setPinnedOpen] = useState(false);
  const open = transientOpen || pinnedOpen;

  const closeAndRestoreFocus = useCallback(() => {
    setTransientOpen(false);
    setPinnedOpen(false);
    restoringFocus.current = true;
    triggerRef.current?.focus();
    restoringFocus.current = false;
  }, []);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current?.contains(event.target as Node)) return;
      setTransientOpen(false);
      setPinnedOpen(false);
      window.setTimeout(() => {
        restoringFocus.current = true;
        triggerRef.current?.focus();
        restoringFocus.current = false;
      }, 0);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeAndRestoreFocus();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [closeAndRestoreFocus, open]);

  return (
    <span
      ref={rootRef}
      className="rc-source-linkage-info"
      onPointerEnter={() => setTransientOpen(true)}
      onPointerLeave={() => setTransientOpen(false)}
      onBlur={(event) => {
        if (event.relatedTarget instanceof Node && rootRef.current?.contains(event.relatedTarget)) return;
        setTransientOpen(false);
        setPinnedOpen(false);
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        className="rc-source-linkage-info-trigger"
        aria-label={t("info_short")}
        aria-expanded={open}
        aria-controls={noteId}
        onPointerDown={() => {
          pointerInteraction.current = true;
        }}
        onPointerUp={() => {
          pointerInteraction.current = false;
        }}
        onPointerCancel={() => {
          pointerInteraction.current = false;
        }}
        onFocus={() => {
          if (!pointerInteraction.current && !restoringFocus.current) setPinnedOpen(true);
        }}
        onClick={() => {
          pointerInteraction.current = false;
          setTransientOpen(false);
          setPinnedOpen((value) => !value);
        }}
      >
        <Info size={13} aria-hidden />
      </button>
      {open ? (
        <span id={noteId} role="note" aria-label={t("info_short")} className="rc-source-linkage-info-note">
          {t("info_full")}
        </span>
      ) : null}
    </span>
  );
}

function renderTokens(tokens: ReactNode[]) {
  return tokens.map((token, index) => (
    <Fragment key={index}>
      {index > 0 ? <span className="sep" aria-hidden /> : null}
      {token}
    </Fragment>
  ));
}

export function SourceLinkageStrip({
  sections,
  isStreaming = false,
}: {
  sections: ReportSection[];
  isStreaming?: boolean;
}) {
  const t = useTranslations("trust_strip");
  const stats = useMemo(() => computeReportTrust(sections), [sections]);
  const presentation = useMemo(
    () => projectSourceLinkage({
      verified: stats.verified,
      unverified: stats.unverified,
      sources: stats.sources,
    }),
    [stats],
  );

  if (!isStreaming && !presentation.visible) return null;

  const tokens: ReactNode[] = [];
  const citationCount = presentation.citationCount ?? 0;
  const figureReviewCount = presentation.figureReviewCount ?? 0;
  const sourceCount = presentation.sourceCount ?? 0;
  if (isStreaming) {
    tokens.push(
      <span className="rc-trust-item" key="collecting">
        <span className="rc-spinner legacy-rc-spinner--sm" aria-hidden />
        <span>{t("collecting")}</span>
      </span>,
    );
  }
  if (citationCount > 0) {
    tokens.push(
      <span className="rc-trust-item citation-linkage" key="citations">
        {!isStreaming ? <Link size={12} aria-hidden /> : null}
        <span>{t("citation_count", { count: citationCount })}</span>
      </span>,
    );
  }
  if (figureReviewCount > 0) {
    tokens.push(
      <button
        type="button"
        className="rc-trust-item figure-review"
        onClick={() => scrollToFirstUnverified()}
        title={t("review_jump")}
        aria-label={t("review_jump")}
        key="review"
      >
        {t("figure_review_count", { count: figureReviewCount })}
      </button>,
    );
  }
  if (!isStreaming && sourceCount > 0) {
    tokens.push(
      <span className="rc-trust-item source-count" key="sources">
        <FileText size={12} aria-hidden />
        <span>{t("source_count", { count: sourceCount })}</span>
      </span>,
    );
  }

  return (
    <div className="rc-trust" data-testid="source-linkage-strip" role="group">
      {renderTokens(tokens)}
      <span className="sep" aria-hidden />
      <SourceLinkageInfo />
    </div>
  );
}
