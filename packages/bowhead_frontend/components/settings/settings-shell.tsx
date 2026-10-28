"use client";

/**
 * SettingsShell — unified settings shell (AdminShell + content column).
 *
 * The settings sections live in the sidebar's settings content (P2 batch 4, lib/settings-destinations.ts);
 * this shell renders the content column, the team-role gate and the Save bar slot.
 *
 * Usage: <SettingsShell active="subagents">{page content}</SettingsShell>
 */

import { Suspense, useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { fetchMe } from "@/lib/auth-api";
import { CAPABILITY_SETTINGS_TABS, capabilityReturnDestination } from "@/lib/navigation-centers";
import { useAppRouter } from "@/lib/use-app-router";
import {
  MODEL_ADMIN_ROLES,
  TEAM_SETTINGS_ROLES,
  teamRoleForUser,
  type SettingsTeamRole,
} from "@/lib/settings-navigation";
import { CapabilityLink } from "@/components/capabilities/capability-link";
import { LucideGlyph } from "@/components/icon-refresh";
import { ChevronLeft } from "lucide-react";
import { AdminShell } from "@/components/admin-shell";
import { AccessWhoCanHelp } from "@/components/access/access-request-panel";
import { accessRequestsEnabledFrom } from "@/lib/use-access-requests-enabled";
import { SaveBarSlotContext } from "@/components/ui/save-bar";
import { SETTINGS_TAB_ROLES, type SettingsTabId } from "@/lib/settings-destinations";
// The tab ids and their role requirements live in lib/settings-destinations.ts, shared with the sidebar's
// settings content (P2 batch 4).
export type Tab = SettingsTabId;

/**
 * Two content widths (Page Layout Spec: forms/settings 880, tables/lists 1120).
 * Emit only `data-ui-width`, styled in styles/ui-refresh.css.
 */
const TABLE_WIDTH_TABS: ReadonlySet<Tab> = new Set<Tab>([
  "people", "workspaces", "members", "scheduled", "tools", "capabilities", "subagents", "workflows",
  "feedback-inbox", "team-operations", "access-requests", "openapi",
]);

// Resource pages with their own navigation context.
export const OWN_ROUTE_TABS = new Set<Tab>(["tools", "capabilities", "workflows", "subagents", "access-requests"]);
export type SettingsScope = "personal" | "feature" | "team" | "space";

export type TeamRole = SettingsTeamRole;

export const TEAM_ADMIN_ROLES: readonly TeamRole[] = TEAM_SETTINGS_ROLES;
// Keeps front-end tab gating in sync with the backend (get/put/providers/rotate/delete all _REQUIRE_TEAM_ADMIN).
// credential-readiness D5 (review V8): constants moved into lib/settings-navigation.ts (lib-level
// can_act branches need them; lib cannot import components/). Reexport here for compatibility.
export { MODEL_ADMIN_ROLES };
export const ADMIN_VIEWER_ROLES: readonly TeamRole[] = [
  "owner", "team_admin", "audit_admin",
];

// Team-admin only tabs (tool sources, team theme policy, ...) mirror the backend _REQUIRE_TEAM_ADMIN guards.
export const SETTINGS_TAB_REQUIRED_TEAM_ROLES: Partial<Record<Tab, readonly TeamRole[]>> = SETTINGS_TAB_ROLES;

function inferredScope(active: Tab): SettingsScope {
  if (active === "access-requests") return "feature";
  if (["people", "workspaces", "llm", "datasource", "integrations", "email-service", "public-entries", "oidc", "license", "openapi", "team-appearance", "tools", "capabilities", "subagents", "workflows", "scheduled", "feedback-inbox", "team-operations", "reference-retention"].includes(active)) return "team";
  if (["general", "instructions", "members"].includes(active)) return "space";
  return "personal";
}

type ResourceReturn = { href: string; label: string } | null;

/** Small "‹ Back to …" return link above the page title (Capability Center design brief, page header). */
function ResourceReturnLink({ destination }: { destination: ResourceReturn }) {
  const t = useTranslations("sidebar.centers");
  return destination ? (
    <CapabilityLink className="settings-breadcrumb" href={destination.href}>
      <LucideGlyph icon={ChevronLeft} size={14} aria-hidden="true" />
      <span>{t("back_to_resource", { name: t(destination.label) })}</span>
    </CapabilityLink>
  ) : null;
}

/**
 * Built-in Agents belong to team settings (Agents & Reports polish spec D1): without an explicit team-overview origin
 * the page carries the settings breadcrumb "‹ Team settings" rather than a return to the Agents catalog.
 */
function TeamSettingsBreadcrumb() {
  const navT = useTranslations("settings.nav");
  return (
    <CapabilityLink className="settings-breadcrumb" href="/team/settings/">
      <LucideGlyph icon={ChevronLeft} size={14} aria-hidden="true" />
      <span>{navT("team_settings")}</span>
    </CapabilityLink>
  );
}

function ContextualResourceReturn({ active, fallback }: { active: Tab; fallback: ResourceReturn }) {
  const query = useSearchParams();
  const origin = capabilityReturnDestination(active, query?.toString() ?? "");
  if (!origin && active === "subagents") return <TeamSettingsBreadcrumb />;
  return <ResourceReturnLink destination={origin ?? fallback} />;
}

export function SettingsShell({
  active,
  children,
  scope,
  returnToSetup = false,
  title,
}: {
  active: Tab;
  children: ReactNode;
  scope?: SettingsScope;
  returnToSetup?: boolean;
  /**
   * Top-bar title. Pass the page H1 text (from the H1's own message key) so the large-title
   * pattern hides the bar title while the H1 is on screen; defaults to the section name.
   */
  title?: string;
}) {
  const resolvedScope = scope ?? inferredScope(active);
  const router = useAppRouter();
  const navT = useTranslations("settings.nav");
  const accessT = useTranslations("settings.access_boundary");
  const setupReturnT = useTranslations("settings.setup_return");
  const centerT = useTranslations("sidebar.centers");
  const [saveBarSlot, setSaveBarSlot] = useState<HTMLElement | null>(null);
  const [teamRole, setTeamRole] = useState<TeamRole | null>(null);
  const [teamOnboardingVisible, setTeamOnboardingVisible] = useState(false);
  const [accessRequestsEnabled, setAccessRequestsEnabled] = useState(false);
  const [teamRoleError, setTeamRoleError] = useState(false);
  const [identityRetryTick, setIdentityRetryTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setTeamRole(null);
    setTeamRoleError(false);
    void fetchMe()
      .then((u) => {
        if (cancelled) return;
        setTeamOnboardingVisible(Boolean(u.capabilities?.team_onboarding));
        setAccessRequestsEnabled(accessRequestsEnabledFrom(u));
        setTeamRole(teamRoleForUser(u));
      })
      .catch(() => {
        if (!cancelled) setTeamRoleError(true);
      });
    return () => { cancelled = true; };
  }, [identityRetryTick]);
  const canAccessActiveTeamTab = Boolean(
    teamRole && (
      active === "scheduled"
      || active === "feedback-inbox" // Members can deep-link; API personalizes page content.
      || TEAM_ADMIN_ROLES.includes(teamRole)
      || SETTINGS_TAB_REQUIRED_TEAM_ROLES[active]?.includes(teamRole)
    ),
  );
  const resource = active === "subagents" ? null
    : active === "workflows" ? { href: "/skills/", label: "skills" }
      : active === "tools" ? { href: "/capabilities/?type=tools", label: "tools_catalog" }
        : active === "capabilities" ? { href: "/capabilities/?type=capabilities", label: "capabilities_catalog" }
          : null;
  const resourceReturn = ["subagents", "workflows", "tools", "capabilities"].includes(active) ? (
    <Suspense fallback={active === "subagents" ? <TeamSettingsBreadcrumb /> : <ResourceReturnLink destination={resource} />}>
      <ContextualResourceReturn active={active} fallback={resource} />
    </Suspense>
  ) : null;
  const settingsSurface = (
      <div
        className="settings-shell is-settings-surface"
        data-settings-scope={resolvedScope}
        data-settings-tab={active}
        style={{
        flex: 1, minHeight: 0, display: "grid",
        gridTemplateColumns: "minmax(0, 1fr)",
        background: "var(--c-bg)",
      }}
      >
        <section className="settings-content" style={{ padding: "28px 32px", overflow: "auto", minWidth: 0 }}>
          <div className="settings-content-inner" data-ui-width={TABLE_WIDTH_TABS.has(active) ? "table" : "form"}>
            {active === "subagents" && resourceReturn ? (
              <div className="builtin-agents-return">{resourceReturn}</div>
            ) : resourceReturn}
            {returnToSetup && teamOnboardingVisible ? (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 16,
                  marginBottom: 20,
                  border: "1px solid var(--c-line)",
                  borderRadius: 9,
                  padding: "10px 13px",
                  color: "var(--c-ink-2)",
                  background: "var(--c-surface)",
                  fontSize: "0.75rem",
                }}
              >
                <span>{setupReturnT("context")}</span>
                <button
                  type="button"
                  className="settings-inline-retry"
                  onClick={() => router.push("/setup")}
                >
                  {setupReturnT("action")}
                </button>
              </div>
            ) : null}
            {resolvedScope !== "team" ? children : teamRole === null ? (
              teamRoleError ? (
                <div className="legacy-settings-gate ui-settings-gate">
                  <p role="alert">{accessT("error_title")}</p>
                  <button
                    type="button"
                    className="settings-inline-retry"
                    onClick={() => setIdentityRetryTick((value) => value + 1)}
                  >
                    {accessT("retry")}
                  </button>
                </div>
              ) : (
                <div role="status" className="legacy-settings-gate ui-settings-gate ui-settings-gate--checking">
                  {accessT("checking")}
                </div>
              )
            ) : canAccessActiveTeamTab ? children : (
              <>
                <div role="alert" className="legacy-settings-gate ui-settings-gate">{navT("team_settings_access_denied")}</div>
                {accessRequestsEnabled ? <AccessWhoCanHelp scope="team" /> : null}
              </>
            )}
          </div>
          <div ref={setSaveBarSlot} className="ui-savebar-slot" />
        </section>
      </div>
  );

  return (
    <AdminShell
      current="settings"
      mobileTitle={title ?? (active === "subagents" ? centerT("agents") : CAPABILITY_SETTINGS_TABS.has(active) ? centerT("title") : navT("settings_label"))}
      settingsActiveTab={active}
    >
      <SaveBarSlotContext.Provider value={saveBarSlot}>
        {settingsSurface}
      </SaveBarSlotContext.Provider>
    </AdminShell>
  );
}
