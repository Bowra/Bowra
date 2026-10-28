"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { fetchMe } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { isTeamSettingsPathTab, teamSettingsPath } from "@/lib/team-settings-paths";
import {
  SETTINGS_TAB_REQUIRED_TEAM_ROLES,
  type TeamRole,
  type Tab,
} from "@/components/settings/settings-shell";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/notice";
import { AccessWhoCanHelp } from "@/components/access/access-request-panel";
import { accessRequestsEnabledFrom } from "@/lib/use-access-requests-enabled";

type AccessState =
  | { tab: Tab; status: "checking" }
  | { tab: Tab; status: "allowed"; role: TeamRole }
  | { tab: Tab; status: "denied"; role: TeamRole | "unknown" }
  | { tab: Tab; status: "error"; authRequired?: boolean };

const ROLE_LABEL_KEYS: Record<TeamRole | "unknown", string> = {
  owner: "roles.owner",
  team_admin: "roles.team_admin",
  audit_admin: "roles.audit_admin",
  member: "roles.member",
  unknown: "roles.unknown",
};

export function SettingsAccessBoundary({
  tab,
  children,
}: {
  tab: Tab;
  children: ReactNode;
}) {
  const t = useTranslations("settings.access_boundary");
  const required = SETTINGS_TAB_REQUIRED_TEAM_ROLES[tab];
  const [retryTick, setRetryTick] = useState(0);
  const [accessRequestsEnabled, setAccessRequestsEnabled] = useState(false);
  const [state, setState] = useState<AccessState>(
    required?.length ? { tab, status: "checking" } : { tab, status: "allowed", role: "member" },
  );

  useEffect(() => {
    let cancelled = false;
    if (!required?.length) {
      setState({ tab, status: "allowed", role: "member" });
      return () => { cancelled = true; };
    }

    setState({ tab, status: "checking" });
    void fetchMe()
      .then((me) => {
        if (cancelled) return;
        const role = (me.team_role || (me.is_team_admin ? "team_admin" : "member")) as TeamRole;
        setAccessRequestsEnabled(accessRequestsEnabledFrom(me));
        setState(required.includes(role)
          ? { tab, status: "allowed", role }
          : { tab, status: "denied", role });
      })
      .catch(() => {
        if (!cancelled) setState({ tab, status: "error" });
      });

    return () => { cancelled = true; };
  }, [required, retryTick, tab]);

  if (!required?.length) {
    return <>{children}</>;
  }

  const labelRole = (role: TeamRole | "unknown") => t(ROLE_LABEL_KEYS[role]);
  const roles = required.map(labelRole).join(", ");
  const nextPath = isTeamSettingsPathTab(tab)
    ? teamSettingsPath(tab)
    : `/settings/${tab}/`;
  const signInUrl = appPath(`/login/?next=${encodeURIComponent(nextPath)}`);

  if (state.tab !== tab || state.status === "checking") {
    return <Notice tone="muted">{t("checking")}</Notice>;
  }

  if (state.status === "allowed") {
    return <>{children}</>;
  }

  return (
    <div className="legacy-access-boundary ui-access-state">
      <h1 className="ui-page-title legacy-access-boundary-title">
        {state.status === "denied" ? t("denied_title") : t("error_title")}
      </h1>
      <Notice tone={state.status === "denied" ? "error" : "muted"} className="ui-access-state-body">
        {state.status === "denied"
          ? t("denied_body", { roles, role: labelRole(state.role) })
          : t("error_body", { roles })}
      </Notice>
      {state.status === "denied" && accessRequestsEnabled ? <AccessWhoCanHelp scope="team" /> : null}
      <Button
        variant="secondary"
        onClick={() => setRetryTick((value) => value + 1)}
        className="legacy-access-boundary-action"
      >
        {t("retry")}
      </Button>
      {state.status === "error" ? (
        <Button
          variant="secondary"
          onClick={() => { window.location.href = signInUrl; }}
          className="legacy-access-boundary-action legacy-access-boundary-action--next"
        >
          {t("sign_in")}
        </Button>
      ) : null}
    </div>
  );
}
