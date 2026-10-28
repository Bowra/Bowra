export interface ResourceUsageSummary {
  current_agent_count: number;
  running_task_count: number;
  draft_agent_count: number;
  historical_agent_count: number;
  scheduled_binding_count: number;
  restricted_count: number;
  completeness: "complete" | "partial";
  reason_codes: string[];
}

export interface ResourceUsageItem {
  kind: "agent" | "task";
  id: string;
  agent_id: string;
  display_name: string;
  relation: "current" | "running" | "draft" | "history";
  readiness: "ready" | "degraded" | "unavailable" | "unknown";
  subject_ref: string;
  evidence_level: "exact" | "stable_only";
  resource_revision: string | null;
  href: string | null;
  status: string | null;
}

export interface ResourceUsageResponse {
  kind: "resource_usage";
  schema_version: 1;
  resource: {
    kind: "skill" | "tool_source" | "tool_operation";
    id: string;
    display_name: string;
    revision: string | null;
  };
  summary: ResourceUsageSummary;
  actual_usage_availability: "unavailable";
  completeness: "complete" | "partial";
  reason_codes: string[];
  groups: {
    current: ResourceUsageItem[];
    running: ResourceUsageItem[];
    draft: ResourceUsageItem[];
    history: ResourceUsageItem[];
  };
  next_cursors: Partial<Record<"current" | "running" | "draft" | "history", string>>;
}
