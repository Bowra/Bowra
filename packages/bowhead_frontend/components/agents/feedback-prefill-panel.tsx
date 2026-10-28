"use client";

// agent-feedback-loop FE batch B—prefill-review panel in editor (design trio ③).
// Contract (tech-design §2.3): ① show prefill verbatim as a line-by-line diff, never a summary; **cannot apply directly** upon opening,
// owner must explicitly check “已逐行审阅”; ② fixed banner “此内容由成员反馈起草，来源不可信，保存前逐行审阅”
// + proposer/cosignature count. Only hand edited body to editor draft—save/evaluate/release retain the original pipeline and all gates.

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import type { FeedbackPrefillHandoff } from "@/lib/feedback-candidates-api";
import { lineDiff } from "@/lib/line-diff";

export function FeedbackPrefillPanel({
  handoff,
  baseline,
  stale = false,
  realigning = false,
  onApply,
  onDiscard,
  onRealign,
}: {
  handoff: FeedbackPrefillHandoff;
  baseline: string;
  /** Handoff's draft baseline has changed (§2.3 ⑤): read-only until realigned against the new baseline. */
  stale?: boolean;
  realigning?: boolean;
  onApply: (edited: string) => void;
  onDiscard: () => void;
  onRealign?: () => void;
}) {
  const t = useTranslations("feedback_prefill");
  const [reviewed, setReviewed] = useState(false);
  // Changed draft (realignment/another candidate) = unreviewed: reset the checkbox; never apply new content using the old review check.
  useEffect(() => setReviewed(false), [handoff]);
  const edited = handoff.prefill.kind === "agent_system_prompt" || handoff.prefill.kind === "team_skill_body" ? handoff.prefill.edited_content : "";
  const diff = useMemo(() => lineDiff(baseline, edited), [baseline, edited]);
  const changed = diff.some((line) => line.kind !== "ctx");

  return (
    <section data-testid="feedback-prefill-panel" aria-labelledby="feedback-prefill-title" style={{ display: "flex", flexDirection: "column", gap: 12, marginBottom: 18 }}>
      <div role="note" style={{ background: "var(--c-warn-soft)", border: "1px solid #e3d3a6", borderRadius: 8, padding: "12px 14px" }}>
        <div id="feedback-prefill-title" style={{ font: "600 13px var(--sans, inherit)", color: "#7d5f14" }}>{t("banner")}</div>
        <div style={{ fontSize: "0.75rem", color: "#8a6f2a", marginTop: 3 }}>
          {t("banner_meta", { proposer: handoff.banner.proposer_display || handoff.banner.proposer_id, count: handoff.banner.evidence_count, candidate: handoff.candidate_id })}
        </div>
      </div>
      {stale ? (
        <div role="status" data-testid="feedback-prefill-stale" style={{ background: "var(--c-info-soft)", border: "1px solid #c6dbe6", borderRadius: 8, padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ font: "600 13px var(--sans, inherit)", color: "var(--c-info)" }}>{t("stale_title")}</div>
          <div style={{ fontSize: "0.75rem", color: "#4a6d84", lineHeight: 1.6 }}>{t("stale_body")}</div>
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="primary" disabled={realigning || !onRealign} onClick={() => onRealign?.()}>{realigning ? t("realigning") : t("realign")}</Button>
            <Button size="sm" variant="secondary" disabled={realigning} onClick={onDiscard}>{t("discard")}</Button>
          </div>
        </div>
      ) : null}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", opacity: stale ? 0.55 : 1 }}>
        <span style={{ fontSize: "0.6875rem", fontWeight: 600, letterSpacing: ".06em", textTransform: "uppercase", color: "var(--c-ink-4)" }}>{t("diff_title")}</span>
        <span style={{ fontSize: "0.75rem", color: "var(--c-ink-4)" }}>{changed ? t("diff_legend") : t("diff_no_change")}</span>
      </div>
      <pre
        data-testid="feedback-prefill-diff"
        style={{ margin: 0, border: "1px solid var(--c-line)", borderRadius: 8, background: "var(--c-surface)", padding: "6px 0", maxHeight: 360, overflow: "auto", font: "400 12.5px/1.8 ui-monospace, SFMono-Regular, Menlo, monospace" }}
      >
        {diff.map((line, i) => (
          <div
            key={i}
            data-kind={line.kind}
            style={{
              display: "flex", gap: 10, padding: "0 12px", whiteSpace: "pre-wrap",
              background: line.kind === "del" ? "var(--c-err-soft)" : line.kind === "add" ? "var(--c-pos-soft)" : "transparent",
              color: line.kind === "del" ? "var(--c-err)" : line.kind === "add" ? "var(--c-pos)" : "var(--c-ink-3)",
              textDecoration: line.kind === "del" ? "line-through" : "none",
            }}
          >
            <span aria-hidden style={{ width: 12, flex: "none", opacity: 0.7 }}>{line.kind === "del" ? "−" : line.kind === "add" ? "+" : ""}</span>
            <span>{line.text}</span>
          </div>
        ))}
      </pre>
      {(handoff.prefill.kind === "agent_system_prompt" || handoff.prefill.kind === "team_skill_body") && handoff.prefill.rationale ? (
        <div style={{ fontSize: "0.78125rem", color: "var(--c-ink-3)" }}>{t("rationale", { text: handoff.prefill.rationale })}</div>
      ) : null}
      <label style={{ display: "flex", gap: 9, alignItems: "flex-start", fontSize: "0.8125rem", color: "var(--c-ink-2)" }}>
        <input type="checkbox" checked={reviewed} disabled={stale} onChange={(e) => setReviewed(e.target.checked)} style={{ marginTop: 3 }} />
        <span>{t("reviewed")}</span>
      </label>
      <div style={{ display: "flex", gap: 10 }}>
        <Button size="sm" variant="primary" disabled={!reviewed || stale} onClick={() => onApply(edited)}>{t("apply")}</Button>
        <Button size="sm" variant="ghost" disabled={realigning} onClick={onDiscard}>{t("discard")}</Button>
      </div>
      <p style={{ margin: 0, fontSize: "0.75rem", color: "var(--c-ink-4)" }}>{t("apply_hint")}</p>
    </section>
  );
}
