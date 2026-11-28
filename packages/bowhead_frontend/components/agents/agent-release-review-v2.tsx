"use client";

/**
 * The reviewer page with FEATURE_AGENT_RELEASE_BAR on (slices 2-5 tech design §5, decision SD3): only what
 * the candidate detail serves. There is no instruction text or line diff; a changed field says 改了, an
 * unchanged one 和 vN 一样. The parent keeps loading, the decision requests and the decline dialog.
 */
import { useEffect, useRef } from "react";
import { useLocale, useTranslations } from "next-intl";

import type { AgentReleaseCandidateDetail, AgentVersionReviewerProjection } from "@/lib/agents-api";
import { releaseEvidence } from "@/lib/agent-release-evidence";
import { releaseNoteText } from "@/lib/agent-release-notes";
import { appPath } from "@/lib/base-path";
import { formatDateTime } from "@/lib/locale";
import { toast } from "@/lib/use-toast";
import { ResourceDiff } from "@/components/ui/resource-diff";
import { AgentBreadcrumb } from "./agent-breadcrumb";
import { agentArtworkStyle } from "./agent-artwork";
import { useCiConfirm } from "./agent-ci-shared";
import { agentMark } from "./agent-mark";
import { agentReleaseReasonKey } from "./agent-release-reason";
import { PluginUpdateNotice } from "./plugin-update-notice";

type Diff = AgentReleaseCandidateDetail["diff"];

/** The first shared release: no earlier release, or the earlier one was private. */
export function isFirstShare(detail: AgentReleaseCandidateDetail): boolean {
  const after = detail.diff.audience.after.mode;
  if (after === "private") return false;
  return detail.base === null || detail.diff.audience.before?.mode === "private";
}

/** The release number approving activates (the server's next_release_sequence of the base). */
export function nextReleaseVersion(detail: AgentReleaseCandidateDetail): number {
  return (detail.base?.release_sequence ?? 0) + 1;
}

export type ReviewChangeKey = "audience" | "name" | "description" | "listing" | "instructions" | "model" | "primary" | "uses" | "knowledge" | "output";

/** One line per served `diff` item, in reading order; `expanded` marks a wider audience or knowledge scope. */
export function reviewChangeLines(diff: Diff): Array<{ key: ReviewChangeKey; changed: boolean; expanded: boolean }> {
  const listing = new Set(diff.listing_fields);
  const otherListing = [...listing].some((field) => field !== "display_name" && field !== "description");
  return [
    { key: "audience", changed: diff.audience.changed, expanded: diff.audience.expanded },
    { key: "name", changed: listing.has("display_name"), expanded: false },
    { key: "description", changed: listing.has("description"), expanded: false },
    ...(otherListing ? [{ key: "listing" as const, changed: true, expanded: false }] : []),
    { key: "instructions", changed: diff.instructions_changed, expanded: false },
    { key: "model", changed: diff.model_changed, expanded: false },
    { key: "primary", changed: diff.primary_capability_changed, expanded: false },
    {
      key: "uses",
      changed: diff.tool_set_changed || diff.skill_set_changed || Boolean(diff.plugin_set_changed) || Boolean(diff.runtime_extensions_changed)
        || Boolean(diff.capability_protocol_block_changed),
      expanded: false,
    },
    { key: "knowledge", changed: Boolean(diff.knowledge_access_changed), expanded: Boolean(diff.knowledge_access_expanded) },
    { key: "output", changed: diff.output_contract_changed, expanded: false },
  ];
}

function isVersionEvaluation(value: AgentReleaseCandidateDetail["evaluation"]): value is AgentVersionReviewerProjection {
  return Boolean(value && "schema_version" in value && value.schema_version === 2);
}

