"use client";

/**
 * SkillCoachDraftPanel — personal-skill coach revision review panel (skill-import batch 4).
 *
 * Design docs/sprints/skill-import-adaptation-2026-09-08 (Claude Design, screens D/E).
 * Shares two things with team-side `FeedbackPrefillPanel`: `lineDiff` line-by-line comparison and requiring review checkbox before applying
 * as protection. **Does not share that component**: its banner requires proposer/co-sign count/candidate id, absent here;
 * revision comes from the user's uploaded package.
 *
 * Apply copies revised body into editor draft; saving still uses existing `PUT /skills/{name}/body`,
 * with this flow's inventory hook recalculating state. Panel itself neither saves nor changes state.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { lineDiff } from "@/lib/line-diff";
import { bodySha256, degradeMessageKey, isDegraded, type SkillCoachDraft } from "@/lib/skill-coach-api";
import type { SkillImportFinding } from "@/lib/api";

export function SkillCoachDraftPanel({
  draft,
  baseline,
  findings,
  locale,
  onApply,
  onDiscard,
}: {
  draft: SkillCoachDraft;
  /** Current on-disk SKILL.md (including frontmatter), the exact coach revision baseline. */
  baseline: string;
  /** Current inventory findings link each change to a finding (id matching, never text matching). */
  findings: SkillImportFinding[];
  locale: string;
  onApply: (edited: string) => void;
  onDiscard: () => void;
}) {
  const t = useTranslations("skill_coach");
  const rootRef = useRef<HTMLElement | null>(null);
  // Start button lives in drawer's top status area; coach takes 1–2 minutes (116: 142s). Without scrolling here,
  // returning user sees no screen change, while draft lies below fold.
  useEffect(() => {
    const el = rootRef.current;
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest" });
  }, [draft]);
  const [reviewed, setReviewed] = useState(false);
  // New draft **or changed baseline** invalidates review: reset checkbox.
  // Tracking only draft restores old check after manual editing/saving, while diff now has red lines deleting newly written content.
  useEffect(() => setReviewed(false), [draft, baseline]);

  // Has drafting baseline changed (including another tab/device)? Save API is last-write-wins;
  // applying stale draft silently overwrites others' changes. Compare fingerprints, fail open when unknown (warning only).
  const [stale, setStale] = useState(false);
  useEffect(() => {
    let alive = true;
    const expected = draft.base?.body_sha256;
    if (!expected) {
      setStale(false);
      return () => {
        alive = false;
      };
    }
    void bodySha256(baseline).then((actual) => {
      if (alive) setStale(actual !== expected);
    });
    return () => {
      alive = false;
    };
  }, [draft, baseline]);

  const banner = draft.banner?.text || t("banner");
  const edited = isDegraded(draft) ? "" : draft.prefill.edited_content;
  const diff = useMemo(() => (edited ? lineDiff(baseline, edited) : []), [baseline, edited]);

  if (isDegraded(draft)) {
    return (
      <section ref={rootRef} className="coach-panel" data-testid="skill-coach-degraded" aria-labelledby="skill-coach-title">
        <div className="coach-banner" role="note">
          <div id="skill-coach-title" className="coach-banner-t">
            {t("degraded_title")}
          </div>
          <div className="coach-banner-d">{t(degradeMessageKey(draft.degrade_reason))}</div>
          {draft.rationale ? <p className="coach-notes-b">{draft.rationale}</p> : null}
          {draft.degrade_reason === "invented_tool" && draft.invented?.length ? (
            <ul className="coach-invented">
              {draft.invented.map((n) => (
                <li key={n}>
                  <code>{n}</code>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        <div className="coach-actions">
          <button type="button" className="btn sec sm" onClick={onDiscard}>
            {t("close")}
          </button>
        </div>
      </section>
    );
  }

  const addressed = findings.filter((f) => draft.report.findings_addressed.includes(f.id));
  const changed = diff.some((l) => l.kind !== "ctx");

  return (
    <section ref={rootRef} className="coach-panel" data-testid="skill-coach-panel" aria-labelledby="skill-coach-title">
      <div className="coach-banner" role="note">
        <div id="skill-coach-title" className="coach-banner-t">
          {banner}
        </div>
        <div className="coach-banner-d">{t("banner_sub")}</div>
      </div>

      <div className="coach-difthead">
        <span className="coach-difttitle">{t("diff_title")}</span>
        <span className="coach-diftlegend">{changed ? t("diff_legend") : t("diff_no_change")}</span>
      </div>
      <pre className="coach-diff" data-testid="skill-coach-diff">
        {diff.map((line, i) => (
          <div key={i} data-kind={line.kind} className={`coach-dl ${line.kind}`}>
            <span aria-hidden className="g">
              {line.kind === "del" ? "−" : line.kind === "add" ? "+" : ""}
            </span>
            <span className="c">{line.text}</span>
          </div>
        ))}
      </pre>

      {stale ? (
        <div className="coach-stale" role="status" data-testid="skill-coach-stale">
          {t("stale")}
        </div>
      ) : null}

      {addressed.length ? (
        <div className="coach-addressed">
          <div className="coach-sect-t">{t("addressed_title")}</div>
          {draft.report.truncated ? <div className="coach-hint">{t("addressed_truncated")}</div> : null}
          {addressed.map((f) => (
            <div key={f.id} className="coach-addr-row">
              <span className={`imp-sev ${f.severity === "red" ? "red" : "yel"}`}>
                {f.severity === "red" ? t("sev_red") : t("sev_yellow")}
              </span>
              <span className="coach-addr-msg">{locale.startsWith("zh") ? f.message_zh : f.message_en}</span>
            </div>
          ))}
        </div>
      ) : null}

      {draft.prefill.rationale ? (
        <div className="coach-notes">
          <div className="coach-sect-t">{t("notes_title")}</div>
          {/* Coach explanation has three parts (what changed / what did not and why / what user still needs to do), in server prefill.rationale,
              not report.notes (previous version copied a documented field server never sends, making this whole section dead code). */}
          <p className="coach-notes-b">{draft.prefill.rationale}</p>
        </div>
      ) : null}

      {draft.base?.import_status === "overridden" ? (
        <div className="coach-warn" role="note" data-testid="skill-coach-overridden-warn">
          {/* Charter alone cannot rely on model including this in rationale: omission would leave user uninformed.
              Saving always reruns inventory; remaining red findings revert overridden to pending, disabling a previously active skill. */}
          {t("overridden_warn")}
        </div>
      ) : null}
      <label className="coach-reviewed">
        <input type="checkbox" checked={reviewed} disabled={stale} onChange={(e) => setReviewed(e.target.checked)} />
        <span>{t("reviewed")}</span>
      </label>
      <div className="coach-actions">
        <button type="button" className="btn pri sm" disabled={!reviewed || !changed || stale} onClick={() => onApply(edited)}>
          {t("apply")}
        </button>
        <button type="button" className="btn ghost sm" onClick={onDiscard}>
          {t("discard")}
        </button>
      </div>
      <p className="coach-hint">{t("apply_hint")}</p>
    </section>
  );
}
