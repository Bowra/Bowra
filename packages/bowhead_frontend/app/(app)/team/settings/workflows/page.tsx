"use client";

/**
 * /settings/workflows — team workflows (shared reusable recipe management).
 *
 * G12 consolidation (2026-07-03, docs/skills-tools-ssot.md §5-G12, user chose A + implement without design):
 * workflow is fundamentally skill; original personal/team tabs duplicated personal data already reached through
 * /skills “对话生成” group (and personal rows
 * never had dry-run verdict, a team-skill field) → remove personal tab, manage personal solidified
 * recipes on skills page; header hint points there.
 *
 * Data = listTeamSkills() (.data/team_skills/<team_id>/*; flag off → 404 silently treated as empty).
 * Actions (wired; high-impact protection first previews server impact, no optimistic toggle):
 *   - Enable/disable: patchTeamSkill(status enabled|disabled).
 *   - Delete: deleteTeamSkill(id) (also removes on-disk SKILL.md+scripts).
 *   - Share: ShareDialog(kind=team_skill) (content-permissions).
 *
 * Whole-page gate: /me capabilities.workflow_creator (= sole BOWHEAD_CCA_WORKFLOW_CREATOR switch);
 * off shows “功能未开启”, not empty list, avoiding implication nothing ever solidified.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import {
  approveTeamSkill,
  deleteTeamSkill,
  getTeamSkill,
  listTeamSkills,
  patchTeamSkill,
  previewTeamSkillImpact,
  rejectTeamSkill,
  type ApprovalCasKeys,
  type TeamSkillDetail,
  type TeamSkillPublic,
} from "@/lib/workflow-solidify-api";
import { teamSkillOwnerName } from "@/lib/team-skill-owner";
import type { SkillResourceChangeImpact } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import type { ErrorPresentation } from "@/lib/error-presentation";
import {
  isWorkflowCreatorDisabled,
  skillWorkflowErrorPresentation,
} from "@/lib/skill-workflow-error";
import { fetchMe } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { SettingsShell } from "@/components/settings/settings-shell";
import { ErrorNotice } from "@/components/error-notice";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { formatDateYMD, formatByteSize } from "@/lib/format";
import { FormBadge, VerdictBadge, VisBadge } from "@/components/workflow-solidify/wf-shared";
import { ShareDialog } from "@/components/share/share-dialog";
import { ShAudienceBadge } from "@/components/share/share-ui";
import type { Audience } from "@/lib/content-share-api";
import { randomUuid } from "@/lib/random-id";

type Row = {
  key: string;
  displayName: string;
  name: string;
  description: string;
  verdict: string | null;
  enabled: boolean;
  sizeBytes: number;
  updated: string | null;
  visibility?: string;
  by?: string;
  teamId: string; // patch/delete key
  // content-permissions: team-skill sharing state.
  audience?: Audience;
  shareCount?: number;
  sharedBy?: string | null;
  // skill-team-governance: approval state (suppress audience badge for pending rows, review P2-2).
  approval: string;
  candidateState?: string | null;
  raw: TeamSkillPublic;
};

type PendingImpact = {
  row: Row;
  action: "disable" | "delete";
  impact: SkillResourceChangeImpact;
  idempotencyKey: string;
};

function newIdempotencyKey(): string {
  return randomUuid();
}

function ImpactDescription({ pending }: { pending: PendingImpact }) {
  const t = useTranslations("settings.workflows");
  const summary = pending.impact.summary;
  return (
    <div style={{ display: "grid", gap: 10 }}>
      <span>{t(pending.action === "delete" ? "impact_delete_lead" : "impact_disable_lead")}</span>
      <span style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <span>{t("impact_current_agents", { n: summary.current_agent_count })}</span>
        <span>{t("impact_draft_agents", { n: summary.draft_agent_count })}</span>
        <span>{t("impact_running_tasks", { n: summary.running_task_count })}</span>
        <span>{t("impact_schedules", { n: summary.scheduled_binding_count })}</span>
      </span>
      <span>{t("impact_runtime_note")}</span>
      {pending.impact.required_follow_up.length ? <span>{t("impact_schedules_note")}</span> : null}
    </div>
  );
}

function teamToRow(s: TeamSkillPublic, ownerFallback: string): Row {
  const owner = teamSkillOwnerName(s, ownerFallback);
  return {
    key: `o:${s.id}`,
    displayName: s.display_name || s.name,
    name: s.name,
    description: s.description,
    verdict: s.verdict,
    enabled: s.status !== "disabled",
    sizeBytes: s.size_bytes,
    updated: s.updated_at ?? s.created_at ?? null,
    visibility: s.visibility,
    by: owner,
    teamId: s.id,
    audience: s.audience,
    shareCount: s.share_count,
    // shared_by is the creator's raw id (set only when someone else shared it with me).
    sharedBy: s.shared_by ? owner : null,
    approval: s.approval || "approved",
    candidateState: s.candidate_state ?? null,
    raw: s,
  };
}

/* ───────── approval (skill-team-governance P3, PRD §3.6 C) ───────── */

