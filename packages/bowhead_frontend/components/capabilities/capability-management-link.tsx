"use client";

import { useTranslations } from "next-intl";
import type { MeUser } from "@/lib/auth-api";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { teamRoleForUser } from "@/lib/settings-navigation";
import { withCapabilityReturn, type CapabilityReturnOrigin } from "@/lib/navigation-centers";
import { CapabilityLink } from "./capability-link";

const DESTINATIONS = {
  agents: { route: "/team/settings/subagents/", label: "manage_builtins", button: "manage_builtins_button", gate: "subagent_creator" },
  skills: { route: "/team/settings/workflows/", label: "manage_workflows", button: "manage_workflows_button", gate: "workflow_creator" },
  tools: { route: "/team/settings/tools/", label: "manage_tools", button: "manage_tools_button" },
  capabilities: { route: "/team/settings/capabilities/", label: "manage_capabilities", button: "manage_capabilities_button" },
} as const;

type ManagedResource = keyof typeof DESTINATIONS;

/**
 * The team-maintenance destination of a resource page, or null when the account may not manage it
 * (owners and team admins, plus the resource's capability gate).
 */
export function useCapabilityManagementDestination({ resource, me: suppliedMe, origin }: {
  resource: ManagedResource;
  me?: MeUser | null;
  origin?: CapabilityReturnOrigin;
}): { href: string; label: string; buttonLabel: string } | null {
  const session = useOptionalAuthSession();
  const me = suppliedMe ?? session?.me;
  const t = useTranslations("sidebar.centers");
  const role = teamRoleForUser(me);
  const destination = DESTINATIONS[resource];
  if (role !== "owner" && role !== "team_admin") return null;
  if ("gate" in destination && !me?.capabilities?.[destination.gate]) return null;
  return {
    href: origin ? withCapabilityReturn(destination.route, origin) : destination.route,
    // Menus contract: "⋯" menu items use Title Case, buttons use sentence case — separate keys per role.
    label: t(destination.label),
    buttonLabel: t(destination.button),
  };
}

/** Team maintenance is an action of its resource page, independent of personal authoring. */
export function CapabilityManagementLink(props: {
  resource: ManagedResource;
  me?: MeUser | null;
  origin?: CapabilityReturnOrigin;
}) {
  const destination = useCapabilityManagementDestination(props);
  if (!destination) return null;
  return <CapabilityLink className="ui-btn" href={destination.href}>{destination.buttonLabel}</CapabilityLink>;
}
