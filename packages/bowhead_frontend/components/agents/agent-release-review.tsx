"use client";

import { useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import {
  approveAgentReleaseCandidate,
  getAgentReleaseCandidate,
  rejectAgentReleaseCandidate,
  withdrawAgentReleaseCandidate,
  type AgentReleaseCandidateDetail,
  type AgentVersionReviewerProjection,
} from "@/lib/agents-api";
import { releaseEvidence } from "@/lib/agent-release-evidence";
import { ApiError } from "@/lib/api-client";
import { appPath } from "@/lib/base-path";
import { AgentBreadcrumb } from "./agent-breadcrumb";
import { agentArtworkStyle } from "./agent-artwork";
import { agentMark } from "./agent-mark";
import { agentReleaseBlockedText, agentReleaseErrorKey, agentReleaseReasonKey } from "./agent-release-reason";
import { CharIcon } from "@/components/char-icon";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ResourceDiff } from "@/components/ui/resource-diff";
import { releaseNoteText } from "@/lib/agent-release-notes";
import { PluginUpdateNotice } from "./plugin-update-notice";

const DIFF_KEYS = [
  "instructions_changed",
  "model_changed",
  "output_contract_changed",
  "tool_set_changed",
  "skill_set_changed",
  "runtime_extensions_changed",
  "primary_capability_changed",
  "capability_protocol_block_changed",
  "behavior_changed",
  "dependency_changed",
  "capability_changed",
  "risk_changed",
] as const;

