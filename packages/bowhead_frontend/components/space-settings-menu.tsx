"use client";

import { useEffect, useId, useState } from "react";
import { useTranslations } from "next-intl";

import { ErrorDiagnosticsInline } from "@/components/error-notice";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Notice } from "@/components/ui/notice";
import { MenuItemReason, useMenuItem } from "@/components/ui/menu";
import { type WorkspaceDTO } from "@/lib/api";
import { ApiError, apiJson, apiVoid } from "@/lib/api-client";
import { appPath } from "@/lib/base-path";
import { confirmCanvasNavigation } from "@/lib/canvas-navigation-guard";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { clearSpaceStorage } from "@/lib/space-storage";
import { SPACE_SETTINGS_PAGES, spaceSettingsPath, type SpaceSettingsPage } from "@/lib/space-settings-paths";
import { useAppRouter } from "@/lib/use-app-router";
import { invalidateSpaces, readSpaces, restoreActiveSpace, switchActiveSpace } from "@/lib/use-space";
import { toast } from "@/lib/use-toast";

function SpaceSettingsLink({ page }: { page: SpaceSettingsPage }) {
  const t = useTranslations("settings.tabs");
  const router = useAppRouter();
  const path = spaceSettingsPath(page);
  const props = useMenuItem({ label: t(page), onSelect: (event) => {
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    router.push(path);
  } });
  return <a className="app-floating-menu-item" href={appPath(path)} {...props}>{t(page)}</a>;
}

export function SpaceSettingsItems({ space, onLeave }: { space: WorkspaceDTO; onLeave: () => void }) {
  const t = useTranslations("workspace");
  const [canLeave, setCanLeave] = useState<boolean | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const reasonId = useId();
  useEffect(() => {
    let cancelled = false;
    setCanLeave(null);
    setFailed(false);
    void apiJson<{ can_leave: boolean }>(`/api/v1/workspaces/${encodeURIComponent(space.id)}/leave`)
      .then((result) => { if (!cancelled) setCanLeave(result.can_leave); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [space.id, retry]);
  const reason = failed ? t("leave_check_failed") : canLeave === null ? t("leave_checking") : !canLeave ? t("leave_last_admin") : null;
  const leaveProps = useMenuItem({ label: t("leave"), disabled: canLeave !== true, onSelect: onLeave });
  const retryProps = useMenuItem({ label: t("leave_retry"), closeOnSelect: false, onSelect: () => setRetry(value => value + 1) });
  return <>
    <div className="v2-user-menu-label">{space.name}</div>
    {space.role === "admin" ? <>
      {SPACE_SETTINGS_PAGES.map(page => <SpaceSettingsLink key={page} page={page} />)}
      <div role="separator" className="ui-row-menu-sep" />
    </> : null}
    <button type="button" className="app-floating-menu-item is-danger" aria-describedby={reason ? reasonId : undefined} {...leaveProps}>
      {t("leave")}…
    </button>
    {reason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
    {failed ? <button type="button" className="app-floating-menu-item" {...retryProps}>{t("leave_retry")}</button> : null}
  </>;
}

export function LeaveSpaceDialog({ space, onClose }: { space: WorkspaceDTO; onClose: () => void }) {
  const t = useTranslations("workspace");
  const router = useAppRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const leave = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      if (!await confirmCanvasNavigation()) return;
      await apiVoid(`/api/v1/workspaces/${encodeURIComponent(space.id)}/leave`, { method: "POST" });
      clearSpaceStorage();
      restoreActiveSpace("");
      invalidateSpaces();
      const fresh = await readSpaces();
      const next = fresh.items.find(item => item.id !== space.id);
      if (next) switchActiveSpace(next.id);
      onClose();
      router.replace("/");
      toast({ kind: "success", text: t("leave_success", { name: space.name }) });
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };
  const errorKey = error instanceof ApiError && error.code === "last_workspace_admin" ? "leave_last_admin"
    : error instanceof ApiError && error.code === "not_workspace_member" ? "leave_not_member"
    : error instanceof ApiError && error.code === "workspace_archived" ? "leave_archived" : "leave_failed";
  return <ConfirmDialog title={t("leave_title", { name: space.name })} description={t("leave_description")}
    confirmLabel={t("leave")} busy={busy} onCancel={onClose} onConfirm={() => void leave()}>
    {error ? <><Notice tone="error">{t(errorKey)}</Notice><ErrorDiagnosticsInline error={error} surface="global" /></> : null}
  </ConfirmDialog>;
}
