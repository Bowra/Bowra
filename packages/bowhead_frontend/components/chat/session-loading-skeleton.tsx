"use client";

/**
 * session-loading-skeleton — placeholder during session hydration.
 *
 * chat-ux-2026-08 batch E. Replace page.tsx's 740px single-column skeleton to fix two issues:
 *
 * 1. **Wrong shape → layout jumps when content arrives.** The old skeleton was full-width single-column,
 *    while actual content splits into chat and report canvas. On 116, DOM was ready at 1.27s, but first chat content
 *    appeared at 7.1s for a small session and ≈19s for a large one: users spent more than ten seconds
 *    looking at a wrongly shaped placeholder before it abruptly split into columns.
 * 2. **No meaning.** Gray bars did not say what users were waiting for. Add a sentence after 8 seconds
 *    so ongoing work can be distinguished from a stall.
 *
 * The skeleton deliberately previews real content: two **short right-aligned bars** atop the left column are user questions,
 * followed by a rounded running disclosure band and body; the right column has report title, body, and chart placeholders.
 */
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

/** Add a second sentence after this duration. Use 8s: small sessions on 116 rendered their first frame at 7.1s;
 *  normal sessions should not show the second sentence, so seeing it indicates a genuinely heavy session. */
const SLOW_AFTER_MS = 8000;

export function SessionLoadingSkeleton() {
  const t = useTranslations("chat");
  const [slow, setSlow] = useState(false);

  useEffect(() => {
    const id = setTimeout(() => setSlow(true), SLOW_AFTER_MS);
    return () => clearTimeout(id);
  }, []);

  return (
    <div className="v2-session-skel" aria-busy="true" data-testid="session-loading">
      <div className="v2-session-skel-pane v2-session-skel-chat">
        <div
          className="v2-session-skel-label"
          data-testid="session-loading-label"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <span className="v2-session-skel-spin" aria-hidden />
          {slow ? t("loading_session_slow") : t("loading_session")}
        </div>
        {/* User question: short right-aligned bars. */}
        <div className="v2-section-skeleton" style={{ height: 15, width: "62%", margin: "0 0 8px auto" }} />
        <div className="v2-section-skeleton" style={{ height: 15, width: "44%", margin: "0 0 20px auto" }} />
        {/* Running disclosure band. */}
        <div className="v2-section-skeleton" style={{ height: 38, width: "100%", borderRadius: 10, margin: "0 0 16px" }} />
        {/* Body. */}
        <div className="v2-section-skeleton" style={{ height: 13, width: "96%", margin: "0 0 9px" }} />
        <div className="v2-section-skeleton" style={{ height: 13, width: "88%", margin: "0 0 9px" }} />
        <div className="v2-section-skeleton" style={{ height: 13, width: "72%", margin: "0 0 9px" }} />
      </div>
      <div className="v2-session-skel-pane v2-session-skel-canvas">
        <div className="v2-section-skeleton" style={{ height: 11, width: "30%", margin: "0 0 12px" }} />
        <div className="v2-section-skeleton v2-skeleton-h1" />
        <div className="v2-section-skeleton" style={{ height: 13, width: "94%", margin: "0 0 9px" }} />
        <div className="v2-section-skeleton" style={{ height: 13, width: "90%", margin: "0 0 20px" }} />
        <div className="v2-section-skeleton" style={{ height: 90, width: "100%", borderRadius: 9 }} />
      </div>
    </div>
  );
}
