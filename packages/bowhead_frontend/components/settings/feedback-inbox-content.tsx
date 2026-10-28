"use client";

// agent-feedback-loop FE batch A: feedback inbox (team-management tab, tech-design §2.7 trio ②).
// Skeleton: open list + details expansion + adopt/reject/withdraw. API decides all permissions (codes → wording);
// frontend never recalculates permission; mine / for_my_agent are server signals.
// Four adoption outcomes: prefill (handoff to editor/inline display) / reroute (candidate stays open) / degraded (decision persisted, no prefill) / error.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { addedProgress, SpaceInstructionTargets } from "@/components/settings/space-instruction-targets";
import { Button } from "@/components/ui/button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { formatDateYMD } from "@/lib/format";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { Switch } from "@/components/ui/switch";
import type { SpaceInstructions } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import {
  adoptFeedbackCandidate,
  decideFeedbackCandidate,
  getFeedbackCandidate,
  listFeedbackCandidates,
  rerouteFeedbackCandidate,
  setFeedbackCaseworkEnabled,
  stashFeedbackPrefill,
  withdrawFeedbackCandidate,
  type FeedbackAdoptResult,
  type FeedbackCandidateDetail,
  type FeedbackCandidateRow,
  type FeedbackCoachStatus,
  type FeedbackInboxView,
  type FeedbackRerouteTarget,
  type FeedbackSpaceInstructionTarget,
} from "@/lib/feedback-candidates-api";
import { useAppRouter } from "@/lib/use-app-router";

const ERROR_KEYS: Record<string, string> = {
  self_approval_forbidden: "err_self_approval_forbidden",
  feedback_candidate_not_open: "err_not_open",
  feedback_decision_forbidden: "err_decision_forbidden",
  feedback_adopt_forbidden: "err_adopt_forbidden",
  feedback_adopt_required: "err_adopt_required",
  feedback_adopt_kind_unsupported: "err_kind_unsupported",
  feedback_withdraw_forbidden: "err_withdraw_forbidden",
  feedback_store_unavailable: "err_store_unavailable",
  agent_working_copy_missing: "err_working_copy_missing",
  agent_release_cutover_required: "err_cutover_required",
  team_skill_body_missing: "err_skill_body_missing",
  team_model_unconfigured: "err_model_unconfigured",
  feedback_candidate_not_found: "err_not_found",
  not_found: "err_not_found",
  feedback_reroute_forbidden: "err_reroute_forbidden",
  feedback_reroute_target_not_found: "err_reroute_target_not_found",
  feedback_reroute_not_available: "err_reroute_not_available",
  signature_suppressed: "err_signature_suppressed",
  quota_exceeded: "err_quota_exceeded",
  feedback_settings_forbidden: "err_settings_forbidden",
};

type Outcome =
  | { kind: "decided"; state: string }
  | { kind: "adopted"; result: FeedbackAdoptResult }
  | { kind: "reroute"; result: FeedbackAdoptResult }
  | { kind: "rerouted"; label: string };

function rerouteTargetOf(t: FeedbackRerouteTarget | null | undefined): { target_kind: NonNullable<FeedbackRerouteTarget["target_kind"]>; target_ref: string; label: string } | null {
  if (!t || !t.target_kind || !t.target_ref) return null;
  return { target_kind: t.target_kind, target_ref: t.target_ref, label: t.label || t.target_ref };
}

function proposedText(row: FeedbackCandidateRow): string {
  return typeof row.proposed?.text === "string" ? row.proposed.text : "";
}

