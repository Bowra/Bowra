"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { fetchMe } from "@/lib/auth-api";
import { spaceMemberErrorKey } from "@/lib/space-member-error";
import { ErrorDiagnosticsInline } from "@/components/error-notice";
import { apiJson, apiVoid } from "@/lib/api-client";
import { TEAM_SETTINGS_ROLES, teamRoleForUser } from "@/lib/settings-navigation";
import { useAppRouter } from "@/lib/use-app-router";
import { useSpace } from "@/lib/use-space";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { ListRow, ListRows, useListRowsLayout } from "@/components/ui/list-rows";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { TableFrame } from "@/components/ui/table-frame";

type Role = "admin" | "member" | "viewer";
type Member = { user_id: string; role: Role; joined_at: string; email?: string; display_name?: string | null };
type Candidate = { user_id: string; email: string; display_name: string | null };

export function SettingsMembers() {
  const t = useTranslations("settings.members");
  // Below 640 use list rows instead of a table.
  const listLayout = useListRowsLayout();
  const router = useAppRouter();
  const workspace = useSpace();
  const [selectedWs, setSelectedWs] = useState("");
  const [members, setMembers] = useState<Member[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [candidateId, setCandidateId] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorCause, setErrorCause] = useState<unknown>();
  const loadRevision = useRef(0);
  const currentSpace = useRef(selectedWs);
  currentSpace.current = selectedWs;
  const failure = (caught: unknown, operation: "load_failed" | "add_failed" | "role_failed" | "remove_failed") => {
    const key = spaceMemberErrorKey(caught);
    setErrorCause(caught);
    setError(key === "err_generic" ? t(operation, { err: "" }) : t(key));
  };
  const [meId, setMeId] = useState("");
  const [canManageSpaces, setCanManageSpaces] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<string | null>(null);
  const ready = Boolean(selectedWs) && !workspace.loading && !workspace.error;

  useEffect(() => {
    if (workspace.loading || workspace.error) return;
    if (selectedWs && workspace.items.some((item) => item.id === selectedWs)) return;
    setSelectedWs(workspace.activeId && workspace.items.some((item) => item.id === workspace.activeId)
      ? workspace.activeId
      : workspace.items[0]?.id ?? "");
  }, [selectedWs, workspace.activeId, workspace.error, workspace.items, workspace.loading]);

  useEffect(() => {
    void fetchMe().then((me) => {
      setMeId(me.id);
      const teamRole = teamRoleForUser(me);
      setCanManageSpaces(teamRole !== null && TEAM_SETTINGS_ROLES.includes(teamRole));
    }).catch((caught) => failure(caught, "load_failed"));
  }, []);

  const load = useCallback(async () => {
    const revision = ++loadRevision.current;
    if (!ready) {
      setMembers([]);
      setCandidates([]);
      return;
    }
    setError(null);
    try {
      const [memberResult, candidateResult] = await Promise.all([
        apiJson<{ members?: Member[] }>(`/api/v1/workspaces/${encodeURIComponent(selectedWs)}/members`),
        apiJson<{ items?: Candidate[] }>(`/api/v1/workspaces/${encodeURIComponent(selectedWs)}/member-candidates`),
      ]);
      if (revision !== loadRevision.current || currentSpace.current !== selectedWs) return;
      setMembers(memberResult.members ?? []);
      const nextCandidates = candidateResult.items ?? [];
      setCandidates(nextCandidates);
      setCandidateId((current) => nextCandidates.some((item) => item.user_id === current) ? current : "");
    } catch (caught) {
      if (revision !== loadRevision.current || currentSpace.current !== selectedWs) return;
      const key = spaceMemberErrorKey(caught);
      setErrorCause(caught);
      setError(key === "err_generic" ? t("load_failed", { err: "" }) : t(key));
    }
  }, [ready, selectedWs, t]);
  useEffect(() => { void load(); }, [load]);

  const flash = (next: string, isError = false) => {
    if (isError) setError(next); else setMessage(next);
  };
  const addMember = async () => {
    if (!candidateId || !ready) return;
    ++loadRevision.current;
    setBusy(true);
    setError(null);
    setErrorCause(undefined);
    try {
      await apiVoid(`/api/v1/workspaces/${encodeURIComponent(selectedWs)}/members`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: candidateId, role }),
      });
      flash(t("member_added"));
      setCandidateId("");
      await load();
    } catch (caught) {
      if (currentSpace.current === selectedWs) failure(caught, "add_failed");
    } finally { setBusy(false); }
  };
  const updateRole = async (userId: string, nextRole: Role) => {
    ++loadRevision.current;
    setBusy(true);
    setError(null);
    setErrorCause(undefined);
    try {
      await apiVoid(`/api/v1/workspaces/${encodeURIComponent(selectedWs)}/members/${encodeURIComponent(userId)}/role`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role: nextRole }) });
      await load();
    } catch (caught) {
      if (currentSpace.current === selectedWs) failure(caught, "role_failed");
    } finally { setBusy(false); }
  };
  const removeMember = async () => {
    if (!removeTarget) return;
    ++loadRevision.current;
    setBusy(true);
    setError(null);
    setErrorCause(undefined);
    try {
      await apiVoid(`/api/v1/workspaces/${encodeURIComponent(selectedWs)}/members/${encodeURIComponent(removeTarget)}`, { method: "DELETE" });
      setRemoveTarget(null);
      await load();
    } catch (caught) {
      if (currentSpace.current === selectedWs) failure(caught, "remove_failed");
    } finally { setBusy(false); }
  };

  return <div className="legacy-members-page">
    <h1 className="ui-page-title legacy-members-title">{t("title")}</h1><p className="ui-page-desc legacy-members-subtitle">{t("subtitle")}</p>
    {message ? <Notice tone="success">{message}</Notice> : null}
    {error && !removeTarget ? <><Notice tone="error">{error}</Notice><ErrorDiagnosticsInline error={errorCause} surface="settings" /></> : null}
    <Field id="settings-members-workspace" label={t("workspace_label")} className="legacy-space-members-workspace-field">
      <select id="settings-members-workspace" value={selectedWs} onChange={(event) => setSelectedWs(event.target.value)} disabled={busy || workspace.loading || !workspace.items.length} className="ui-input legacy-input-members">
        {workspace.items.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
    </Field>
    {!workspace.loading && !workspace.error && !selectedWs ? <Notice tone="muted">
      {canManageSpaces ? <Button size="sm" onClick={() => router.push("/team/settings/spaces/")}>{t("open_workspaces")}</Button> : t("workspace_contact_admin")}
    </Notice> : null}
    {ready ? <>
      <section className="ui-section-card legacy-members-section ui-members-group ui-members-add">
        <h2 className="ui-section-title legacy-members-section-title">{t("add_existing_member")}</h2><p className="ui-section-desc legacy-members-subtitle">{t("add_existing_member_hint")}</p>
        <div className="legacy-members-add-row ui-members-add-row">
          <Field id="settings-members-candidate" label={t("member_label")} className="legacy-space-members-candidate-field">
            <select id="settings-members-candidate" value={candidateId} onChange={(event) => setCandidateId(event.target.value)} disabled={busy || !candidates.length} className="ui-input legacy-input-members">
              <option value="">{candidates.length ? t("select_member") : t("no_member_candidates")}</option>
              {candidates.map((item) => <option key={item.user_id} value={item.user_id}>{item.display_name ? `${item.display_name} · ${item.email}` : item.email}</option>)}
            </select>
          </Field>
          <Field id="settings-members-role" label={t("role_label")}><select id="settings-members-role" value={role} onChange={(event) => setRole(event.target.value as Role)} className="ui-input legacy-input-members"><RoleOptions t={t} /></select></Field>
          <Button variant="primary" disabled={busy || !candidateId} onClick={() => void addMember()}>{t("add_member")}</Button>
        </div>
        <p className="ui-section-desc legacy-members-subtitle legacy-members-subtitle--last">{t("manage_team_members_hint")}</p>
      </section>
      <section className="ui-section-card legacy-members-section ui-members-group ui-members-list"><h2 className="ui-section-title legacy-members-section-title">{t("active_members_title", { count: members.length })}</h2>
        {listLayout ? (
          // Mobile (< 640): list rows replace table (decision 12); name/email occupy two heading lines, joined date a third, role dropdown at row end, Remove in ⋯.
          <ListRows label={t("active_members_title", { count: members.length })} className="ui-members-rows">
            {members.map((member) => {
              const lastAdmin = member.role === "admin" && members.filter((item) => item.role === "admin").length === 1;
              const locked = member.user_id === meId && lastAdmin;
              const name = member.display_name || member.email || member.user_id;
              // Ellipsize long email in the row; retain full node text for screen readers and expose it on hover (same contract as credit list rows).
              return <ListRow key={member.user_id} title={<span title={name}>{name}</span>}
                subtitle={member.display_name && member.email ? <span title={member.email}>{member.email}</span> : undefined}
                meta={<span className="ui-tabular">{t("table.joined")} {formatDate(member.joined_at)}</span>}
                status={<select aria-label={t("role_for", { userId: member.email || member.user_id })} value={member.role} disabled={busy || locked} onChange={(event) => void updateRole(member.user_id, event.target.value as Role)} className="ui-input legacy-input-members"><RoleOptions t={t} /></select>}
                actions={<RowActionsMenu label={t("more_actions_for", { name })} pending={busy} actions={[{ id: "remove", label: t("remove"), danger: true, disabled: busy || locked, disabledReason: locked ? t("remove_last_admin_reason") : undefined, onSelect: () => setRemoveTarget(member.user_id) }]} />} />;
            })}
          </ListRows>
        ) : <TableFrame><table className="ui-table legacy-members-table"><thead><tr><th className="legacy-members-cell">{t("table.member")}</th><th className="legacy-members-cell">{t("table.role")}</th><th className="legacy-members-cell">{t("table.joined")}</th><th className="legacy-members-cell ui-table-actions" /></tr></thead><tbody>{members.map((member) => {
          const lastAdmin = member.role === "admin" && members.filter((item) => item.role === "admin").length === 1;
          const locked = member.user_id === meId && lastAdmin;
          return <tr key={member.user_id}><td className="legacy-members-cell">{member.display_name || member.email || member.user_id}</td><td className="legacy-members-cell"><select aria-label={t("role_for", { userId: member.email || member.user_id })} value={member.role} disabled={busy || locked} onChange={(event) => void updateRole(member.user_id, event.target.value as Role)} className="ui-input legacy-input-members"><RoleOptions t={t} /></select></td><td className="legacy-members-cell ui-tabular">{formatDate(member.joined_at)}</td><td className="legacy-members-cell ui-table-actions"><RowActionsMenu label={t("more_actions_for", { name: member.display_name || member.email || member.user_id })} pending={busy} actions={[{ id: "remove", label: t("remove"), danger: true, disabled: busy || locked, disabledReason: locked ? t("remove_last_admin_reason") : undefined, onSelect: () => setRemoveTarget(member.user_id) }]} /></td></tr>;
        })}</tbody></table></TableFrame>}
      </section>
    </> : null}
    {removeTarget ? <ConfirmDialog title={t("confirm_remove_title")} description={t("confirm_remove_desc", { userId: removeTarget })} confirmLabel={t("remove")} cancelLabel={t("cancel")} busy={busy} onCancel={() => setRemoveTarget(null)} onConfirm={() => void removeMember()}>{error ? <><Notice tone="error">{error}</Notice><ErrorDiagnosticsInline error={errorCause} surface="settings" /></> : null}</ConfirmDialog> : null}
  </div>;
}

function RoleOptions({ t }: { t: ReturnType<typeof useTranslations<"settings.members">> }) { return <><option value="admin">{t("role_admin")}</option><option value="member">{t("role_member")}</option><option value="viewer">{t("role_viewer")}</option></>; }
function formatDate(value: string) { return value ? new Intl.DateTimeFormat(undefined, { year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(value)) : "—"; }

