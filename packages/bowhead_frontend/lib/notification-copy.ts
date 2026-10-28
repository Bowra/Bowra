// Backend snapshots notification title/body in Chinese (Agent release/evaluation uses English);
// bell and home Needs Attention directly render them. Rewrite by type + metadata in current language;
// legacy rows missing parameters (names/labels absent from metadata) fall back per field to persisted text.

type Translate = (key: string, values?: Record<string, string | number>) => string;

export interface NotificationCopySource {
  type: string;
  category?: string | null;
  title: string;
  body: string | null;
  subjectType?: string | null;
  actionUrl?: string | null;
  metadata?: Record<string, unknown> | null;
}

export interface NotificationCopy {
  title: string;
  body: string | null;
}

function str(meta: Record<string, unknown>, key: string): string | null {
  const value = meta[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function num(meta: Record<string, unknown>, key: string): number | null {
  const value = meta[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatMinute(value: string | null, locale: string): string | null {
  if (!value) return null;
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return null;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(ms);
}

const AGENT_RELEASE_TYPES = new Set([
  "agent.release.approval_required",
  "agent.release.rejected",
  "agent.release.withdrawn",
  "agent.release.stale",
  "agent.release.updated",
  "agent.release.high_risk",
  "agent.release.audience_contracted",
]);

// Owner version notifications: outbox event → copy key. The result type splits by readiness.
const AGENT_VERSION_NOTICES: Record<string, string> = {
  "agent.version.proposal_received": "proposal",
  "agent.version.candidate_prepared": "prepared",
  "agent.version.result_ready": "ready",
  "agent.version.result_blocked": "blocked",
  "agent.version.result_unknown": "unknown",
  "agent.version.candidate_stale": "stale",
};

function agentVersionCopy(
  item: NotificationCopySource,
  meta: Record<string, unknown>,
  c: (key: string, values?: Record<string, string | number>) => string,
): NotificationCopy | null {
  const base = AGENT_VERSION_NOTICES[item.type];
  const name = str(meta, "agentName");
  if (!base || !name) return null;
  const key = base === "ready" && str(meta, "readiness") === "ready_with_risks" ? "ready_risks" : base;
  const groups = num(meta, "groupCount");
  const reasons = num(meta, "reasonCount");
  const body = key === "proposal"
    ? groups !== null ? c("agent_version_notice_proposal_body", { count: groups }) : item.body
    : key === "blocked" && reasons
      ? c("agent_version_notice_blocked_body_count", { count: reasons })
      : c(`agent_version_notice_${key}_body`);
  return { title: c(`agent_version_notice_${key}`, { name }), body };
}

const PAUSED_REASONS = new Set(["recipe_lost", "recipe_changed", "recipe_rejected"]);

const FEEDBACK_STATES = new Set(["accepted", "rejected", "expired"]);
const FEEDBACK_KINDS = new Set(["agent_def", "team_skill", "semantic", "tool_source", "new_asset", "retire"]);

export function localizeNotification(item: NotificationCopySource, t: Translate, locale: string): NotificationCopy {
  const meta = item.metadata ?? {};
  const raw: NotificationCopy = { title: item.title, body: item.body };
  const c = (key: string, values?: Record<string, string | number>) => t(`copy.${key}`, values);
  const tool = str(meta, "toolName");
  const risk = str(meta, "riskLevel");
  const jobName = str(meta, "jobName");

  switch (item.type) {
    case "memory.candidate": {
      const expiring = num(meta, "expiringCount");
      if (expiring !== null) {
        return { title: c("memory_expiring_title"), body: c("memory_expiring_body", { count: expiring }) };
      }
      const created = num(meta, "createdCount");
      if (created === null) return { title: c("memory_new_title"), body: raw.body };
      const preview = str(meta, "preview");
      return {
        title: c("memory_new_title"),
        body: preview
          ? c("memory_new_body_preview", { count: created, preview })
          : c("memory_new_body", { count: created }),
      };
    }
    case "space_assignment_requested":
      return { title: c("space_requested_title"), body: c("space_requested_body") };
    case "space_assignment_ready":
      return { title: c("space_ready_title"), body: c("space_ready_body") };
    case "approval.required":
      return {
        title: c("approval_required_title"),
        body: tool && risk ? c("approval_required_body", { tool, risk }) : raw.body,
      };
    case "approval.submitted":
      return { title: c("approval_submitted_title"), body: tool ? c("approval_submitted_body", { tool }) : raw.body };
    case "approval.approved":
      return { title: c("approval_approved_title"), body: tool ? c("approval_approved_body", { tool }) : raw.body };
    case "approval.rejected":
      return { title: c("approval_rejected_title"), body: tool ? c("approval_rejected_body", { tool }) : raw.body };
    case "approval.executed":
      return { title: c("approval_executed_title"), body: tool ? c("approval_executed_body", { tool }) : raw.body };
    case "approval.execution_failed":
      // Body is raw execution error (system output, not product copy); preserve unchanged.
      return { title: c("approval_execution_failed_title"), body: raw.body };
    case "access_requested": {
      const who = str(meta, "requesterName");
      const label = str(meta, "resourceLabel");
      return {
        title: c("access_requested_title"),
        body: who && label ? c("access_requested_body", { who, label }) : raw.body,
      };
    }
    case "access_request_expired":
      // Neutral for every cause (expired, deleted, approval that did not take effect).
      return { title: c("access_request_expired_title"), body: null };
    case "access_request_decided": {
      const outcome = str(meta, "outcome") ?? (item.actionUrl ? "approved" : null);
      // Access-request expansion rows (decidedBy=manager) never reveal why a request ended
      // or who decided; they carry the manager's note verbatim.
      if (str(meta, "decidedBy") === "manager") {
        if (outcome === "stale" || outcome === null) return { title: c("access_request_expired_title"), body: null };
        if (outcome !== "approved" && outcome !== "approved_pending" && outcome !== "rejected") return raw;
        const note = str(meta, "note");
        return {
          title: c(`access_decided_${outcome}_title`),
          body: note
            ? c("access_decided_note_body", { note })
            : outcome === "approved" && item.actionUrl
              ? c("access_decided_approved_body")
              : null,
        };
      }
      if (outcome !== "approved" && outcome !== "rejected" && outcome !== "stale") return raw;
      return { title: c(`access_decided_${outcome}_title`), body: null };
    }
    case "access_requested_escalated": {
      const who = str(meta, "requesterName");
      const label = str(meta, "resourceLabel");
      return {
        title: c("access_escalated_title"),
        body: who && label ? c("access_escalated_body", { who, label }) : raw.body,
      };
    }
    case "access_requests_digest": {
      const count = num(meta, "count");
      return count === null ? raw : { title: c("access_digest_title", { count }), body: null };
    }
    case "access_request_release_failed": {
      const who = str(meta, "requesterName");
      const label = str(meta, "resourceLabel");
      return {
        title: c("access_release_failed_title"),
        body: who && label ? c("access_release_failed_body", { who, label }) : raw.body,
      };
    }
    case "access_request_admin_decided": {
      const who = str(meta, "adminName");
      const label = str(meta, "resourceLabel");
      const decision = str(meta, "decision");
      return {
        title: c("access_admin_decided_title"),
        body:
          who && label && (decision === "approved" || decision === "rejected")
            ? c(`access_admin_decided_${decision}_body`, { who, label })
            : raw.body,
      };
    }
    case "scheduled.failed":
      return { title: c("scheduled_failed_title"), body: jobName ? c("scheduled_failed_body", { name: jobName }) : raw.body };
    case "scheduled.paused": {
      const reason = (str(meta, "reason") ?? "").replace(/^scheduled_/, "");
      if (reason === "team_model_unconfigured") {
        return item.subjectType === "team"
          ? { title: c("scheduled_paused_model_admin_title"), body: c("scheduled_paused_model_admin_body") }
          : {
              title: c("scheduled_paused_title"),
              body: jobName ? c("scheduled_paused_model_owner_body", { name: jobName }) : raw.body,
            };
      }
      if (PAUSED_REASONS.has(reason)) {
        return {
          title: c(`scheduled_paused_${reason}_title`),
          body: jobName ? c(`scheduled_paused_${reason}_body`, { name: jobName }) : raw.body,
        };
      }
      return { title: c("scheduled_paused_title"), body: jobName ? c("scheduled_paused_body", { name: jobName }) : raw.body };
    }
    case "scheduled.recovered":
      return { title: c("scheduled_recovered_title"), body: jobName ? c("scheduled_recovered_body", { name: jobName }) : raw.body };
    case "routine.delivered":
      // Body is task-produced summary (content itself); do not translate.
      return { title: jobName ? c("routine_delivered_title", { name: jobName }) : raw.title, body: raw.body };
    case "routine.needs_input":
      return { title: c("routine_needs_input_title"), body: jobName ? c("routine_needs_input_body", { name: jobName }) : raw.body };
    case "feedback_candidate_receipt": {
      const state = str(meta, "finalState");
      if (!state || !FEEDBACK_STATES.has(state)) return raw;
      const stateLabel = c(`feedback_state_${state}`);
      const id = str(meta, "candidateId");
      const kind = str(meta, "targetKind");
      const when = formatMinute(str(meta, "decidedAt"), locale);
      const kindLabel = kind ? (FEEDBACK_KINDS.has(kind) ? c(`feedback_kind_${kind}`) : kind) : null;
      return {
        title: c("feedback_receipt_title", { state: stateLabel }),
        body: id && kindLabel && when ? c("feedback_receipt_body", { id, kind: kindLabel, state: stateLabel, when }) : raw.body,
      };
    }
    case "skill_approval_submitted": {
      const who = str(meta, "actorName");
      const label = str(meta, "skillLabel");
      return who && label ? { title: c("skill_submitted_title", { who, label }), body: null } : raw;
    }
    case "skill_approval_decided": {
      const label = str(meta, "skillLabel");
      const decision = str(meta, "decision");
      if (!label) return raw;
      if (decision === "approved") return { title: c("skill_approved_title", { label }), body: null };
      const reason = str(meta, "reason");
      return {
        title: reason ? c("skill_rejected_reason_title", { label, reason }) : c("skill_rejected_title", { label }),
        body: null,
      };
    }
    case "content_shared": {
      const who = str(meta, "actorName");
      const label = str(meta, "resourceLabel");
      return who && label ? { title: c("content_shared_title", { who, label }), body: null } : raw;
    }
    case "agent.eval.regression_failed":
      return { title: c("agent_eval_failed_title"), body: c("agent_eval_failed_body") };
    default:
      if (item.type in AGENT_VERSION_NOTICES) return agentVersionCopy(item, meta, c) ?? raw;
      if (AGENT_RELEASE_TYPES.has(item.type)) {
        const key = item.type.replace("agent.release.", "");
        // Name snapshotted at projection; rows written before the snapshot keep the unnamed title.
        const agentName = str(meta, "agentName");
        return {
          title: agentName ? c(`agent_release_${key}_title_named`, { name: agentName }) : c(`agent_release_${key}_title`),
          body: c(key === "approval_required" ? "agent_release_approval_required_body" : "agent_release_body"),
        };
      }
      return raw;
  }
}

const TYPE_LABEL_KEYS: Record<string, string> = {
  "approval.required": "type_approval_required",
  "approval.submitted": "type_approval_submitted",
  "approval.rejected": "type_approval_rejected",
  "approval.approved": "type_approval_approved",
  "approval.execution_failed": "type_approval_execution_failed",
  "approval.executed": "type_approval_executed",
  access_requested: "type_access_requested",
  access_request_decided: "type_access_request_decided",
  access_request_expired: "type_access_request_expired",
  access_request_admin_decided: "type_access_request_admin_decided",
  access_requested_escalated: "type_access_requested",
  access_requests_digest: "type_access_requested",
  access_request_release_failed: "type_access_request_release_failed",
  // skill-team-governance: two team-skill approval types (submission→admins / decision→author).
  skill_approval_submitted: "type_skill_approval_submitted",
  skill_approval_decided: "type_skill_approval_decided",
  "memory.candidate": "type_memory_candidate",
  feedback_candidate_receipt: "type_feedback_candidate_receipt",
  space_assignment_requested: "type_space_assignment",
  space_assignment_ready: "type_space_assignment",
  "scheduled.failed": "type_scheduled_failed",
  "scheduled.paused": "type_scheduled_paused",
  "scheduled.recovered": "type_scheduled_recovered",
  "routine.delivered": "type_routine_delivered",
  "routine.needs_input": "type_routine_needs_input",
  content_shared: "type_content_shared",
  "agent.eval.regression_failed": "type_agent_eval",
};

const CATEGORY_LABEL_KEYS: Record<string, string> = {
  approval: "category_approval",
  access_request: "category_access_request",
  async_job: "category_async_job",
  share: "category_share",
  memory: "category_memory",
  feedback: "category_feedback",
  space_assignment: "category_space_assignment",
};

/** Type label: known type → known category → generic Update; never expose raw category codes. */
export function notificationTypeLabel(item: { type: string; category?: string | null }, t: Translate): string {
  if (item.type.startsWith("agent.release.")) return t("type_agent_release");
  if (item.type.startsWith("agent.version.")) return t("type_agent_version");
  const key = TYPE_LABEL_KEYS[item.type] ?? (item.category ? CATEGORY_LABEL_KEYS[item.category] : undefined);
  return t(key ?? "type_update");
}
