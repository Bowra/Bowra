import { EXTRA_ADMIN_DESTINATIONS } from "@/edition/settings";
import type { MeUser } from "./auth-api";

export type SettingsTeamRole = NonNullable<MeUser["team_role"]>;
export type AdministrationDestinationId =
  | "approvals"
  | "traces"
  | "credits"
  | "audit"
  | "access";

export interface AdministrationDestination {
  id: AdministrationDestinationId;
  route: string;
  requiredRoles: readonly SettingsTeamRole[];
}

export const TEAM_SETTINGS_ROLES: readonly SettingsTeamRole[] = [
  "owner",
  "team_admin",
];

// credential-readiness D5 (review V8): model-admin roles moved down from settings-shell—
// lib/ (chat-send-errors, etc.) needs them for can_act branches in unavailable hints; importing
// components/ from lib/ would invert the layers. Same role source as backend _REQUIRE_TEAM_ADMIN.
export const MODEL_ADMIN_ROLES: readonly SettingsTeamRole[] = [
  "owner",
  "team_admin",
];

export const ADMINISTRATION_DESTINATIONS: readonly AdministrationDestination[] = [
  {
    id: "traces",
    route: "/admin/turns/",
    // The live replay page and its backend debug endpoints require team admin.
    // Do not repeat the legacy menu bug that sent audit_admin to a denied page.
    requiredRoles: TEAM_SETTINGS_ROLES,
  },
  {
    id: "approvals",
    route: "/admin/approvals/",
    requiredRoles: TEAM_SETTINGS_ROLES,
  },
  {
    id: "credits",
    route: "/admin/credits/",
    requiredRoles: TEAM_SETTINGS_ROLES,
  },
  ...EXTRA_ADMIN_DESTINATIONS,
  {
    id: "access",
    route: "/admin/access/",
    requiredRoles: TEAM_SETTINGS_ROLES,
  },
];

export function teamRoleForUser(
  user: Pick<MeUser, "team_role" | "is_team_admin"> | null | undefined,
): SettingsTeamRole | null {
  if (!user) return null;
  return user.team_role ?? (user.is_team_admin ? "team_admin" : "member");
}

export function visibleAdministrationDestinations(
  role: SettingsTeamRole | null,
): AdministrationDestination[] {
  if (!role) return [];
  return ADMINISTRATION_DESTINATIONS.filter((destination) =>
    destination.requiredRoles.includes(role),
  );
}
