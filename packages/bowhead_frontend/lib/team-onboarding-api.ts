import { fetchWithRefresh } from "@/lib/auth-api";

export type ReadinessState =
  | "not_applicable"
  | "unknown"
  | "missing"
  | "ready"
  | "warning"
  | "blocked";

export type ReadinessGroup = "core" | "conditional" | "recommended";
export type CapabilityState = "enabled" | "disabled" | "invalid" | "unknown";

export interface ReadinessCheck {
  id: string;
  state: ReadinessState;
  required: boolean;
  satisfied: boolean;
  reason_code: string;
}

export interface ReadinessItem {
  id: string;
  group: ReadinessGroup;
  state: ReadinessState;
  required: boolean;
  required_scope: "core" | "capability" | null;
  satisfied: boolean;
  blocking: boolean;
  reason_code: string;
  required_roles: string[];
  can_act: boolean;
  observed_at: string;
  action: { kind: "internal_link"; href: string } | null;
  checks: ReadinessCheck[];
}

export interface TeamOnboardingStatus {
  schema_version: 1;
  team_id: string;
  milestone: "core_configuring" | "core_ready";
  partial: boolean;
  generated_at: string;
  core: { satisfied: number; total: 5 };
  items: ReadinessItem[];
  context: {
    team_name: string;
    current_role: string;
    active_spaces: number | null;
    active_seats: number | null;
    max_seats: number | null;
    license_health: string | null;
    license_expires_at: string | null;
    grace_until: string | null;
    current_public_entry: string | null;
    account_creation_policy: string | null;
    auth_methods: string[];
    email_source: "team" | "env" | "none" | "unknown";
    kb_feature_licensed: boolean | null;
    optional_capabilities: Record<string, CapabilityState>;
    // credential-readiness R8b: runtime delivery signal (optional: old backend snapshots lack this field).
    runtime?: {
      llm_refusals_total: number;
      llm_refusals_by_reason: Record<string, number>;
      observed_since: string | null;
    };
  };
}

export class TeamOnboardingApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function fetchTeamOnboardingStatus(
  signal?: AbortSignal,
): Promise<TeamOnboardingStatus> {
  const response = await fetchWithRefresh(
    "/api/v1/team-onboarding/status",
    { signal },
  );
  if (!response.ok) {
    throw new TeamOnboardingApiError(
      response.status,
      `team onboarding status: ${response.status}`,
    );
  }
  return response.json() as Promise<TeamOnboardingStatus>;
}