export function AgentReleaseReviewV2({ agentId, detail, busy, error, completed, onApprove, onReject, onWithdraw, onDismissError }: {
  agentId: string;
  detail: AgentReleaseCandidateDetail;
  busy: "approve" | "reject" | "withdraw" | null;
  error: string | null;
  completed: string | null;
  onApprove: () => void;
  onReject: () => void;
  onWithdraw: () => void;
  onDismissError: () => void;
}) {
  const t = useTranslations("agents_marketplace");
  const tList = useTranslations("agent_list_v2");
  const locale = useLocale();
  const resultRef = useRef<HTMLDivElement | null>(null);
  const candidate = detail.candidate;
  const firstShare = isFirstShare(detail);
  const version = nextReleaseVersion(detail);
  const baseVersion = detail.base?.release_sequence ?? null;
  const versionEvaluation = isVersionEvaluation(detail.evaluation) ? detail.evaluation : null;
  const legacyEvaluation = detail.evaluation && !isVersionEvaluation(detail.evaluation) ? detail.evaluation : null;
  const selfReview = versionEvaluation?.actor_overlap === true || detail.actions.withdraw;
  const effectiveCompletion = completed ?? (detail.state === "pending" ? null : detail.state);
  const submitted = formatDateTime(detail.submitted_at, locale, { dateStyle: "medium", timeStyle: "short" }) || detail.submitted_at;
  const listingName = typeof candidate.listing.display_name === "string" && candidate.listing.display_name.trim()
    ? candidate.listing.display_name : detail.agent.display_name;
  const listingDescription = typeof candidate.listing.description === "string" ? candidate.listing.description : "";
  const primaryName = !locale.toLocaleLowerCase().startsWith("zh")
    ? candidate.behavior.primary_capability_name_en || candidate.behavior.primary_capability_name
    : candidate.behavior.primary_capability_name;

  useEffect(() => {
    if (completed) resultRef.current?.focus();
  }, [completed]);

  const audienceText = (summary: AgentReleaseCandidateDetail["candidate"]["audience"]) => summary.mode === "custom"
    ? tList("review_audience_custom", { users: summary.user_count, groups: summary.group_count })
    : t(`release_audience_${summary.mode}` as never);
  const lines = reviewChangeLines(detail.diff);
  const changed = lines.filter((line) => line.changed);
  const unchanged = lines.filter((line) => !line.changed);
  const otherFlags = (["dependency_changed", "capability_changed", "risk_changed"] as const).filter((key) => detail.diff[key]);

  const quality = (() => {
    if (versionEvaluation) return t(`version_timeline_check_${["ready", "ready_with_risks", "blocked"].includes(versionEvaluation.readiness) ? versionEvaluation.readiness : "unknown"}` as never);
    if (!legacyEvaluation) return tList("review_quality_none");
    const evidence = releaseEvidence(legacyEvaluation.release_gate, legacyEvaluation.suites);
    if (evidence.kind === "safety_only") return tList("review_quality_no_cases");
    if (evidence.kind === "waiting") return t("eval_gate_waiting");
    if (evidence.kind === "not_run") return t("eval_gate_not_run");
    return tList("review_quality_tested", { passed: evidence.passed, count: evidence.count });
  })();
  const lookAt = [
    ...(firstShare ? [tList("review_look_first_share")] : []),
    ...(detail.diff.audience.expanded ? [tList("review_look_audience_expanded")] : []),
    ...(detail.diff.knowledge_access_expanded ? [tList("review_look_knowledge_expanded")] : []),
  ];
  const riskReasons = [...new Set((candidate.risk.reasons ?? []).map((code) => t(agentReleaseReasonKey(code) as never)))];
  const semantic = candidate.safety.semantic;
  const semanticMissing = semantic.status !== "pass" && (semantic.reason_codes.includes("semantic_scanner_unavailable")
    || ["unavailable", "skipped", "not_run", "unknown"].includes(semantic.status));
  const safetyReasons = [...new Set(candidate.safety.summary.reason_codes.map((code) => t(agentReleaseReasonKey(code) as never)))];

  const copyLink = () => {
    const url = new URL(appPath(`/agents/detail/?${new URLSearchParams({ id: agentId })}`), window.location.origin).toString();
    void navigator.clipboard?.writeText(url).then(
      () => toast({ kind: "success", text: t("release_bar_link_copied") }),
      () => toast({ kind: "error", text: url }),
    );
  };
  const resultText = effectiveCompletion === "activated"
    ? candidate.audience.mode === "teamwide"
      ? tList("review_result_teamwide", { version })
      : candidate.audience.mode === "custom"
        ? tList("review_result_custom", { version, users: candidate.audience.user_count, groups: candidate.audience.group_count })
        : tList("review_result_private", { version })
    : effectiveCompletion === "withdrawn" || effectiveCompletion === "rejected"
      ? tList(effectiveCompletion === "withdrawn" ? "review_result_withdrawn" : "review_result_rejected")
      : effectiveCompletion ? t(`release_review_complete_${effectiveCompletion}` as never) : null;
  // Withdrawing ends the review and the next submission needs a new test, so it is confirmed like the bar's 撤回提交….
  const { confirm, dialog } = useCiConfirm();
  const withdraw = async () => {
    if (!await confirm({
      title: tList("review_withdraw_title"),
      description: tList("review_withdraw_body"),
      confirmLabel: tList("review_withdraw_confirm"),
      cancelLabel: tList("review_withdraw_keep"),
      tone: "danger",
    })) return;
    onWithdraw();
  };

  return (
    <div className="am-page am-release-page" data-layout="v2">
      <AgentBreadcrumb documentLink href="/agents/manage/governance/" label={tList("view_team")} />
      <header className="am-review-hero">
        <span className="am-management-mark" aria-hidden="true" style={agentArtworkStyle(`${agentId}:${detail.agent.display_name}`)}>{agentMark(detail.agent.display_name)}</span>
        <div>
          <h1>{tList("review_title", { name: detail.agent.display_name })}</h1>
          <span>
            {tList("review_meta", { date: submitted, version })}
            {selfReview ? ` · ${tList("review_meta_self")}` : ""}
          </span>
        </div>
      </header>
      {error ? <div className="am-notice" role="alert"><span style={{ whiteSpace: "pre-line" }}>{error}</span><button type="button" onClick={onDismissError}>{t("dismiss")}</button></div> : null}
      <PluginUpdateNotice updates={detail.plugin_updates} skillUpdates={detail.skill_updates} />

      <div className="am-review-layout">
        <div className="am-review-primary">
          {firstShare ? (
            <div className="am-review-v2-banner" role="note">
              <strong>{tList("review_first_share_title")}</strong> {tList("review_first_share_body")}
            </div>
          ) : null}

          <section className="am-review-card am-review-v2-card" aria-labelledby="am-review-v2-what">
            <h2 id="am-review-v2-what">{tList("review_changes_heading")}</h2>
            <p>{detail.release_note?.trim() ? tList("review_note", { note: releaseNoteText(detail.release_note, t) }) : tList("review_note_none")}</p>
            {detail.base === null ? <p>{tList("review_changes_first_release")}</p> : (
              <dl className="am-review-v2-facts">
                {changed.map((line) => <div key={line.key}>
                  <dt>{tList(`review_field_${line.key}`)}</dt>
                  <dd>
                    {line.key === "audience" && detail.diff.audience.before
                      // Only counts are served: a swapped member keeps 「指定成员 1 人」 on both sides, so say the list changed.
                      ? audienceText(detail.diff.audience.before) === audienceText(detail.diff.audience.after)
                        ? tList("review_audience_list_changed", { audience: audienceText(detail.diff.audience.after) })
                        : tList("review_audience_change", { before: audienceText(detail.diff.audience.before), after: audienceText(detail.diff.audience.after) })
                      : tList("review_changed")}
                    {line.expanded ? <span className="am-review-v2-chip" data-tone="warning">{tList("review_expanded")}</span> : null}
                  </dd>
                </div>)}
                {unchanged.length ? <div>
                  <dt>{tList("review_field_other")}</dt>
                  <dd>{tList("review_same", { version: baseVersion ?? 0, fields: unchanged.map((line) => tList(`review_field_${line.key}`)).join(tList("review_list_separator")) })}</dd>
                </div> : null}
              </dl>
            )}
            {otherFlags.length ? <ul className="am-review-v2-other">{otherFlags.map((key) => <li key={key}>{t(`release_diff_${key}` as never)}</li>)}</ul> : null}
            <ResourceDiff input={{ plugins: detail.diff.plugin_items, tools: detail.diff.tool_changes, skills: detail.diff.skill_changes }} />
          </section>

          <details className="am-review-card am-review-v2-card" open={firstShare || undefined}>
            <summary>{tList("review_full_heading", { version })}</summary>
            <dl className="am-review-v2-facts">
              <div><dt>{tList("review_field_name")}</dt><dd>{listingName}</dd></div>
              <div><dt>{tList("review_field_description")}</dt><dd>{listingDescription || t("release_review_listing_empty")}</dd></div>
              <div><dt>{tList("review_field_instructions")}</dt><dd>{detail.base === null ? tList("review_full_instructions") : `${detail.diff.instructions_changed ? tList("review_changed") : tList("review_same_short", { version: baseVersion ?? 0 })} · ${tList("review_full_instructions")}`}</dd></div>
              <div><dt>{tList("review_field_uses")}</dt><dd>{tList("review_full_uses", { tools: candidate.behavior.tool_count, skills: candidate.behavior.skill_count })}</dd></div>
              <div><dt>{tList("review_field_model")}</dt><dd translate="no">{candidate.behavior.model || tList("review_full_model_default")}</dd></div>
              <div>
                <dt>{tList("review_field_primary")}</dt>
                <dd>{primaryName || t("release_primary_none")} {candidate.behavior.requested_primary_capability_ref ? <small translate="no">{candidate.behavior.requested_primary_capability_ref}</small> : null}
                  {" · "}{t(`release_primary_${candidate.behavior.verification_state}` as never)}</dd>
              </div>
              <div><dt>{tList("review_field_knowledge")}</dt><dd>{detail.base === null ? tList("review_full_knowledge_first") : detail.diff.knowledge_access_changed ? tList("review_changed") : tList("review_same_short", { version: baseVersion ?? 0 })}</dd></div>
              <div><dt>{tList("review_field_output")}</dt><dd>{candidate.behavior.has_output_contract ? tList("review_full_output_yes", { count: candidate.behavior.output_contract_refs.length }) : tList("review_full_output_no")}</dd></div>
              <div><dt>{tList("review_field_audience")}</dt><dd>{audienceText(candidate.audience)}</dd></div>
              <div><dt>{tList("review_field_data")}</dt><dd>{t("release_review_audience_boundary")}</dd></div>
            </dl>
          </details>

          {versionEvaluation || legacyEvaluation ? <section className="am-review-card am-review-v2-card" aria-labelledby="am-review-v2-tests">
            <h2 id="am-review-v2-tests">{tList("review_tests_heading")}</h2>
            {versionEvaluation ? <>
              <p>{t("version_review_readiness", { readiness: quality })} · {t("version_review_snapshot", { date: formatDateTime(versionEvaluation.snapshot_created_at, locale, { dateStyle: "medium" }) || versionEvaluation.snapshot_created_at })}</p>
              {versionEvaluation.override ? <p><strong>{t("version_review_override_title")}</strong> {versionEvaluation.override.reason_overrides.map((item) => `${item.reason_code}: ${item.reason_note}`).join(" · ")}</p> : null}
            </> : null}
            {legacyEvaluation ? <ul className="am-review-v2-suites">
              {legacyEvaluation.suites.map((suite) => <li key={suite.suite_id}>
                <strong>{suite.suite_kind === "platform_contract" ? t("eval_platform_suite_name") : suite.name}</strong>
                <span>{t(`eval_state_${suite.evidence.state}` as never)}</span>
                <small>{t("release_review_eval_cases", { count: suite.evidence.run?.summary?.case_count ?? suite.revision?.case_count ?? 0, passed: suite.evidence.run?.summary?.["pass@k_cases"] ?? 0 })}</small>
              </li>)}
            </ul> : null}
            <p className="am-review-v2-hint">{tList("review_tests_hint", { reason: t("release_reject_evidence") })}</p>
          </section> : null}
        </div>

        <aside>
          <section className="am-review-decision-card am-review-v2-decision" aria-labelledby="am-review-v2-decide">
            <h2 id="am-review-v2-decide">{tList("review_decision_heading")}</h2>
            <dl className="am-review-v2-panel">
              <div><dt>{tList("review_look")}</dt><dd>{lookAt.length ? lookAt.join(" · ") : tList("review_look_none")}</dd></div>
              <div><dt>{tList("review_quality")}</dt><dd>{quality}</dd></div>
              <div>
                <dt>{tList("review_risk")}</dt>
                <dd>{firstShare ? tList("review_risk_first_share") : <>
                  {t("risk_label", { level: t(`risk_${candidate.risk.level ?? "unknown"}` as never) })}
                  {riskReasons.length ? <small>{riskReasons.join(" · ")}</small> : null}
                </>}</dd>
              </div>
              <div>
                <dt>{tList("review_semantic")}</dt>
                <dd>{semanticMissing ? tList("review_semantic_missing")
                  : <>{t("release_safety_status", { status: t(`release_safety_${candidate.safety.status}` as never) })}{safetyReasons.length ? <small>{safetyReasons.join(" · ")}</small> : null}</>}</dd>
              </div>
            </dl>
            {!effectiveCompletion ? <div className="am-review-actions">
              {detail.actions.approve ? <button type="button" className="am-action-primary" disabled={busy !== null || legacyEvaluation?.release_gate.ready === false || versionEvaluation?.decision_availability.approve === false} onClick={onApprove}>
                {busy === "approve" ? t("release_approving") : tList("review_approve", { version })}
              </button> : null}
              {detail.actions.reject ? <button type="button" disabled={busy !== null || versionEvaluation?.decision_availability.reject === false} onClick={onReject}>
                {busy === "reject" ? t("release_rejecting") : tList("review_reject")}
              </button> : null}
              {detail.actions.withdraw ? <button type="button" className="am-review-v2-link" disabled={busy !== null} onClick={() => void withdraw()}>
                {busy === "withdraw" ? t("release_withdrawing") : tList("review_withdraw")}
              </button> : null}
            </div> : null}
            {!effectiveCompletion ? <small>{t("release_review_runtime_note")}</small> : null}
            {effectiveCompletion && resultText ? (
              <div className="am-review-v2-result" role="status" tabIndex={-1} ref={resultRef} data-state={effectiveCompletion}>
                <p>{resultText}</p>
                <div>
                  {effectiveCompletion === "activated" ? <button type="button" onClick={copyLink}>{tList("review_copy_link")}</button> : null}
                  <a href={appPath("/agents/manage/governance/")}>{tList("review_back_to_queue")}</a>
                </div>
              </div>
            ) : null}
          </section>
          {dialog}
        </aside>
      </div>
    </div>
  );
}
