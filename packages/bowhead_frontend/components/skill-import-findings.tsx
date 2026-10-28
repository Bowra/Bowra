"use client";

/**
 * SkillImportFindings — package inventory display (shared by upload receipt/settings drawer).
 *
 * Match Claude Design (docs/sprints/skill-import-adaptation-2026-09-08, batch 3):
 * blockers first with distinct backgrounds, collapsed hints afterward. Must render inside `.sset` (styles/skills-settings.css).
 *
 * Server tech-design §3.3: severity has red/yellow only. **Red blocks activation, yellow does not**;
 * yellow starts collapsed and does not urge action. Truncated findings permit only at-least-N counts.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import type { SkillImportFinding, SkillImportReport } from "@/lib/api";

const MAX_RED_SHOWN = 3;
const MAX_YELLOW_SHOWN = 2;

/** Choose report wording by UI locale: server sends both, no frontend translation. */
function findingMessage(f: SkillImportFinding, locale: string): string {
  return locale.startsWith("zh") ? f.message_zh : f.message_en;
}

function whereLabel(f: SkillImportFinding): string {
  return f.line ? `${f.path} · L${f.line}` : f.path;
}

function FindingRow({ f, locale }: { f: SkillImportFinding; locale: string }) {
  const t = useTranslations("skill_import");
  return (
    <div className={`imp-row${f.severity === "red" ? " red" : ""}`}>
      <span className={`imp-sev ${f.severity === "red" ? "red" : "yel"}`}>
        {f.severity === "red" ? t("sev_red") : t("sev_yellow")}
      </span>
      <div className="imp-main">
        <div className="imp-msg">{findingMessage(f, locale)}</div>
        <div className="imp-where">{whereLabel(f)}</div>
      </div>
    </div>
  );
}

export function SkillImportFindings({
  report,
  locale,
}: {
  report: SkillImportReport;
  locale: string;
}) {
  const t = useTranslations("skill_import");
  const [expanded, setExpanded] = useState(false);
  // Collapse when changing skill (drawer reuses component instance), so next inventory does not start expanded.
  useEffect(() => setExpanded(false), [report]);
  const findings = report.findings ?? [];
  const reds = findings.filter((f) => f.severity === "red");
  const yellows = findings.filter((f) => f.severity !== "red");
  if (!findings.length) return null;
  // Collapse reds too: imported skill declaring a dozen nonexistent tools yields a dozen reds (server cap 200);
  // expanding all pushes dialog/drawer buttons offscreen (review P1-1). Show reds first, with more slots than yellow.
  const shownReds = expanded ? reds : reds.slice(0, MAX_RED_SHOWN);
  const shownYellows = expanded ? yellows : yellows.slice(0, MAX_YELLOW_SHOWN);
  const hidden = expanded
    ? 0
    : reds.length - shownReds.length + (yellows.length - shownYellows.length);
  return (
    <div className="imp-list">
      {shownReds.map((f) => (
        <FindingRow key={f.id} f={f} locale={locale} />
      ))}
      {shownYellows.map((f) => (
        <FindingRow key={f.id} f={f} locale={locale} />
      ))}
      {hidden ? (
        <button type="button" className="imp-more" onClick={() => setExpanded(true)}>
          {/* Truncated report cannot show all even when expanded; never promise that (re-review P2-4). */}
          {report.truncated ? t("more_findings_partial", { count: hidden }) : t("more_findings", { count: hidden })}
        </button>
      ) : null}
    </div>
  );
}

/** One-sentence conclusion. Separate red/hint counts; truncated reports say at least. */
export function skillImportSummary(
  report: SkillImportReport | null | undefined,
  t: ReturnType<typeof useTranslations<"skill_import">>,
): string {
  // No report means no conclusion; never claim “不影响使用” under “暂不生效” heading (re-review P3-8).
  if (!report) return "";
  const findings = report.findings ?? [];
  const counted = findings.filter((f) => f.severity === "red").length;
  // `red_count` is calculated **before truncation**; oversized findings are a subset, array counting underreports (review P1-2:
  // red_count=40 with 3 array items cannot mean at least 3; extreme truncation empties array,
  // leaving pending skill's “有阻断项” heading followed by “不影响使用”).
  const reds = Math.max(report.red_count ?? 0, counted);
  const yellows = findings.length - counted;
  const atLeast = Boolean(report.truncated) || reds > counted;
  if (!reds)
    return atLeast
      ? t("summary_hints_at_least", { hints: yellows })
      : t("summary_hints", { hints: yellows });
  // **Omit yellow count** when truncated: server orders reds first and cuts tail; once reds truncate, all yellows are already gone,
  // so claiming another 0 hints asserts something removed from report (re-review P2-3).
  return atLeast
    ? t("summary_blocking_at_least", { blocking: reds })
    : t("summary_blocking", { blocking: reds, hints: yellows });
}