export function FeedbackInboxContent() {
  const t = useTranslations("settings.feedbackInbox");
  const commonT = useTranslations("common");
  const router = useAppRouter();
  const [items, setItems] = useState<FeedbackCandidateRow[]>([]);
  const [viewerIsAdmin, setViewerIsAdmin] = useState(false);
  const [coach, setCoach] = useState<FeedbackCoachStatus | null>(null);
  const [coachBusy, setCoachBusy] = useState(false);
  const [coachErr, setCoachErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<FeedbackInboxView>("open");
  const [pendingCount, setPendingCount] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<FeedbackCandidateDetail | null>(null);
  const [detailErr, setDetailErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [rejectNote, setRejectNote] = useState("");
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  const [actionErr, setActionErr] = useState<Record<string, string>>({});
  const requestVersionRef = useRef(0);
  const mountedRef = useRef(true);

  const kindLabel = useMemo<Record<string, string>>(
    () => ({
      agent_def: t("kind_agent_def"),
      semantic: t("kind_semantic"),
      team_skill: t("kind_team_skill"),
      tool_source: t("kind_tool_source"),
      new_asset: t("kind_new_asset"),
      retire: t("kind_retire"),
    }),
    [t],
  );

  const errorText = useCallback(
    (cause: unknown): string => {
      const code = cause instanceof ApiError ? cause.code : undefined;
      const key = code ? ERROR_KEYS[code] : undefined;
      if (key) return t(key as never);
      return cause instanceof Error && cause.message ? cause.message : t("err_generic");
    },
    [t],
  );

  const load = useCallback(async (showLoading = false) => {
    const requestVersion = ++requestVersionRef.current;
    if (showLoading) {
      setLoading(true);
      setItems([]);
    }
    try {
      setErr(null);
      const result = await listFeedbackCandidates(view);
      if (requestVersion === requestVersionRef.current) {
        setItems(result.items);
        if (view === "open") setPendingCount(result.items.length);
        setViewerIsAdmin(result.viewer_is_admin);
        setCoach(result.coach ?? null);
      }
    } catch (e) {
      if (requestVersion === requestVersionRef.current) setErr(errorText(e));
    } finally {
      if (mountedRef.current && requestVersion === requestVersionRef.current) setLoading(false);
    }
  }, [errorText, view]);

  useEffect(() => {
    mountedRef.current = true;
    void load(true);
    return () => {
      mountedRef.current = false;
    };
  }, [load]);

  const toggleDetail = useCallback(
    async (id: string) => {
      if (openId === id) {
        setOpenId(null);
        setDetail(null);
        return;
      }
      setOpenId(id);
      setDetail(null);
      setDetailErr(null);
      try {
        const d = await getFeedbackCandidate(id);
        if (mountedRef.current) setDetail(d);
      } catch (e) {
        if (mountedRef.current) setDetailErr(errorText(e));
      }
    },
    [openId, errorText],
  );

  const withBusy = useCallback(async (id: string, fn: () => Promise<void>) => {
    setBusy((prev) => new Set(prev).add(id));
    setActionErr((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    try {
      await fn();
    } catch (e) {
      if (mountedRef.current) setActionErr((prev) => ({ ...prev, [id]: errorText(e) }));
    } finally {
      if (mountedRef.current) setBusy((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  }, [errorText]);

  const reject = (id: string) =>
    withBusy(id, async () => {
      const r = await decideFeedbackCandidate(id, "rejected", rejectNote.trim());
      setRejecting(null);
      setRejectNote("");
      // Never refetch list: server lists only open rows; processed row disappears along with outcome hint and Open editor button.
      setOutcomes((prev) => ({ ...prev, [id]: { kind: "decided", state: r.state } }));
    });

  const withdraw = (id: string) =>
    withBusy(id, async () => {
      const r = await withdrawFeedbackCandidate(id);
      setOutcomes((prev) => ({ ...prev, [id]: { kind: "decided", state: r.state } }));
    });

  const adopt = (row: FeedbackCandidateRow) =>
    withBusy(row.id, async () => {
      const result = await adoptFeedbackCandidate(row.id);
      if (result.reroute) {
        setOutcomes((prev) => ({ ...prev, [row.id]: { kind: "reroute", result } }));
        return;
      }
      setOutcomes((prev) => ({ ...prev, [row.id]: { kind: "adopted", result } }));
    });

  const openPrefillTarget = (row: FeedbackCandidateRow, result: FeedbackAdoptResult) => {
    const prefill = result.prefill;
    if (!prefill) return;
    if (prefill.kind === "agent_system_prompt") {
      stashFeedbackPrefill({
        candidate_id: row.id,
        target_kind: row.target_kind,
        target_ref: row.target_ref,
        prefill,
        base: result.base ?? {},
        banner: result.banner,
      });
      router.push(`/agents/manage/?agent_id=${encodeURIComponent(row.target_ref)}&feedback_prefill=1`);
      return;
    }
    if (prefill.kind === "team_skill_body") {
      stashFeedbackPrefill({ candidate_id: row.id, target_kind: row.target_kind, target_ref: row.target_ref, prefill, base: result.base ?? {}, banner: result.banner });
      router.push(`/skills/?skill=${encodeURIComponent(row.target_ref)}&feedback_prefill=1`);
      return;
    }
    if (prefill.kind === "cca_intent") {
      stashFeedbackPrefill({ candidate_id: row.id, target_kind: row.target_kind, target_ref: row.target_ref, prefill, base: result.base ?? {}, banner: result.banner });
      router.push(`/agents/new/?feedback_prefill=1&ref=${encodeURIComponent(row.target_ref)}`);
    }
  };

  const landed = (space: FeedbackSpaceInstructionTarget, workspaceId: string, value: SpaceInstructions) =>
    space.workspace_id === workspaceId
      ? {
          ...space,
          instructions_version: value.version,
          added: { version: value.version, by_name: value.updated_by_name ?? "", at: value.updated_at ?? new Date().toISOString() },
        }
      : space;

  // A closed suggestion in the "Processed" view carries its Space targets on the row itself.
  const markRowAdded = (rowId: string, workspaceId: string, value: SpaceInstructions) =>
    setItems((prev) =>
      prev.map((item) =>
        item.id === rowId && item.space_targets
          ? { ...item, space_targets: { ...item.space_targets, spaces: item.space_targets.spaces.map((space) => landed(space, workspaceId, value)) } }
          : item,
      ),
    );

  // C6: the saved version is shown on the source Space's row and as progress on the card.
  const markAdded = (rowId: string, workspaceId: string, value: SpaceInstructions) =>
    setOutcomes((prev) => {
      const outcome = prev[rowId];
      const prefill = outcome?.kind === "adopted" ? outcome.result.prefill : null;
      if (outcome?.kind !== "adopted" || prefill?.kind !== "space_instructions") return prev;
      const spaces = prefill.spaces.map((space) => landed(space, workspaceId, value));
      return { ...prev, [rowId]: { ...outcome, result: { ...outcome.result, prefill: { ...prefill, spaces } } } };
    });

  const reroute = (row: FeedbackCandidateRow, target: { target_kind: NonNullable<FeedbackRerouteTarget["target_kind"]>; target_ref: string; label: string }) =>
    withBusy(row.id, async () => {
      const r = await rerouteFeedbackCandidate(row.id, { target_kind: target.target_kind, target_ref: target.target_ref });
      // Original row leaves open list; new target redecides approver. Insert server-returned candidate, otherwise show only hint (caller lacks target lane).
      setOutcomes((prev) => ({ ...prev, [row.id]: { kind: "rerouted", label: target.label } }));
      if (r.candidate) {
        const next = r.candidate;
        // Merged existing candidate: replace row with fresh server projection (evidence count/casework changed); created: prepend.
        setItems((prev) => (prev.some((x) => x.id === next.id) ? prev.map((x) => (x.id === next.id ? next : x)) : [next, ...prev]));
      }
    });

  const toggleCoach = async (enabled: boolean) => {
    const prev = coach;
    if (!prev) return;
    setCoachBusy(true);
    setCoachErr(null);
    setCoach({ ...prev, casework_enabled: enabled });
    try {
      const r = await setFeedbackCaseworkEnabled(enabled);
      if (mountedRef.current) setCoach((c) => (c ? { ...c, casework_enabled: r.enabled } : c));
    } catch (e) {
      if (mountedRef.current) {
        setCoach(prev);
        setCoachErr(errorText(e));
      }
    } finally {
      if (mountedRef.current) setCoachBusy(false);
    }
  };

  const decidedStateLabel = (state: string) =>
    state === "rejected"
      ? t("result_rejected")
      : state === "superseded"
        ? t("result_withdrawn")
        : state === "expired"
          ? t("result_expired")
          : t("result_accepted");

  // FR7: same issue approved before. Give structural facts (when/who/coach verdict location), never decide for approver;
  // approver investigates recurrence: wrong location, ineffective edit, or later overwrite.
  const recurrenceTitle = (r: NonNullable<FeedbackCandidateRow["recurrence"]>): string => {
    const lines = [t("recurrence_hint")];
    if (r.decided_by_display || r.decided_by) lines.push(t("recurrence_by", { name: r.decided_by_display || r.decided_by || "" }));
    if (r.prior_verdict === "edit") lines.push(t("recurrence_prior_edit"));
    if (r.prior_verdict === "reroute") lines.push(t("recurrence_prior_reroute"));
    return lines.join("\n");
  };

  // Background casework chip (tech-design §2.8.4: no internal vocabulary). null = nothing to say.
  const caseworkLabel = (row: FeedbackCandidateRow): { text: string; tone: "info" | "muted" | "warn" } | null => {
    const cw = row.casework;
    if (!cw) return null;
    if (cw.status === "done" && cw.verdict === "edit") return { text: t("casework_drafted"), tone: "info" };
    if (cw.status === "done" && cw.verdict === "reroute") {
      const target = rerouteTargetOf(cw.reroute_target);
      return { text: target ? t("casework_reroute_to", { target: target.label }) : t("casework_reroute"), tone: "info" };
    }
    if (cw.status === "blocked" && cw.reason_code === "team_model_unconfigured") {
      return { text: viewerIsAdmin ? t("casework_blocked_model_admin") : t("casework_blocked_model_member"), tone: "warn" };
    }
    if (cw.status === "blocked" && cw.reason_code === "credit_blocked") return { text: t("casework_blocked_credit"), tone: "warn" };
    if (cw.status === "failed" && cw.reason_code === "surface_too_large") return { text: t("casework_failed_surface"), tone: "muted" };
    if (cw.status === "failed") return { text: t("casework_failed"), tone: "muted" };
    return null;
  };

  const renderPrefill = (row: FeedbackCandidateRow, result: FeedbackAdoptResult) => {
    const prefill = result.prefill;
    if (result.degraded || !prefill) {
      const reason = result.degrade_reason ?? "coach_unavailable";
      return (
        <Notice tone="info">
          {t("result_accepted")} · {t(`degrade_${reason}` as never)}
        </Notice>
      );
    }
    const body =
      prefill.kind === "agent_system_prompt" || prefill.kind === "team_skill_body"
        ? prefill.edited_content
        : prefill.kind === "space_instructions"
          ? prefill.text
          : prefill.kind === "tool_source_patch"
            ? prefill.proposal_text
            : prefill.kind === "cca_intent"
              ? prefill.purpose
              : prefill.reason;
    return (
      <div className="legacy-fbi-prefill">
        <Notice tone="success">{t("result_accepted")} · {t(`prefill_${prefill.kind}` as never)}</Notice>
        <pre className="legacy-fbi-pre">
          {body}
        </pre>
        {prefill.kind === "agent_system_prompt" || prefill.kind === "team_skill_body" || prefill.kind === "cca_intent" ? (
          <div>
            <Button size="sm" variant="primary" onClick={() => openPrefillTarget(row, result)}>
              {prefill.kind === "cca_intent" ? t("prefill_go_create") : t("prefill_open_editor")}
            </Button>
          </div>
        ) : null}
        {prefill.kind === "space_instructions" ? (
          <SpaceInstructionTargets
            text={prefill.text}
            spaces={prefill.spaces}
            prefill={{
              candidateId: row.id,
              proposer: result.banner.proposer_display || result.banner.proposer_id,
              evidenceCount: result.banner.evidence_count,
            }}
            onOpenSpaces={() => router.push("/team/settings/spaces/")}
            onAdded={(workspaceId, value) => markAdded(row.id, workspaceId, value)}
          />
        ) : null}
      </div>
    );
  };

  const renderDetail = (row: FeedbackCandidateRow) => {
    if (detailErr) return <Notice tone="error">{detailErr}</Notice>;
    if (!detail || detail.id !== row.id) {
      return <div role="status" className="legacy-fbi-detail-loading">{t("detail_loading")}</div>;
    }
    const reviewer = detail.viewer_role !== "proposer";
    return (
      <div className="legacy-fbi-detail">
        {detail.state !== "open" && outcomes[row.id]?.kind !== "rerouted" ? (
          <Notice tone="muted">{detail.decide_note?.startsWith("rerouted:") ? t("result_rerouted") : decidedStateLabel(detail.state)}</Notice>
        ) : null}
        <div>
          <div className="legacy-fbi-label">
            {t("detail_rubric")}
          </div>
          <div className="legacy-fbi-rubric">
            {detail.rubric_text || t("detail_no_rubric")}
          </div>
        </div>
        <div>
          <div className="legacy-fbi-label">
            {t("detail_evidence", { count: detail.evidence_count })}
          </div>
          <ul className="legacy-fbi-evidence">
            {detail.evidence.entries.map((e, i) => (
              <li key={`${e.contributor_id ?? "anon"}-${i}`} className="legacy-fbi-evidence-item">
                <strong className="legacy-fbi-contrib">{(e.contributor_id && detail.contributor_displays?.[e.contributor_id]) || e.contributor_id || t("detail_anonymous")}</strong>
                {e.disclosure_ts ? ` · ${formatDateYMD(e.disclosure_ts)}` : ""}
                {e.rubric_text ? `：${e.rubric_text}` : ` · ${t("detail_cosign_only")}`}
              </li>
            ))}
            {detail.evidence.others_count > 0 ? (
              <li className="legacy-fbi-evidence-more">{t("detail_others", { count: detail.evidence.others_count })}</li>
            ) : null}
          </ul>
          <div className="legacy-fbi-sources">
            {t("detail_sources", { viewable: detail.sources.viewable, total: detail.sources.total })}
          </div>
        </div>
        {reviewer && detail.decide_note ? (
          <div className="legacy-fbi-note">{t("detail_note", { note: detail.decide_note })}</div>
        ) : null}
      </div>
    );
  };

  const emptyBox = (text: string) => (
    <div className="legacy-fbi-empty">
      {text}
    </div>
  );

  return (
    <div className="legacy-fbi-page">
      <h1 className="ui-page-title legacy-feedback-inbox-title">{t("title")}</h1>
      <p className="ui-page-desc legacy-feedback-inbox-desc">{t("subtitle")}</p>
      {coach ? (
        <div data-testid="fbi-coach-bar" className="legacy-fbi-coach">
          <span>{t("coach_usage", { count: coach.month_drafts })}</span>
          <span className="legacy-fbi-spacer" />
          <label className={`legacy-fbi-coach-label${coachBusy ? " is-busy" : ""}`}>
            <span>{t("coach_toggle_label")}</span>
            <Switch
              checked={coach.casework_enabled}
              onCheckedChange={(next) => void toggleCoach(next)}
              aria-label={t("coach_toggle_label")}
              busy={coachBusy}
              className={`legacy-switch-coach${coach.casework_enabled ? " is-on" : ""}${coachBusy ? " is-busy" : ""}`}
            >
              <span className={`legacy-switch-coach-knob${coach.casework_enabled ? " is-on" : ""}`} />
            </Switch>
          </label>
          {coachErr ? <span className="legacy-fbi-coach-err">{coachErr}</span> : null}
        </div>
      ) : null}

      <SegmentedControl
        value={view}
        onValueChange={(next) => {
          setOutcomes({});
          setOpenId(null);
          setView(next);
        }}
        aria-label={t("view_label")}
        className="legacy-fbi-views"
      >
        <Segment value="open">{pendingCount !== null ? t("view_open_count", { count: pendingCount }) : t("view_open")}</Segment>
        <Segment value="decided">{t("view_decided")}</Segment>
        <Segment value="all">{t("view_all")}</Segment>
      </SegmentedControl>
      {loading ? (
        <LoadingSkeleton label={t("loading")} />
      ) : (
        <>
          {err ? <Notice tone="error">{err}</Notice> : null}
          {/* A full-page load failure offers retry (Page Layout Spec §4). */}
          {err ? (
            <div className="look-fbi-retry">
              <Button onClick={() => void load(true)}>{commonT("retry")}</Button>
            </div>
          ) : null}
          {!err && items.length === 0 ? emptyBox(view === "open" ? t("empty") : t("empty_decided")) : null}
          {items.length > 0 ? (
            <div className="legacy-fbi-list">
              {items.map((row) => {
                const isOpen = openId === row.id;
                const outcome = outcomes[row.id];
                const isBusy = busy.has(row.id);
                const canAct = row.state === "open" && !outcome;
                // Agent adoption (including retired agents) belongs to resource owner: for_my_agent is sole server signal, also for admins.
                const agentKind = row.target_kind === "agent_def" || (row.target_kind === "retire" && row.proposed?.asset_kind === "agent_def");
                const canAdopt = !row.mine && (agentKind ? Boolean(row.for_my_agent) : viewerIsAdmin);
                return (
                  <div key={row.id} data-testid={`fbi-row-${row.id}`} className={`legacy-fbi-row${isOpen ? " is-open" : ""}`}>
                    <div className="legacy-fbi-row-head">
                      <span className="legacy-fbi-kind">
                        {kindLabel[row.target_kind] ?? row.target_kind}
                      </span>
                      <span className="legacy-fbi-target">{row.target_label || row.target_ref}</span>
                      {row.mine ? <span className="legacy-fbi-flag">{t("badge_mine")}</span> : null}
                      {row.for_my_agent ? <span className="legacy-fbi-flag">{t("badge_for_my_agent")}</span> : null}
                      {row.recurrence ? (
                        <span
                          data-testid={`fbi-recurrence-${row.id}`}
                          className="legacy-fbi-chip is-warn"
                          title={recurrenceTitle(row.recurrence)}
                        >
                          {typeof row.recurrence.days_ago !== "number"
                            ? t("recurrence_badge_unknown")
                            : row.recurrence.days_ago === 0
                              ? t("recurrence_badge_today")
                              : t("recurrence_badge", { days: row.recurrence.days_ago })}
                        </span>
                      ) : null}
                      {(() => {
                        const cl = caseworkLabel(row);
                        if (!cl) return null;
                        return (
                          <span data-testid={`fbi-casework-${row.id}`} className={`legacy-fbi-chip is-${cl.tone}`}>
                            {cl.text}
                          </span>
                        );
                      })()}
                      <span className="legacy-fbi-spacer" />
                      {row.state === "open" ? (
                        <span className="legacy-fbi-expires">{t("expires", { date: formatDateYMD(row.expires_at) })}</span>
                      ) : (
                        <span className="legacy-fbi-expires">
                          {decidedStateLabel(row.state)}
                          {row.decided_at ? ` · ${formatDateYMD(row.decided_at)}` : ""}
                        </span>
                      )}
                    </div>
                    <div className="legacy-fbi-text">{proposedText(row) || t("no_text")}</div>
                    <div className="legacy-fbi-meta">
                      <span>{t("evidence_count", { count: row.evidence_count })}</span>
                      {(() => {
                        const prefill = outcome?.kind === "adopted" ? outcome.result.prefill : null;
                        const spaces = prefill?.kind === "space_instructions" ? prefill.spaces : row.space_targets?.spaces;
                        const progress = spaces ? addedProgress(spaces) : null;
                        return progress ? <span className="legacy-fbi-progress">{t("added_progress", progress)}</span> : null;
                      })()}
                      {viewerIsAdmin && row.proposer_id ? <span>{t("proposer", { id: row.proposer_display || row.proposer_id })}</span> : null}
                      <button type="button" onClick={() => void toggleDetail(row.id)} aria-expanded={isOpen} className="legacy-fbi-toggle">
                        {isOpen ? t("detail_hide") : t("detail_show")}
                      </button>
                    </div>
                    {isOpen ? renderDetail(row) : null}
                    {actionErr[row.id] ? <div className="legacy-fbi-mt10"><Notice tone="error">{actionErr[row.id]}</Notice></div> : null}
                    {outcome?.kind === "decided" ? <div className="legacy-fbi-mt10"><Notice tone="muted">{decidedStateLabel(outcome.state)}</Notice></div> : null}
                    {outcome?.kind === "adopted" ? <div className="legacy-fbi-mt10">{renderPrefill(row, outcome.result)}</div> : null}
                    {row.state === "accepted" && row.space_targets && !outcome ? (
                      <div className="legacy-fbi-mt10">
                        <SpaceInstructionTargets
                          text={row.space_targets.text}
                          spaces={row.space_targets.spaces}
                          prefill={{ candidateId: row.id, proposer: row.proposer_display || row.proposer_id || "", evidenceCount: row.evidence_count }}
                          onOpenSpaces={() => router.push("/team/settings/spaces/")}
                          onAdded={(workspaceId, value) => markRowAdded(row.id, workspaceId, value)}
                        />
                      </div>
                    ) : null}
                    {outcome?.kind === "rerouted" ? <div className="legacy-fbi-mt10"><Notice tone="success">{t("reroute_done", { target: outcome.label })}</Notice></div> : null}
                    {outcome?.kind === "reroute" ? (() => {
                      const target = rerouteTargetOf(outcome.result.reroute?.target);
                      const canReroute = viewerIsAdmin || Boolean(row.for_my_agent);
                      return (
                        <div className="legacy-fbi-reroute">
                          <div className="legacy-fbi-reroute-title">{t("reroute_title")}</div>
                          <div className="legacy-fbi-reroute-reason">{outcome.result.reroute?.reason}</div>
                          <div className="legacy-fbi-reroute-hint">{target ? t("reroute_hint_target", { target: target.label }) : t("reroute_hint")}</div>
                          {target && canReroute ? (
                            <div className="legacy-fbi-mt8">
                              <Button size="sm" variant="primary" disabled={isBusy} onClick={() => void reroute(row, target)}>
                                {isBusy ? t("action_processing") : t("action_reroute", { target: target.label })}
                              </Button>
                            </div>
                          ) : null}
                        </div>
                      );
                    })() : null}
                    {canAct || outcome?.kind === "reroute" ? (
                      <div className="legacy-fbi-actions">
                        {rejecting === row.id ? (
                          <div className="legacy-fbi-reject">
                            <textarea className="settings-input" rows={2} value={rejectNote} aria-label={t("reject_note_placeholder")} placeholder={t("reject_note_placeholder")} onChange={(e) => setRejectNote(e.target.value)} />
                            <div className="legacy-fbi-btnrow">
                              <Button size="sm" variant="ghost" disabled={isBusy} onClick={() => { setRejecting(null); setRejectNote(""); }}>{t("reject_cancel")}</Button>
                              <Button size="sm" variant="danger" disabled={isBusy} onClick={() => void reject(row.id)}>{isBusy ? t("action_processing") : t("reject_confirm")}</Button>
                            </div>
                          </div>
                        ) : (
                          <div className="legacy-fbi-btnrow is-wrap">
                            {row.mine ? (
                              <Button size="sm" variant="secondary" disabled={isBusy} onClick={() => void withdraw(row.id)}>{t("action_withdraw")}</Button>
                            ) : null}
                            {viewerIsAdmin && !row.mine ? (
                              <Button size="sm" variant="secondary" disabled={isBusy} onClick={() => setRejecting(row.id)}>{t("action_reject")}</Button>
                            ) : null}
                            {(() => {
                              // Coach already chose reroute with available target: approver transfers directly without first adopting to see reroute card.
                              const target = row.casework?.status === "done" && row.casework.verdict === "reroute" ? rerouteTargetOf(row.casework.reroute_target) : null;
                              const canReroute = !row.mine && (viewerIsAdmin || Boolean(row.for_my_agent));
                              return target && canReroute ? (
                                <Button size="sm" variant="secondary" disabled={isBusy} onClick={() => void reroute(row, target)}>{t("action_reroute", { target: target.label })}</Button>
                              ) : null;
                            })()}
                            {canAdopt ? (
                              <Button size="sm" variant="primary" disabled={isBusy} onClick={() => void adopt(row)}>{isBusy ? t("action_processing") : t("action_adopt")}</Button>
                            ) : null}
                          </div>
                        )}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
