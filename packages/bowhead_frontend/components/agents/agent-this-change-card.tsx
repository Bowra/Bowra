"use client";

/**
 * The Release tab's "This change" card (S6): what changed and who changed it, the evaluation suites, and
 * the test state, each with a link to the tab that holds it. It only states the situation; the test card
 * below keeps the actions.
 */
import { useId, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { ChevronRight } from "@/components/icons";
import { summarizeDraftChanges } from "@/lib/agent-draft-changes";
import type { AgentDraftChange } from "@/lib/agents-api";
import type { TestChangeState } from "./agent-version-validation";

type Tone = "neutral" | "accent" | "positive" | "warning" | "danger";

/** The short label of a test state, as the rail, the This-change card and the test card say it. */
export function testStateLabelKey(state: TestChangeState, regressed: boolean, inconclusive = false): string {
  if (state === "regressions" && !regressed) return "version_test_state_blocked";
  // The test ran but gave no verdict (for example inconsistent results): it did run, so not "couldn't run".
  if (state === "could_not_run" && inconclusive) return "version_test_state_no_conclusion";
  return `version_test_state_${state === "no_cases" ? "not_tested" : state === "nothing" ? "none" : state}`;
}

/** The test state the card repeats in its header, with the tone of the test card below. */
const STATE_TONE: Record<TestChangeState, Tone> = {
  nothing: "neutral",
  not_tested: "neutral",
  no_cases: "neutral",
  testing: "accent",
  passed: "positive",
  risks: "warning",
  regressions: "danger",
  could_not_run: "warning",
  stale: "warning",
  review: "warning",
  rebase: "warning",
};

export function AgentThisChangeCard({
  changes,
  state,
  regressed = true,
  inconclusive = false,
  live,
  pendingCount,
  suites,
  onOpenConfig,
  onOpenEvaluate,
  onViewCheck,
}: {
  changes: readonly AgentDraftChange[];
  state: TestChangeState;
  /** A blocked result has cases that got worse; false when required cases fail on both versions. */
  regressed?: boolean;
  /** A "could not run" result that did run but gave no verdict. */
  inconclusive?: boolean;
  live: number;
  pendingCount: number;
  suites: Array<{ key: string; name: string; count: number }>;
  onOpenConfig?: () => void;
  onOpenEvaluate?: () => void;
  onViewCheck: () => void;
}) {
  const t = useTranslations("agents_marketplace");
  const titleId = useId();
  const summary = summarizeDraftChanges(changes);
  const separator = t("version_list_separator");

  if (!summary.length) {
    return (
      <section className="am-this-change" aria-labelledby={titleId} data-empty="true">
        <header>
          <div>
            <h3 id={titleId}>{t("version_change_same_title", { live })}</h3>
            <p>{t("version_change_same_body")}</p>
          </div>
          {onOpenConfig ? <button type="button" className="am-this-change-go" onClick={onOpenConfig}>{t("version_change_go_config")}</button> : null}
        </header>
      </section>
    );
  }

  const tone = STATE_TONE[state];
  const checkText = state === "rebase" ? t("version_rail_rebase", { version: `v${live}` }) : t(testStateLabelKey(state, regressed, inconclusive) as never);
  const untested = state === "not_tested" || state === "no_cases" || state === "stale";
  const row = (label: string, value: ReactNode, action: { label: string; onClick?: () => void } | null, tone?: Tone) => (
    <div className="am-this-change-row" data-tone={tone}>
      <dt>{label}</dt>
      <dd>{value}</dd>
      {action?.onClick ? (
        <button type="button" className="am-this-change-go" onClick={action.onClick}>{action.label}<ChevronRight size={14} aria-hidden="true" /></button>
      ) : <span />}
    </div>
  );

  return (
    <section className="am-this-change" aria-labelledby={titleId} data-tone={tone}>
      <header>
        <div>
          <h3 id={titleId}>{t("version_change_title")}</h3>
          <p>{t("version_change_base", { live })}</p>
        </div>
        <span className="am-this-change-status" data-tone={tone}>{checkText}</span>
      </header>
      <dl>
        {row(
          t("version_change_row_changes"),
          <>
            {summary.map((item, index) => (
              <span key={item.section} className="am-this-change-section">
                {index ? separator : null}
                {t("version_change_section_count", { section: t(`version_config_section_${item.section}` as never), count: item.count })}
                {item.sources.map((source) => (
                  <span key={source.tag} className="am-change-source" data-source={source.tag}>
                    {t("version_change_source_count", { source: t(`version_change_source_${source.tag}` as never), count: source.count })}
                  </span>
                ))}
              </span>
            ))}
            {pendingCount ? <small>{t("version_change_pending", { count: pendingCount })}</small> : null}
          </>,
          { label: t("version_change_go_config"), onClick: onOpenConfig },
        )}
        {row(
          t("version_change_row_eval"),
          suites.length
            ? suites.map((suite) => t("version_change_eval_suite", { name: suite.name, count: suite.count })).join(separator)
            : t("version_change_eval_none"),
          { label: t("version_change_go_eval"), onClick: onOpenEvaluate },
        )}
        {row(
          t("version_change_row_check"),
          checkText,
          // A change that isn't tested has no details yet; the link leads to the card that tests it.
          { label: t(untested ? "version_change_go_start_check" : "version_change_go_details"), onClick: onViewCheck },
          tone,
        )}
      </dl>
    </section>
  );
}