function formatDate(value: string, locale: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function isVersionEvaluation(value: AgentReleaseCandidateDetail["evaluation"]): value is AgentVersionReviewerProjection {
  return Boolean(value && "schema_version" in value && value.schema_version === 2);
}

export function AgentReleaseReview({ agentId, candidateId }: { agentId: string; candidateId: string }) {
  const t = useTranslations("agents_marketplace");
  const locale = useLocale();
  const [detail, setDetail] = useState<AgentReleaseCandidateDetail | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("needs_changes");
  const [busy, setBusy] = useState<"approve" | "reject" | "withdraw" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [completed, setCompleted] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAgentReleaseCandidate(agentId, candidateId).then((value) => {
      if (!cancelled) setDetail(value);
    }).catch((cause) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : t("release_review_load_failed"));
    });
    return () => { cancelled = true; };
  }, [agentId, candidateId, t]);

  const changes = useMemo(() => {
    if (!detail) return [];
    return [
      ...detail.diff.listing_fields.map((field) => `listing:${field}`),
      ...DIFF_KEYS.filter((key) => detail.diff[key]),
    ];
  }, [detail]);

  const act = async (action: "approve" | "reject" | "withdraw") => {
    if (!detail || busy) return;
    setBusy(action);
    try {
      const result = action === "approve"
        ? await approveAgentReleaseCandidate(detail)
        : action === "reject"
          ? await rejectAgentReleaseCandidate(detail, reason)
          : await withdrawAgentReleaseCandidate(detail);
      setCompleted(result.state);
      setRejectOpen(false);
      setError(null);
    } catch (cause) {
      // A blocked publication answers with a code that says what is missing.  Without
      // this the reviewer gets the generic "state changed, refresh the queue", which
      // sends them to do the one thing that cannot help.
      const key = agentReleaseErrorKey(cause instanceof ApiError ? cause.code : undefined);
      const blockedText = cause instanceof ApiError ? agentReleaseBlockedText(cause, (name, values) => t(name as never, values as never)) : null;
      setError(blockedText ?? (key
        ? t(key as never)
        : cause instanceof Error ? cause.message : t("release_review_action_failed")));
    } finally {
      setBusy(null);
    }
  };

  if (!detail) {
    return <div className="am-page am-release-page"><AgentBreadcrumb documentLink href="/agents/manage/governance/" label={t("back_to_manage")} />{error && !rejectOpen ? <div className="am-notice" role="alert"><span>{error}</span></div> : <div className="am-detail-loading" aria-busy="true" />}</div>;
  }

  const candidate = detail.candidate;
  const candidateDisplayName = typeof candidate.listing.display_name === "string"
    ? candidate.listing.display_name
    : detail.agent.display_name;
  const candidateDescription = typeof candidate.listing.description === "string"
    ? candidate.listing.description
    : "";
  const effectiveCompletion = completed ?? (detail.state === "pending" ? null : detail.state);
  const safetyReasons = [...new Set(candidate.safety.summary.reason_codes.map((code) => (
    t(agentReleaseReasonKey(code) as never)
  )))];
  const riskReasons = [...new Set((candidate.risk.reasons ?? []).map((code) => (
    t(agentReleaseReasonKey(code) as never)
  )))];
  // Only Chinese UIs use the Chinese name; other UI languages use English (falling back to the original name if English is absent).
  const primaryCapabilityName = !locale.toLocaleLowerCase().startsWith("zh")
    ? candidate.behavior.primary_capability_name_en || candidate.behavior.primary_capability_name
    : candidate.behavior.primary_capability_name;
  const versionEvaluation = isVersionEvaluation(detail.evaluation) ? detail.evaluation : null;
  const legacyEvaluation = detail.evaluation && !isVersionEvaluation(detail.evaluation) ? detail.evaluation : null;

  const readiness = versionEvaluation?.readiness ?? "unknown";
  const readinessKey = ["ready", "ready_with_risks", "blocked", "unknown"].includes(readiness) ? readiness : "unknown";

  return (
    <div className="am-page am-release-page">
      <AgentBreadcrumb documentLink href="/agents/manage/governance/" label={t("back_to_inbox")} />
      <header className="am-review-hero">
        <span className="am-management-mark" style={agentArtworkStyle(`${agentId}:${detail.agent.display_name}`)}>{agentMark(detail.agent.display_name)}</span>
        <div><p>{t("release_review_eyebrow")}</p><h1>{detail.agent.display_name}</h1><span>{t("release_review_submitted", { date: formatDate(detail.submitted_at, locale) })}</span></div>
      </header>
      {error && !rejectOpen ? <div className="am-notice" role="alert"><span style={{ whiteSpace: "pre-line" }}>{error}</span><button onClick={() => setError(null)}>{t("dismiss")}</button></div> : null}
      <PluginUpdateNotice updates={detail.plugin_updates} skillUpdates={detail.skill_updates} />
      {effectiveCompletion ? <section className="am-review-complete" role="status"><strong>{t(`release_review_complete_${effectiveCompletion}` as never)}</strong><p>{t("release_review_complete_body")}</p><a href={appPath("/agents/manage/governance/")}>{t("back_to_inbox")}</a></section> : null}

      <div className="am-review-layout">
        <div className="am-review-primary">
          <section className="am-review-card am-review-listing">
            <header><span>{t("release_review_listing_eyebrow")}</span><h2>{candidateDisplayName}</h2><p>{candidateDescription || t("release_review_listing_empty")}</p></header>
          </section>
          <section className="am-review-card">
            <header><span>{t("release_review_changes_eyebrow")}</span><h2>{t("release_review_changes_title", { count: changes.length + (detail.diff.audience.changed ? 1 : 0) })}</h2><p>{releaseNoteText(detail.release_note, t)}</p></header>
            <div className="am-review-change-grid">
              {changes.length ? changes.map((change) => <div key={change}><span aria-hidden="true"><CharIcon ch="✓" aria-hidden /></span><p>{change.startsWith("listing:") ? t((change === "listing:display_name" ? "release_diff_listing_name" : change === "listing:description" ? "release_diff_listing_description" : "release_diff_listing_other") as never) : t(`release_diff_${change}` as never)}</p></div>) : <p>{t("release_diff_none")}</p>}
              {detail.diff.audience.changed ? <div data-tone={detail.diff.audience.expanded ? "warning" : "neutral"}><span aria-hidden="true">{detail.diff.audience.expanded ? "!" : <CharIcon ch="✓" aria-hidden />}</span><p>{t(detail.diff.audience.expanded ? "release_diff_audience_expanded" : "release_diff_audience_changed")}</p></div> : null}
            </div>
            <ResourceDiff input={{ plugins: detail.diff.plugin_items, tools: detail.diff.tool_changes, skills: detail.diff.skill_changes }} />
          </section>

          <section className="am-review-card">
            <header><span>{t("release_review_audience_eyebrow")}</span><h2>{t(`release_audience_${candidate.audience.mode}` as never)}</h2>{candidate.audience.mode === "custom" ? <p>{t("release_review_audience_counts", { users: candidate.audience.user_count, groups: candidate.audience.group_count })}</p> : null}</header>
            <p className="am-review-boundary">{t("release_review_audience_boundary")}</p>
          </section>

          {versionEvaluation ? <section className="am-review-card am-review-evaluation">
            <header><span>{t("eval_eyebrow")}</span><h2>{t("version_review_evidence_title")}</h2><p>{t("version_review_evidence_body")}</p></header>
            <div className="am-eval-gate" data-readiness={readinessKey} data-ready={readiness === "ready" && versionEvaluation.decision_availability.approve ? true : versionEvaluation.decision_availability.approve ? "neutral" : false}><span>{readiness === "ready" && versionEvaluation.decision_availability.approve ? <CharIcon ch="✓" /> : "!"}</span><div><strong>{t("version_review_readiness", { readiness: t(`version_timeline_check_${readinessKey}` as never) })}</strong><p>{t("version_review_snapshot", { date: formatDate(versionEvaluation.snapshot_created_at, locale) })}</p></div></div>
            {versionEvaluation.actor_overlap ? <p className="am-review-boundary">{t("version_review_actor_overlap")}</p> : null}
            {versionEvaluation.override ? <div className="am-review-safety"><strong>{t("version_review_override_title")}</strong><p>{versionEvaluation.override.reason_overrides.map((item) => `${item.reason_code}: ${item.reason_note}`).join(" · ")}</p></div> : null}
          </section> : null}

          {legacyEvaluation ? <section className="am-review-card am-review-evaluation">
            <header><span>{t("eval_eyebrow")}</span><h2>{t("release_review_eval_title")}</h2><p>{t("release_review_eval_body")}</p></header>
            {(() => {
              // Same states as the Owner's Evaluate card, without actions; it names who provided no cases (§9).
              const evidence = releaseEvidence(legacyEvaluation.release_gate, legacyEvaluation.suites);
              if (evidence.kind === "waiting") {
                return <div className="am-eval-gate" data-ready={false}><span>!</span><div><strong>{t("eval_gate_waiting")}</strong><p>{t("eval_gate_candidate_body_waiting")}</p></div></div>;
              }
              if (evidence.kind === "safety_only") {
                return <div className="am-eval-gate" data-ready="neutral"><span>i</span><div><strong>{t("release_review_owner_no_cases")}</strong><p>{evidence.safety ? t("release_review_owner_no_cases_body", { passed: evidence.safety.passed, count: evidence.safety.count }) : t("release_review_owner_no_cases_body_unrun")}</p></div></div>;
              }
              if (evidence.kind === "not_run") {
                return <div className="am-eval-gate" data-ready={false}><span>!</span><div><strong>{t("eval_gate_not_run")}</strong><p>{t("eval_gate_not_run_body", { suite: evidence.suite.name.trim() || t("eval_owner_suite_default_name"), count: evidence.count, target: t("eval_last_run_candidate") })}</p></div></div>;
              }
              return <div className="am-eval-gate" data-ready={true}><span><CharIcon ch="✓" /></span><div><strong>{t("eval_gate_ready")}</strong><p>{t("eval_gate_candidate_body")}</p></div></div>;
            })()}
            <div className="am-review-eval-suites">
              {legacyEvaluation.suites.map((suite) => <div key={suite.suite_id}><div><strong>{suite.suite_kind === "platform_contract" ? t("eval_platform_suite_name") : suite.name}</strong><span>{t(`eval_state_${suite.evidence.state}` as never)}</span></div><small>{t("release_review_eval_cases", { count: suite.evidence.run?.summary?.case_count ?? suite.revision?.case_count ?? 0, passed: suite.evidence.run?.summary?.["pass@k_cases"] ?? 0 })}</small></div>)}
            </div>
          </section> : null}
          <details className="am-review-card am-review-details">
            <summary>{t("release_review_behavior_title")}</summary>
            <p>{t("release_review_behavior_body")}</p>
            <dl className="am-review-facts">
              <div className="am-review-fact-wide"><dt>{t("release_review_primary")}</dt><dd>{primaryCapabilityName || t("release_primary_none")} {candidate.behavior.requested_primary_capability_ref ? <small translate="no">{candidate.behavior.requested_primary_capability_ref}</small> : null}</dd></div>
              <div><dt>{t("release_review_verification")}</dt><dd>{t(`release_primary_${candidate.behavior.verification_state}` as never)}</dd></div>
              <div><dt>{t("release_review_outputs")}</dt><dd>{candidate.behavior.output_contract_refs.length}</dd></div>
              <div><dt>{t("release_review_tools")}</dt><dd>{candidate.behavior.tool_count}</dd></div>
              <div><dt>{t("release_review_skills")}</dt><dd>{candidate.behavior.skill_count}</dd></div>
              <div><dt>{t("release_iterations")}</dt><dd>{candidate.behavior.max_iterations}</dd></div>
              <div><dt>{t("release_output_contract_title")}</dt><dd>{candidate.behavior.has_output_contract ? t("yes") : t("no")}</dd></div>
            </dl>
          </details>

        </div>

        <aside>
          <section className="am-review-decision-card">
            <span>{t("release_review_decision_eyebrow")}</span>
            <h2>{t("release_review_decision_title")}</h2>
            <div className="am-review-risk" data-risk={candidate.risk.level ?? "unknown"}><strong>{t("risk_label", { level: t(`risk_${candidate.risk.level ?? "unknown"}` as never) })}</strong><p>{riskReasons.length ? riskReasons.join(" · ") : t("release_review_risk_no_details")}</p></div>
            <div className="am-review-safety" data-status={candidate.safety.status}><strong>{t("release_safety_status", { status: t(`release_safety_${candidate.safety.status}` as never) })}</strong><p>{safetyReasons.length ? safetyReasons.join(" · ") : t("release_safety_no_findings")}</p><small>{t("release_review_safety_boundary")}</small></div>

            {!effectiveCompletion ? <div className="am-review-actions">
              {detail.actions.approve ? <button className="am-action-primary" disabled={busy !== null || legacyEvaluation?.release_gate.ready === false || versionEvaluation?.decision_availability.approve === false} onClick={() => void act("approve")}>{busy === "approve" ? t("release_approving") : t("release_approve")}</button> : null}
              {detail.actions.reject ? <button className="am-danger-action" disabled={busy !== null || versionEvaluation?.decision_availability.reject === false} onClick={() => { setReason("needs_changes"); setError(null); setRejectOpen(true); }}>{busy === "reject" ? t("release_rejecting") : t("release_reject")}</button> : null}
              {detail.actions.withdraw ? <button disabled={busy !== null} onClick={() => void act("withdraw")}>{busy === "withdraw" ? t("release_withdrawing") : t("release_withdraw")}</button> : null}
            </div> : null}
            <small>{t("release_review_runtime_note")}</small>
          </section>
        </aside>
      </div>
      {rejectOpen && !effectiveCompletion && detail.actions.reject ? <ConfirmDialog
        title={t("release_reject_title")}
        description={t("release_reject_body")}
        confirmLabel={busy === "reject" ? t("release_rejecting") : t("release_reject_confirm")}
        cancelLabel={t("release_reject_cancel")}
        busy={busy !== null}
        onCancel={() => setRejectOpen(false)}
        onConfirm={() => void act("reject")}
      >
        <label className="am-review-rejection"><span>{t("release_rejection_reason")}</span><select name="release-rejection-reason" autoComplete="off" value={reason} disabled={busy !== null} onChange={(event) => setReason(event.target.value)}><option value="needs_changes">{t("release_reject_needs_changes")}</option><option value="risk_not_justified">{t("release_reject_risk")}</option><option value="audience_too_broad">{t("release_reject_audience")}</option><option value="insufficient_evidence">{t("release_reject_evidence")}</option></select></label>
        {error ? <p role="alert">{error}</p> : null}
      </ConfirmDialog> : null}
    </div>
  );
}
