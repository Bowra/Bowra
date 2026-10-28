"use client";

/**
 * Batch 2/c12 (workspace-space-split PRD §2.3)—zero-space guidance page (real route).
 *
 * Fix review P0's threefold zero-space first-screen failure: use-workspace-scoped-data.ts previously navigated to
 * a dead /empty-workspace 404. Two parallel paths (user-approved guidance + defaults):
 *  1. Join a Space—pending invitations (new my-invites endpoint), accept in one click
 *  2. Create My First Space—one-click defaults (editable prefill “{名字}的空间”)
 * No invitation and no wish to create → suggest contacting an admin (no dead end).
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { createWorkspace } from "@/lib/api";
import { readApiError } from "@/lib/api-client";
import { fetchMe, fetchWithRefresh } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { setActiveSpaceId } from "@/lib/space-storage";

interface InviteItem {
  invite_id: string;
  workspace_id: string;
  workspace_name: string;
  role: string;
}

export default function EmptyWorkspacePage() {
  const t = useTranslations("empty_workspace");
  const tRoles = useTranslations("settings.members");
  // Invitation space roles are raw API values (admin / member / viewer); display using space-member-page role copy;
  // unknown values display Member, never exposing raw identifiers.
  const roleLabel = (role: string) =>
    role === "admin" ? tRoles("role_admin") : role === "viewer" ? tRoles("role_viewer") : tRoles("role_member");
  const [invites, setInvites] = useState<InviteItem[] | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ShownError | null>(null);

  useEffect(() => {
    void fetchWithRefresh("/api/v1/auth/me/invites")
      .then((r) => (r.ok ? r.json() : { items: [] }))
      .then((d) => setInvites(d.items ?? []))
      .catch(() => setInvites([]));
    void fetchMe()
      .then((me) => {
        const display =
          (me as { display_name?: string; email?: string }).display_name ||
          ((me as { email?: string }).email || "").split("@")[0] ||
          "";
        setName(display ? t("default_name", { name: display }) : t("default_name_fallback"));
      })
      .catch(() => setName(t("default_name_fallback")));
  }, [t]);

  const enterWorkspace = (wsId: string) => {
    try {
      setActiveSpaceId(wsId);
    } catch { /* storage stub in tests */ }
    window.location.href = appPath("/");
  };

  const accept = useCallback(async (inviteId: string) => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await fetchWithRefresh(`/api/v1/auth/me/invites/${encodeURIComponent(inviteId)}/accept`, {
        method: "POST",
      });
      if (!r.ok) throw await readApiError(r);
      const d = await r.json();
      enterWorkspace(d.workspace_id);
    } catch (e) {
      setErr(shownError(t("accept_failed"), e));
      setBusy(false);
    }
  }, [busy, t]);

  const create = useCallback(async () => {
    if (busy || !name.trim()) return;
    setBusy(true);
    try {
      const d = await createWorkspace(name.trim());
      enterWorkspace(d.workspace_id);
    } catch (e) {
      setErr(shownError(t("create_failed"), e));
      setBusy(false);
    }
  }, [busy, name, t]);

  return (
    <div className="legacy-ews ui-ews">
      <h1 className="legacy-ews-title ui-page-title">{t("title")}</h1>
      <p className="legacy-ews-subtitle ui-page-desc">{t("subtitle")}</p>

      {/* Path 1: join a space */}
      <section className="legacy-ews-join ui-ews-group">
        <h2 className="legacy-ews-heading ui-ews-group-title">{t("join_title")}</h2>
        {invites === null ? (
          <div className="legacy-ews-loading ui-ews-note">…</div>
        ) : invites.length === 0 ? (
          <div className="legacy-ews-empty ui-ews-note">{t("no_invites")}</div>
        ) : (
          <div className="legacy-ews-invites ui-ews-card">
            {invites.map((iv) => (
              <div key={iv.invite_id} className="legacy-ews-invite ui-ews-row">
                <div className="ui-ews-row-text">
                  <div className="legacy-ews-invite-name ui-ews-row-title">{iv.workspace_name}</div>
                  <div className="legacy-ews-invite-role ui-ews-row-sub">{roleLabel(iv.role)}</div>
                </div>
                <button type="button" className="settings-btn ui-btn" disabled={busy}
                        onClick={() => accept(iv.invite_id)}>
                  {t("accept")}
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Path 2: create the first space (one-click defaults) */}
      <section className="ui-ews-group">
        <h2 className="legacy-ews-heading ui-ews-group-title">{t("create_title")}</h2>
        <div className="legacy-ews-create ui-ews-card ui-ews-create">
          <input
            type="text"
            value={name}
            maxLength={60}
            disabled={busy}
            onChange={(e) => setName(e.target.value)}
            className="legacy-ews-input ui-input"
            aria-label={t("create_title")}
          />
          <button type="button" className="settings-btn ui-btn ui-btn--primary" disabled={busy || !name.trim()}
                  onClick={create}>
            {busy ? "…" : t("create")}
          </button>
        </div>
      </section>

      {err ? <div className="legacy-ews-error ui-ews-error" role="alert">{err.message}</div> : null}
      {err ? <ErrorDiagnosticsInline error={err.error} surface="route" /> : null}
    </div>
  );
}
