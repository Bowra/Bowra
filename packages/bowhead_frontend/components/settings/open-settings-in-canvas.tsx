"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { lastWorkRoute } from "@/lib/settings-destinations";
import {
  settingsCanvasHref,
  type SettingsCanvasTarget,
} from "@/lib/settings-canvas-target";
import { useAppRouter } from "@/lib/use-app-router";

export function OpenSettingsInCanvas({
  workspaceId,
  disabled = false,
  page = "workspace_i18n",
}: {
  workspaceId: string;
  page?: SettingsCanvasTarget["page"];
  disabled?: boolean;
}) {
  const session = useOptionalAuthSession();
  const me = session?.me;
  if (!me?.id || !me.team_id || !me.capabilities?.settings_pages_canvas)
    return null;
  return (
    <OpenButton
      disabled={disabled}
      target={{
        page,
        userId: me.id,
        teamId: me.team_id,
        workspaceId,
      }}
    />
  );
}

function OpenButton({
  target,
  disabled,
}: {
  target: SettingsCanvasTarget;
  disabled: boolean;
}) {
  const t = useTranslations("settings_canvas");
  const router = useAppRouter();
  return (
    <Button
      variant="secondary"
      disabled={disabled}
      title={disabled ? t("finish_changes") : undefined}
      onClick={() => router.push(settingsCanvasHref(target, lastWorkRoute()))}
    >
      {t("open")}
    </Button>
  );
}