function ApprovalStateBadge({ state }: { state: "pending" | "rejected" | "update" }) {
  const t = useTranslations("settings.workflows");
  const label = {
    pending: t("badge_approval_pending"),
    rejected: t("badge_approval_rejected"),
    update: t("badge_candidate_pending"),
  }[state];
  return (
    <span className={`legacy-ai-wf-state ${state === "rejected" ? "is-danger ui-badge--danger" : "is-warn ui-badge--warning"} ui-badge`}>
      {label}
    </span>
  );
}

function riskChips(src: Pick<TeamSkillPublic, "risk_report">): string[] {
  const rr = src.risk_report;
  if (!rr || typeof rr !== "object") return [];
  const chips: string[] = [];
  const langs = rr.langs;
  if (langs && typeof langs === "object") {
    for (const [ext, n] of Object.entries(langs)) chips.push(`${ext}×${n}`);
  }
  for (const kw of rr.keywords ?? []) chips.push(String(kw));
  if ((rr.shebangs ?? []).length) chips.push(`shebang×${(rr.shebangs ?? []).length}`);
  return chips.slice(0, 12);
}

function ApprovalCard({ row, isAdmin, onDecided }: { row: TeamSkillPublic; isAdmin: boolean; onDecided: () => void }) {
  const t = useTranslations("settings.workflows");
  const te = useTranslations("errors");
  const ownerFallback = useTranslations("agents_marketplace")("team_member");
  const presentError = useCallback(
    (error: unknown) =>
      skillWorkflowErrorPresentation(error, {
        surface: "settings",
        copy: {
          generic: t("error_generic"),
          transport: {
            requestTimedOut: te("transport.requestTimedOut"),
            serviceUnreachable: te("transport.serviceUnreachable"),
            retry: te("transport.retry"),
          },
        },
      }),
    [t, te],
  );
  const isUpdate = row.candidate_state === "pending"; // L2 update candidate versus L1 first submission.
  const [detail, setDetail] = useState<TeamSkillDetail | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ErrorPresentation | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");

  // CAS keys (PRD §3.2: admin approves that specific content); 409 = author overwrote, refresh and review again.
  const keys: ApprovalCasKeys = isUpdate
    ? { candidate_submitted_at: row.candidate_submitted_at, candidate_sha256: row.candidate_sha256 }
    : { source_sha256: row.source_sha256 };

  const toggleExpand = useCallback(() => {
    setExpanded((e) => !e);
    if (!detail) {
      getTeamSkill(row.id)
        .then(setDetail)
        .catch((e) => setErr(presentError(e)));
    }
  }, [detail, presentError, row.id]);

  const run = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setErr(null);
      try {
        await fn();
        onDecided();
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          // Only CAS update collision means “内容已更新”; other 409 (candidate rejected/disk damaged/nothing approvable) directly show server
          // wording to avoid misleading (review P3). Both refresh latest row state.
          const presentation = presentError(e);
          setErr(
            e.code === "E_SKILL_CANDIDATE_STALE"
              ? { ...presentation, message: t("approval_stale") }
              : presentation,
          );
          onDecided();
        } else {
          setErr(presentError(e));
        }
      } finally {
        setBusy(false);
      }
    },
    [onDecided, presentError, t],
  );

  const submittedAt = row.candidate_submitted_at ?? row.updated_at ?? row.created_at ?? null;
  const previewMd = isUpdate ? (detail?.candidate_body_md ?? detail?.body_md) : detail?.body_md;
  // Risk chips visible while collapsed (review P2: admin might approve without expanding); list row already includes risk_report.
  // An update is approved as its candidate, so its chips come from the candidate rather than the live content.
  const reviewed = detail ?? row;
  const chips = riskChips(isUpdate ? { risk_report: reviewed.candidate_risk_report ?? null } : reviewed);
  return (
    <div
      className="legacy-ai-wf-approval ai-wf-card is-approval"
      data-testid={`wf-approval-${row.name}`}
    >
      <div className="legacy-ai-sa-chips">
        <ApprovalStateBadge state={isUpdate ? "update" : "pending"} />
        {row.verdict ? <VerdictBadge verdict={row.verdict} /> : null}
        {chips.map((c) => (
          <span key={c} className="bh-mono legacy-ai-wf-risk ui-badge ui-badge--outline">
            {c}
          </span>
        ))}
      </div>
      <div className="legacy-ai-sa-title-row is-tight ai-sa-title-row">
        <h3 className="legacy-ai-sa-name is-approval ai-sa-name">{row.display_name || row.name}</h3>
        <code className="bh-mono legacy-ai-sa-id ai-sa-id">{row.name}</code>
        <span className="legacy-ai-wf-by ai-wf-by">
          {t("approval_meta", {
            name: teamSkillOwnerName(row, ownerFallback),
            date: submittedAt ? formatDateYMD(submittedAt) : "",
          })}
        </span>
      </div>
      {row.description ? (
        <p className="legacy-ai-sa-desc is-tight ai-sa-desc">{row.description}</p>
      ) : null}
      <div className="legacy-ai-wf-preview">
        <button
          type="button"
          onClick={toggleExpand}
          className="legacy-ai-wf-link ai-wf-link"
        >
          {expanded ? t("approval_collapse") : t("approval_expand")}
        </button>
        {expanded ? (
          previewMd === undefined ? (
            <div className="legacy-ai-wf-preview-loading">{t("loading")}</div>
          ) : (
            <pre className="legacy-ai-wf-pre ai-wf-pre">
              {previewMd || t("approval_body_empty")}
            </pre>
          )
        ) : null}
      </div>
      {err ? (
        <div className="legacy-ai-mt8">
          <ErrorNotice
            message={err.message}
            action={err.action}
            diagnostics={err.diagnostics}
            surface="settings"
            onAction={onDecided}
            diagnosticsEnabled
          />
        </div>
      ) : null}
      <div className="legacy-ai-wf-decide ai-wf-decide">
        <div className="legacy-ai-flex1" />
        {isAdmin ? (
          <>
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => setRejectOpen(true)}>
              {t("approval_reject")}
            </Button>
            <Button size="sm" disabled={busy} onClick={() => void run(() => approveTeamSkill(row.id, keys))}>
              {t("approval_approve")}
            </Button>
          </>
        ) : (
          <span className="legacy-ai-wf-hint ai-wf-by">{t("approval_pending_hint")}</span>
        )}
      </div>

      {rejectOpen ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={t("approval_reject_title", { name: row.display_name || row.name })}
          style={{
            position: "fixed", inset: 0, zIndex: 80, display: "grid", placeItems: "center",
            background: "rgba(22,19,16,0.35)",
          }}
          onClick={(e) => {
            if (e.target === e.currentTarget) setRejectOpen(false);
          }}
        >
          <div style={{
            width: 420, maxWidth: "92vw", background: "var(--c-surface)", borderRadius: 14,
            border: "1px solid var(--c-line)", padding: "18px 20px",
            boxShadow: "0 18px 50px -12px rgba(22,19,16,0.25)",
          }}>
            <h3 style={{ margin: 0, fontSize: "0.9375rem", color: "var(--c-ink)" }}>
              {t("approval_reject_title", { name: row.display_name || row.name })}
            </h3>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={t("approval_reject_placeholder")}
              rows={3}
              style={{
                width: "100%", marginTop: 12, resize: "vertical", fontSize: "0.8125rem", lineHeight: 1.5,
                padding: "8px 10px", borderRadius: 8, border: "1px solid var(--c-line)",
                background: "var(--c-bg)", color: "var(--c-ink)", fontFamily: "inherit",
              }}
            />
            <div style={{ display: "flex", gap: 10, marginTop: 14, justifyContent: "flex-end" }}>
              <Button size="sm" variant="ghost" onClick={() => setRejectOpen(false)}>
                {t("approval_cancel")}
              </Button>
              <Button
                size="sm"
                disabled={busy || !reason.trim()}
                onClick={() => {
                  setRejectOpen(false);
                  void run(() => rejectTeamSkill(row.id, keys, reason.trim()));
                }}
              >
                {t("approval_reject_confirm")}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function RecipeRow({
  r,
  onToggle,
  onDelete,
  onShare,
}: {
  r: Row;
  onToggle: (r: Row, enabled: boolean) => void;
  onDelete: (r: Row) => void;
  onShare: (r: Row) => void;
}) {
  const t = useTranslations("settings.workflows");
  return (
    <div
      className={`legacy-ai-sa-card ai-sa-card ai-wf-card${r.enabled ? "" : " is-off"}`}
      data-testid={`wf-row-team-${r.name}`}
    >
      <div className="legacy-ai-sa-chips">
        <FormBadge form="team-skill" />
        {/* P1b review P2-2: nonapproved rows suppress audience badge (visibility=team_wide awaiting approval,
            not broadcast), show approval badge; live rows with candidate under review add “更新审批中” badge. */}
        {r.approval !== "approved" ? (
          <ApprovalStateBadge state={r.approval === "rejected" ? "rejected" : "pending"} />
        ) : r.audience ? (
          <ShAudienceBadge level={r.audience} n={r.shareCount} />
        ) : r.visibility ? (
          <VisBadge vis={r.visibility} />
        ) : null}
        {r.approval === "approved" && r.candidateState === "pending" ? <ApprovalStateBadge state="update" /> : null}
        {r.sharedBy ? (
          <span className="legacy-ai-wf-shared ai-wf-by">{t("shared_by", { name: r.sharedBy })}</span>
        ) : null}
        {r.verdict ? <VerdictBadge verdict={r.verdict} /> : null}
        {!r.enabled ? (
          <span className="legacy-ai-wf-off ai-sa-chip is-muted">
            {t("status_disabled")}
          </span>
        ) : null}
      </div>

      <div className="legacy-ai-sa-title-row ai-sa-title-row">
        <h3 className="legacy-ai-sa-name ai-sa-name">{r.displayName}</h3>
        <code className="bh-mono legacy-ai-sa-id ai-sa-id">
          {r.name}
        </code>
      </div>

      <p className="legacy-ai-sa-desc ai-sa-desc">
        {r.description}
      </p>

      <div className="legacy-ai-sa-foot ai-sa-foot">
        <span className="bh-mono legacy-ai-sa-meta ai-sa-meta">
          {(r.by ? `${r.by} · ` : "") +
            (r.sizeBytes ? `${formatByteSize(r.sizeBytes)} · ` : "") +
            (r.updated ? t("row_updated", { date: formatDateYMD(r.updated) }) : "")}
        </span>
        <div className="legacy-ai-flex1" />
        <RowActionsMenu
          label={t("more_actions_for", { name: r.displayName || r.name })}
          actions={[
            ...(r.approval === "approved" ? [{ id: "share", label: t("action_share"), onSelect: () => onShare(r) }] : []),
            { id: "toggle", label: r.enabled ? t("action_disable") : t("action_enable"), onSelect: () => onToggle(r, !r.enabled) },
            { id: "delete", label: t("action_delete"), danger: true, onSelect: () => onDelete(r) },
          ]}
        />
        {/* Unapproved rows omit sharing entry (server set_audience gate also 422; consolidated entry prevents accidental action). */}
      </div>
    </div>
  );
}

function EmptyState() {
  const t = useTranslations("settings.workflows");
  return (
    <div className="legacy-ai-wf-empty ai-wf-empty">
      <h3 className="legacy-ai-sa-empty-title ai-wf-empty-title">
        {t("empty_title_team")}
      </h3>
      <p className="legacy-ai-wf-empty-body ai-wf-empty-body">
        {t("empty_body_team")}
      </p>
    </div>
  );
}

export default function WorkflowsSettingsPage() {
  const t = useTranslations("settings.workflows");
  const te = useTranslations("errors");
  const ownerFallback = useTranslations("agents_marketplace")("team_member");
  const presentError = useCallback(
    (failure: unknown) =>
      skillWorkflowErrorPresentation(failure, {
        surface: "settings",
        copy: {
          generic: t("error_generic"),
          transport: {
            requestTimedOut: te("transport.requestTimedOut"),
            serviceUnreachable: te("transport.serviceUnreachable"),
            retry: te("transport.retry"),
          },
        },
      }),
    [t, te],
  );
  const [rows, setRows] = useState<TeamSkillPublic[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [impactGuard, setImpactGuard] = useState(false);
  const [enabledFlag, setEnabledFlag] = useState<boolean | null>(null); // null = unknown (loading).
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorPresentation | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Row | null>(null);
  const [shareTarget, setShareTarget] = useState<Row | null>(null); // content-permissions: team_skill sharing-dialog target.
  const [pendingImpact, setPendingImpact] = useState<PendingImpact | null>(null);
  const [deleting, setDeleting] = useState(false); // P2 (review): deletion in flight prevents double clicks (disables confirm button).
  const deletingRef = useRef(false); // Synchronous guard: state disables button next render, same-tick double click bypasses if(deleting).

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const me = await fetchMe();
      const on = Boolean(me.capabilities?.workflow_creator);
      setEnabledFlag(on);
      setIsAdmin(Boolean(me.is_team_admin));
      if (!on) {
        setRows([]);
        setError(null);
        return;
      }
      const teamRes = await listTeamSkills();
      setRows(teamRes.items ?? []);
      setImpactGuard(Boolean(teamRes.impact_guard_enabled));
      setError(null);
    } catch (e) {
      if (isWorkflowCreatorDisabled(e)) {
        setEnabledFlag(false);
        setRows([]);
        setError(null);
      } else {
        setError(presentError(e));
      }
    } finally {
      setBusy(false);
    }
  }, [presentError]);

  // Approval section (PRD §3.6 C): L1 first-submission pending + L2 update-candidate pending; L1 pending absent from main list
  // (not live asset yet); L2 live row remains in main list (team keeps using old approved version during review).
  const pendingRows = rows.filter((r) => r.approval === "pending" || r.candidate_state === "pending");
  const team: Row[] = rows
    .filter((r) => (r.approval || "approved") !== "pending")
    .map((r) => teamToRow(r, ownerFallback));

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openImpactConfirmation = async (r: Row, action: PendingImpact["action"]) => {
    try {
      const impact = await previewTeamSkillImpact(r.teamId, {
        action: action === "delete" ? "delete" : "patch",
        proposed_change: action === "disable" ? { status: "disabled" } : {},
      });
      if (impact.blocking || !impact.impact_token) {
        setError({
          message: action === "delete" && impact.reason_codes.includes("resource_delete_has_live_dependencies")
            ? t("impact_delete_blocked")
            : t("impact_unavailable"),
        });
        return false;
      }
      setPendingImpact({
        row: r,
        action,
        impact,
        idempotencyKey: newIdempotencyKey(),
      });
      setError(null);
      return true;
    } catch (e) {
      setError(presentError(e));
      return false;
    }
  };

  const onToggle = async (r: Row, enabled: boolean) => {
    if (!enabled && impactGuard) {
      await openImpactConfirmation(r, "disable");
      return;
    }
    // Optimistic toggle (team derived from rows → change source).
    setRows((xs) => xs.map((x) => (x.id === r.teamId ? { ...x, status: enabled ? "enabled" : "disabled" } : x)));
    try {
      await patchTeamSkill(r.teamId, { status: enabled ? "enabled" : "disabled" });
    } catch (e) {
      await refresh(); // Roll back.
      setError(presentError(e));
    }
  };

  const performDelete = async (r: Row) => {
    if (deletingRef.current) return; // P2: prevent double click (ref synchronous, no next-render wait; confirm button additionally disabled by busy).
    deletingRef.current = true;
    setDeleting(true);
    try {
      if (impactGuard) {
        setDeleteTarget(null);
        await openImpactConfirmation(r, "delete");
        return;
      }
      await deleteTeamSkill(r.teamId);
      setDeleteTarget(null);
      await refresh();
    } catch (e) {
      setError(presentError(e));
      setDeleteTarget(null);
    } finally {
      deletingRef.current = false;
      setDeleting(false);
    }
  };

  const confirmImpact = async () => {
    const impactToken = pendingImpact?.impact.impact_token;
    if (!pendingImpact || !impactToken) return;
    setBusy(true);
    try {
      const guard = {
        impactToken,
        idempotencyKey: pendingImpact.idempotencyKey,
      };
      if (pendingImpact.action === "delete") {
        await deleteTeamSkill(pendingImpact.row.teamId, guard);
      } else {
        await patchTeamSkill(pendingImpact.row.teamId, { status: "disabled" }, guard);
      }
      setPendingImpact(null);
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const stale = pendingImpact;
        setPendingImpact(null);
        const refreshed = await openImpactConfirmation(stale.row, stale.action);
        if (refreshed) setError({ message: t("impact_changed") });
      } else {
        setError(presentError(e));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsShell active="workflows" title={t("title")}>
      <div className="legacy-ai-tools-root ai-tools">

        <header className="ui-page-head legacy-workflows-head">
          <h1 className="ui-page-title legacy-workflows-title">{t("title")}</h1>
          <p className="ui-page-desc legacy-workflows-desc">
            {t("subtitle")}{" "}
            <a href={appPath("/skills/?scope=mine")} className="legacy-ai-wf-accent ai-wf-link">
              {t("personal_hint_link")}
            </a>
          </p>
        </header>

        {error ? (
          <ErrorNotice
            message={error.message}
            action={error.action}
            diagnostics={error.diagnostics}
            surface="settings"
            onAction={() => void refresh()}
            diagnosticsEnabled
          />
        ) : null}

        {enabledFlag === false ? (
          <Notice tone="muted">
            {t("disabled_notice")}
          </Notice>
        ) : busy && enabledFlag === null ? (
          <LoadingSkeleton label={t("loading")} />
        ) : enabledFlag ? (
          <>
            {pendingRows.length > 0 ? (
              <section className="legacy-ai-wf-approvals ai-wf-approvals" data-testid="wf-approval-section">
                <h2 className="legacy-ai-wf-h2 ui-section-title ai-section-title">
                  {t("approval_section_title", { count: pendingRows.length })}
                </h2>
                <div className="legacy-ai-ds-list ai-card-list">
                  {pendingRows.map((r) => (
                    <ApprovalCard key={r.id} row={r} isAdmin={isAdmin} onDecided={() => void refresh()} />
                  ))}
                </div>
              </section>
            ) : null}

            <div className="legacy-ai-sa-toolbar is-wrap ai-count-row">
              <div className="legacy-ai-flex1" />
              <span className="bh-mono legacy-ai-sa-count ai-count">
                {t("count", { count: team.length })}
              </span>
            </div>

            {team.length === 0 ? (
              <EmptyState />
            ) : (
              <div className="legacy-ai-ds-list ai-card-list">
                {team.map((r) => (
                  <RecipeRow
                    key={r.key}
                    r={r}
                    onToggle={onToggle}
                    onDelete={(row) => {
                      if (impactGuard) void performDelete(row);
                      else setDeleteTarget(row);
                    }}
                    onShare={setShareTarget}
                  />
                ))}
              </div>
            )}
          </>
        ) : null}

        {shareTarget ? (
          <ShareDialog
            kind="team_skill"
            resourceId={shareTarget.teamId}
            label={shareTarget.displayName || shareTarget.name}
            // Broadcast locked: rows other than team_wide+approved always get 422 from put_audience(team_wide)
            // (visibility change requires approval state machine); entry retains only idempotent selection of approved broadcast state.
            teamWideLocked={!(shareTarget.approval === "approved" && shareTarget.visibility === "team_wide")}
            onClose={() => setShareTarget(null)}
            onChanged={() => { void refresh(); }}
          />
        ) : null}

        {deleteTarget ? (
          <ConfirmDialog
            title={t("delete_title")}
            description={t("delete_description", { label: deleteTarget.displayName, name: deleteTarget.name })}
            confirmLabel={t("delete_confirm")}
            cancelLabel={t("delete_cancel")}
            busy={busy || deleting}
            onCancel={() => setDeleteTarget(null)}
            onConfirm={() => void performDelete(deleteTarget)}
          />
        ) : null}

        {pendingImpact ? (
          <ConfirmDialog
            title={t(
              pendingImpact.action === "delete" ? "impact_delete_title" : "impact_disable_title",
              { name: pendingImpact.row.displayName },
            )}
            description={<ImpactDescription pending={pendingImpact} />}
            confirmLabel={t(pendingImpact.action === "delete" ? "delete_confirm" : "impact_disable_confirm")}
            cancelLabel={t("delete_cancel")}
            busy={busy}
            onCancel={() => setPendingImpact(null)}
            onConfirm={() => void confirmImpact()}
          />
        ) : null}
      </div>
    </SettingsShell>
  );
}
