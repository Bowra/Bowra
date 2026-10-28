"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { SpaceInstructionsDialog } from "@/components/settings/space-instructions";
import { Field } from "@/components/ui/field";
import { ActiveFilterChips, type ActiveFilter } from "@/components/ui/active-filter-chips";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import {
  AdminSpacesError,
  archiveTeamSpace,
  createTeamSpace,
  fetchTeamSpaceAdmins,
  fetchTeamSpacesPage,
  grantTeamSpaceAdmin,
  revokeTeamSpaceAdmin,
  type SpaceAdminRow,
  type TeamMemberCandidate,
  restoreTeamSpace,
  type TeamSpaceRow,
} from "@/lib/admin-spaces-api";
import {
  fetchAdminMembers,
  type AdminMemberRow,
} from "@/lib/admin-members-api";
import { invalidateSpaces, switchActiveSpace, useSpace } from "@/lib/use-space";
import { toast } from "@/lib/use-toast";
import { useCopyDiagnosticsToastAction } from "@/components/error-notice";

export function TeamSpacesAdmin() {
  const t = useTranslations("settings.workspaces");
  const copyDiagnosticsAction = useCopyDiagnosticsToastAction();
  const currentSpace = useSpace();
  const [items, setItems] = useState<TeamSpaceRow[]>([]);
  const [status, setStatus] = useState<"active" | "archived" | "all">("active");
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [pageSize, setPageSize] = useState(20);
  const [cursor, setCursor] = useState<string | null>(null);
  const [previousCursors, setPreviousCursors] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [restoreTarget, setRestoreTarget] = useState<TeamSpaceRow | null>(null);
  const [adminsTarget, setAdminsTarget] = useState<TeamSpaceRow | null>(null);
  const [instructionsTarget, setInstructionsTarget] = useState<TeamSpaceRow | null>(null);
  const [spaceAdmins, setSpaceAdmins] = useState<SpaceAdminRow[]>([]);
  const [adminCandidates, setAdminCandidates] = useState<TeamMemberCandidate[]>([]);
  const [newAdminId, setNewAdminId] = useState("");
  const [adminsLoading, setAdminsLoading] = useState(false);
  const [recoveryMembers, setRecoveryMembers] = useState<AdminMemberRow[]>([]);
  const [recoveryAdminId, setRecoveryAdminId] = useState("");
  const [recoveryLoading, setRecoveryLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const busyRef = useRef(false);

  const reload = useCallback(() => setRefreshTick((value) => value + 1), []);
  const resetPage = () => {
    setCursor(null);
    setPreviousCursors([]);
    setNextCursor(null);
  };
  const search = () => {
    setQuery(queryInput.trim());
    resetPage();
    reload();
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setItems([]);
    setNextCursor(null);
    void fetchTeamSpacesPage({ status, q: query, pageSize, cursor })
      .then((result) => {
        if (cancelled) return;
        setItems(result.items);
        setNextCursor(result.next_cursor);
        if (result.items.length === 0 && previousCursors.length > 0) {
          setCursor(previousCursors.at(-1) ?? null);
          setPreviousCursors((current) => current.slice(0, -1));
        }
      })
      .catch(() => {
        if (!cancelled) setError(t("load_failed"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [cursor, pageSize, previousCursors.length, query, refreshTick, status, t]);

  const beginBusy = () => {
    if (busyRef.current) return false;
    busyRef.current = true;
    setBusy(true);
    return true;
  };

  const endBusy = () => {
    busyRef.current = false;
    setBusy(false);
  };

  const create = async () => {
    const name = newName.trim();
    if (!name || !beginBusy()) return;
    setError(null);
    try {
      const created = await createTeamSpace({
        name,
        set_as_current: !currentSpace.activeId,
      });
      setNewName("");
      if (!currentSpace.activeId) switchActiveSpace(created.workspace_id);
      invalidateSpaces();
      setStatus("active");
      resetPage();
      reload();
    } catch (caught) {
      setError(formatError(caught, t));
    } finally {
      endBusy();
    }
  };

  const openAdmins = async (target: TeamSpaceRow) => {
    setAdminsTarget(target);
    setNewAdminId("");
    setSpaceAdmins([]);
    setAdminCandidates([]);
    setAdminsLoading(true);
    try {
      const result = await fetchTeamSpaceAdmins(target.workspace_id);
      setSpaceAdmins(result.admins);
      setAdminCandidates(result.team_members);
    } catch (caught) {
      setError(formatError(caught, t));
    } finally {
      setAdminsLoading(false);
    }
  };

  const grantAdmin = async () => {
    if (!adminsTarget || !newAdminId || !beginBusy()) return;
    try {
      await grantTeamSpaceAdmin(adminsTarget.workspace_id, newAdminId);
      const result = await fetchTeamSpaceAdmins(adminsTarget.workspace_id);
      setSpaceAdmins(result.admins);
      setAdminCandidates(result.team_members);
      setNewAdminId("");
      reload();
    } catch (caught) {
      setError(formatError(caught, t));
    } finally {
      endBusy();
    }
  };

  const revokeAdmin = async (userId: string) => {
    if (!adminsTarget || !beginBusy()) return;
    try {
      await revokeTeamSpaceAdmin(adminsTarget.workspace_id, userId);
      setSpaceAdmins((current) => current.filter((member) => member.user_id !== userId));
      reload();
    } catch (caught) {
      setError(formatError(caught, t));
    } finally {
      endBusy();
    }
  };

  // Undo archive: restore immediately using returned version. Failure shows localized error (Copy Diagnostics) and throws, keeping Undo retryable.
  // Exceptions: space no longer archived (`workspace_not_archived`) or version changed by another user (`stale_workspace_update`);
  // retrying that old version would always fail—refresh list, finish normally (closing Undo Toast), replace it with an accurate explanation.
  const undoArchive = async (archived: TeamSpaceRow) => {
    try {
      await restoreTeamSpace(archived.workspace_id, archived.revision);
    } catch (caught) {
      const code = caught instanceof AdminSpacesError ? caught.code : null;
      if (code === "workspace_not_archived" || code === "stale_workspace_update") {
        invalidateSpaces();
        reload();
        toast({
          kind: code === "workspace_not_archived" ? "info" : "warning",
          text: t(code === "workspace_not_archived" ? "archive_undo_already_restored" : "archive_undo_conflict", {
            name: archived.name,
          }),
        });
        return;
      }
      toast({
        kind: "error",
        text: t("archive_undo_failed", { name: archived.name }),
        action: copyDiagnosticsAction(caught, "settings"),
      });
      throw caught;
    }
    invalidateSpaces();
    reload();
  };

  const archive = async (target: TeamSpaceRow) => {
    if (!beginBusy()) return;
    setError(null);
    try {
      const archived = await archiveTeamSpace(target.workspace_id, target.revision);
      invalidateSpaces();
      reload();
      toast({
        kind: "success",
        undo: true,
        text: t("archive_done", { name: target.name }),
        durationMs: 8_000,
        action: {
          label: t("archive_undo"),
          onAction: () => undoArchive({ ...target, ...archived }),
        },
      });
    } catch (caught) {
      setError(formatError(caught, t));
    } finally {
      endBusy();
    }
  };

  const openRestore = async (target: TeamSpaceRow) => {
    setRestoreTarget(target);
    setRecoveryAdminId("");
    setRecoveryMembers([]);
    if (target.admin_count > 0) return;
    setRecoveryLoading(true);
    try {
      const members = await fetchAdminMembers();
      setRecoveryMembers(members.items);
    } catch (caught) {
      setError(formatError(caught, t));
    } finally {
      setRecoveryLoading(false);
    }
  };

  const restore = async (target: TeamSpaceRow) => {
    if (target.admin_count === 0 && !recoveryAdminId) return;
    if (!beginBusy()) return;
    setError(null);
    try {
      await restoreTeamSpace(
        target.workspace_id,
        target.revision,
        target.admin_count === 0 ? recoveryAdminId : undefined,
      );
      invalidateSpaces();
      reload();
    } catch (caught) {
      setError(formatError(caught, t));
    } finally {
      endBusy();
      setRestoreTarget(null);
    }
  };

  const newNameInput = (
    <input
      id="settings-team-spaces-new-name"
      className="settings-input"
      value={newName}
      onChange={(event) => setNewName(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") void create();
      }}
      placeholder={t("new_placeholder")}
    />
  );
  // Active filters (tradeoff 20): submitted query and nondefault status; appear only when at least 2 are active.
  const activeFilters: ActiveFilter[] = [
    ...(query ? [{ key: "q", label: t("filter_chip_query", { q: query }), onRemove: () => { setQueryInput(""); setQuery(""); resetPage(); reload(); } }] : []),
    ...(status !== "active" ? [{ key: "status", label: t("filter_chip_status", { status: t(`status_${status}`) }), onRemove: () => { setStatus("active"); resetPage(); } }] : []),
  ];

  return (
    <div>
      <header className="ui-page-head legacy-team-spaces-head">
        <h1
          className="ui-page-title legacy-team-spaces-title"
        >
          {t("title")}
        </h1>
        <p className="ui-page-desc legacy-team-spaces-desc">
          {t("admin_subtitle")}
        </p>
      </header>

      <section className="ui-group ui-tsa-create" aria-labelledby="settings-team-spaces-create-title">
        <div className="ui-group-head">
          <h2 id="settings-team-spaces-create-title" className="ui-group-title">{t("create_section_title")}</h2>
          <p className="ui-group-desc">{t("create_section_hint")}</p>
        </div>
        <div className="ui-group-card">
          <div className="ui-group-row">
            <label className="ui-group-label" htmlFor="settings-team-spaces-new-name">{t("new_placeholder")}</label>
            <div className="ui-group-control is-wide ui-tsa-create-control">
              {newNameInput}
              <Button variant="primary" disabled={busy || !newName.trim()} onClick={() => void create()}>
                {t("new_btn")}
              </Button>
            </div>
          </div>
        </div>
      </section>

      <section aria-labelledby="settings-team-spaces-governance-title" className="ui-group ui-tsa-list">
        <div className="legacy-tsa-list-head ui-group-head">
          <h2 id="settings-team-spaces-governance-title" className="legacy-tsa-section-title ui-group-title">{t("governance_section_title")}</h2>
          <p className="legacy-tsa-section-hint ui-group-desc">{t("governance_section_hint")}</p>
        </div>
        <div className="team-spaces-filters">
          <Field id="settings-team-spaces-search" label={t("search_label")}>
            <input
              id="settings-team-spaces-search"
              className="settings-input"
              value={queryInput}
              onChange={(event) => setQueryInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") search();
              }}
              placeholder={t("search_placeholder")}
            />
          </Field>
          <Field id="settings-team-spaces-status" label={t("status_label")}>
            <select
              id="settings-team-spaces-status"
              className="settings-input"
              value={status}
              onChange={(event) => {
                setStatus(event.target.value as "active" | "archived" | "all");
                resetPage();
              }}
            >
              <option value="active">{t("status_active")}</option>
              <option value="archived">{t("status_archived")}</option>
              <option value="all">{t("status_all")}</option>
            </select>
          </Field>
          <Button
            onClick={search}
            className="legacy-tsa-aligned"
          >
            {t("search")}
          </Button>
        </div>
        <ActiveFilterChips filters={activeFilters} onClearAll={() => {
          setQueryInput("");
          setQuery("");
          setStatus("active");
          resetPage();
          reload();
        }} />

        {error ? (
          <div>
            <Notice tone="error">{error}</Notice>
            <Button onClick={reload} className="legacy-tsa-retry">{t("retry")}</Button>
          </div>
        ) : loading ? (
          <LoadingSkeleton label={t("loading")} />
        ) : (
          <div className="legacy-tsa-list ui-tsa-list-card">
          {items.map((item) => (
            <div
              key={item.workspace_id}
              className="legacy-tsa-row ui-tsa-row"
            >
              <div className="legacy-tsa-row-main">
                <div className="legacy-tsa-row-title">
                  <strong title={item.workspace_id} className="ui-tsa-row-name">{item.name}</strong>
                  <span className={`legacy-tsa-row-status ui-tsa-row-status ui-tsa-row-status--${item.status}`}>
                    {t(`status_${item.status}`)}
                  </span>
                </div>
                <div className="legacy-tsa-row-counts ui-tsa-row-meta">
                  {t("governance_counts", {
                    members: item.member_count,
                    admins: item.admin_count,
                  })}
                </div>
                {!item.caller_membership ? (
                  <div className="legacy-tsa-row-note ui-tsa-row-meta">
                    {t("not_joined")}
                  </div>
                ) : null}
              </div>
              <RowActionsMenu
                label={t("more_actions_for", { name: item.name })}
                pending={busy}
                actions={
                  item.status === "active"
                    ? [
                        { id: "admins", label: t("manage_admins"), disabled: busy, onSelect: () => void openAdmins(item) },
                        { id: "instructions", label: t("edit_instructions"), disabled: busy, onSelect: () => setInstructionsTarget(item) },
                        { id: "archive", label: t("archive"), disabled: busy, onSelect: () => void archive(item) },
                      ]
                    : [{ id: "restore", label: t("restore"), disabled: busy, onSelect: () => void openRestore(item) }]
                }
              />
            </div>
          ))}
          {items.length === 0 ? (
            <div className="legacy-tsa-empty ui-empty">{t("empty_filter")}</div>
          ) : null}
          </div>
        )}
        {!error ? <div className="team-spaces-pagination" aria-label={t("pagination_label")}>
          <label htmlFor="settings-team-spaces-page-size">{t("page_size_label")}</label>
          <select
            id="settings-team-spaces-page-size"
            className="settings-input"
            value={pageSize}
            disabled={loading}
            onChange={(event) => {
              setPageSize(Number(event.target.value));
              resetPage();
            }}
          >
            {[20, 50, 100].map((size) => <option key={size} value={size}>{size}</option>)}
          </select>
          <span aria-live="polite">{t("page_number", { page: previousCursors.length + 1 })}</span>
          <Button size="sm" disabled={loading || previousCursors.length === 0} onClick={() => {
            setCursor(previousCursors.at(-1) ?? null);
            setPreviousCursors((current) => current.slice(0, -1));
          }}>{t("previous_page")}</Button>
          <Button size="sm" disabled={loading || !nextCursor} onClick={() => {
            if (!nextCursor) return;
            setPreviousCursors((current) => [...current, cursor]);
            setCursor(nextCursor);
          }}>{t("next_page")}</Button>
        </div> : null}
      </section>

      {adminsTarget ? (
        <Dialog
          title={t("manage_admins_title", { name: adminsTarget.name })}
          busy={busy}
          onClose={() => setAdminsTarget(null)}
          footer={<Button variant="ghost" disabled={busy} onClick={() => setAdminsTarget(null)}>{t("cancel")}</Button>}
        >
          <p className="legacy-tsa-admins-hint">{t("manage_admins_hint")}</p>
          <Field id="settings-team-spaces-new-admin" label={t("add_admin_label")}>
            <div className="legacy-tsa-admins-add">
              <select id="settings-team-spaces-new-admin" className="settings-input" value={newAdminId} disabled={adminsLoading || busy} onChange={(event) => setNewAdminId(event.target.value)}>
                <option value="">{t("add_admin_placeholder")}</option>
                {adminCandidates.filter((member) => !spaceAdmins.some((admin) => admin.user_id === member.user_id)).map((member) => (
                  <option key={member.user_id} value={member.user_id}>{member.display_name || member.email}</option>
                ))}
              </select>
              <Button variant="primary" disabled={!newAdminId || adminsLoading || busy} onClick={() => void grantAdmin()}>{t("add_admin")}</Button>
            </div>
          </Field>
          {adminsLoading ? <Notice tone="muted">{t("loading")}</Notice> : (
            <div className="legacy-tsa-admins-list">
              {spaceAdmins.map((admin) => (
                <div key={admin.user_id} className="legacy-tsa-admins-row">
                  <span className="legacy-tsa-admins-name">{admin.display_name || admin.email}</span>
                  <Button size="sm" variant="ghost" disabled={busy || spaceAdmins.length <= 1} onClick={() => void revokeAdmin(admin.user_id)}>{t("remove_admin")}</Button>
                </div>
              ))}
            </div>
          )}
        </Dialog>
      ) : null}
      {instructionsTarget ? (
        <SpaceInstructionsDialog
          workspaceId={instructionsTarget.workspace_id}
          name={instructionsTarget.name}
          onClose={() => setInstructionsTarget(null)}
        />
      ) : null}
      {restoreTarget && restoreTarget.admin_count > 0 ? (
        <ConfirmDialog
          title={t("restore")}
          description={t("restore_confirm", { name: restoreTarget.name })}
          confirmLabel={t("restore")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setRestoreTarget(null)}
          onConfirm={() => void restore(restoreTarget)}
        />
      ) : null}
      {restoreTarget && restoreTarget.admin_count === 0 ? (
        <Dialog
          title={t("restore")}
          busy={busy}
          onClose={() => setRestoreTarget(null)}
          footer={(
            <>
              <Button
                variant="ghost"
                disabled={busy}
                onClick={() => setRestoreTarget(null)}
              >
                {t("cancel")}
              </Button>
              <Button
                variant="primary"
                disabled={busy || recoveryLoading || !recoveryAdminId}
                onClick={() => void restore(restoreTarget)}
              >
                {t("restore")}
              </Button>
            </>
          )}
        >
          <p>{t("restore_recovery_detail", { name: restoreTarget.name })}</p>
          <Field
            id="settings-team-spaces-recovery-admin"
            label={t("restore_recovery_label")}
          >
            <select
              id="settings-team-spaces-recovery-admin"
              className="settings-input"
              value={recoveryAdminId}
              disabled={recoveryLoading}
              onChange={(event) => setRecoveryAdminId(event.target.value)}
            >
              <option value="">{t("restore_recovery_placeholder")}</option>
              {recoveryMembers.map((member) => (
                <option key={member.user_id} value={member.user_id}>
                  {member.display_name || member.email}
                </option>
              ))}
            </select>
          </Field>
        </Dialog>
      ) : null}
    </div>
  );
}

function formatError(
  caught: unknown,
  t: ReturnType<typeof useTranslations<"settings.workspaces">>,
): string {
  if (caught instanceof AdminSpacesError) {
    if (caught.code === "last_active_space") return t("err_last_active_space");
    if (caught.code === "duplicate_workspace_name") return t("err_duplicate_name");
    if (caught.code === "stale_workspace_update") return t("err_stale_update");
    if (caught.code === "restore_admin_required") return t("err_restore_admin_required");
    if (caught.code === "not_team_admin") return t("err_not_team_admin");
    if (caught.code === "last_space_admin") return t("err_last_space_admin");
  }
  return t("action_failed", {
    err: t("err_generic"),
  });
}
