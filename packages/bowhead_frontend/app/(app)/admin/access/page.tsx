"use client";

import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { AdminShell } from "@/components/admin-shell";
import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { AdminRouteState } from "@/components/shell/AdminRouteState";
import { AccessWhoCanHelpWhenEnabled } from "@/components/access/access-request-panel";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ListRow, ListRows, useListRowsLayout } from "@/components/ui/list-rows";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import {
  adminAccessErrorKey,
  type AccessGroup,
  type AccessGroupMember,
  type PermissionExplainResult,
  type ResourceGrant,
  addGroupMember,
  createAccessGroup,
  createResourceGrant,
  explainPermission,
  fetchAccessGroups,
  fetchGroupMembers,
  fetchResourceGrants,
  removeGroupMember,
  revokeResourceGrant,
} from "@/lib/admin-access-api";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { keepFocusAfterRemoval } from "@/lib/focus-after-removal";

const RESOURCE_KINDS = [
  "kb_scope",
  "kb_file",
  "skill",
  "team_skill",
  "team_tool_source",
  "team_tool_op",
  "builtin_capability",
  "tool",
];
const PRINCIPAL_TYPES = ["user", "group", "workspace", "team"];
const PERMISSIONS = ["view", "read", "use", "invoke", "edit", "write", "manage", "share", "admin"];
const SKILL_RESOURCE_KINDS = new Set(["skill", "team_skill"]);
const BUILTIN_CAPABILITIES = [
  "kb_search",
  "skill_invoke",
  "web_search",
  "web_fetch",
  "export",
  "python_sandbox",
  "subagent",
];

// Original inline compactInput / actionButton styles moved to legacy classes (styles/ui-controls-legacy.css “逐页验收 B”);
// Inputs and buttons use the batch-1 appearance (styles/ui-refresh-pages-admin.css).
const INPUT_CLASS = "ui-input legacy-access-input";
const BUTTON_CLASS = "ui-btn ui-btn--primary legacy-access-btn";
const REFRESH_BUTTON_CLASS = "ui-btn legacy-access-btn";

// Match backend SHAREABLE_KINDS: revoking a team-wide allow grant on these resources uses set_audience(custom),
// revoking all team-wide allow grants on the resource together (routes/access_control.py revoke_resource_grant).
const TEAM_AUDIENCE_KINDS = new Set(["team_subagent", "team_skill", "report"]);

type GrantConfirmTarget = {
  id: string;
  principal: string;
  resource: string;
  permission: string;
  /** allow: recipient loses permission; deny: prohibition lifted, other grants may restore access; team_audience: revoke all team-wide grants together. */
  kind: "allow" | "deny" | "team_audience";
};

function grantConfirmTarget(g: {
  id: string;
  principal_type: string;
  principal_id: string;
  resource_kind: string;
  resource_id: string;
  permission: string;
  effect: string;
}): GrantConfirmTarget {
  const kind = g.effect === "deny"
    ? "deny"
    : g.principal_type === "team" && TEAM_AUDIENCE_KINDS.has(g.resource_kind)
      ? "team_audience"
      : "allow";
  return {
    id: g.id,
    principal: `${g.principal_type} ${shortId(g.principal_id)}`,
    resource: `${g.resource_kind} ${shortId(g.resource_id)}`,
    permission: g.permission,
    kind,
  };
}

const REVOKE_BODY_KEY: Record<GrantConfirmTarget["kind"], string> = {
  allow: "revoke_grant_confirm_body",
  deny: "revoke_deny_confirm_body",
  team_audience: "revoke_team_grant_confirm_body",
};

function shortId(id: string | null | undefined): string {
  if (!id) return "-";
  return id.length > 22 ? `${id.slice(0, 10)}...${id.slice(-6)}` : id;
}

type TFn = (key: string, values?: Record<string, string | number>) => string;

function resourcePlaceholder(kind: string, t: TFn): string {
  if (kind === "kb_scope") return t("ph_kb_scope");
  if (kind === "kb_file") return t("ph_kb_file");
  if (kind === "builtin_capability") return t("ph_builtin");
  if (kind === "team_tool_op") return t("ph_team_tool_op");
  return t("ph_resource_id");
}

function permissionsForResource(kind: string): string[] {
  return SKILL_RESOURCE_KINDS.has(kind) ? PERMISSIONS : PERMISSIONS.filter((permission) => permission !== "invoke");
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="access-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

export default function AdminAccessPage() {
  const t = useTranslations("admin.access");
  // Localize reasons from status and known backend detail; status/error codes and request ID go only into diagnostics.
  const showFailure = (err: unknown) => setError(shownError(t(adminAccessErrorKey(err)), err));
  // Mobile width (< 640) turns both tables into list rows (decision 12).
  const listLayout = useListRowsLayout();
  const [pendingRemoval, setPendingRemoval] = useState<{ userId: string; name: string } | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<GrantConfirmTarget | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  // Success removes the row; return focus to its section heading, not the outermost page.
  const membersHeadingRef = useRef<HTMLHeadingElement>(null);
  const grantsHeadingRef = useRef<HTMLHeadingElement>(null);
  const runConfirmed = async (
    action: () => Promise<unknown>,
    close: () => void,
    focusAfter: () => HTMLElement | null,
  ) => {
    if (confirmBusy) return;
    setConfirmBusy(true);
    setError(null);
    try {
      await action();
      keepFocusAfterRemoval(focusAfter);
      reload();
    } catch (err) {
      showFailure(err);
    } finally {
      setConfirmBusy(false);
      close();
    }
  };
  const auth = useAuthRedirect({ requireTeamAdmin: true });

  const [groups, setGroups] = useState<AccessGroup[]>([]);
  const [grants, setGrants] = useState<ResourceGrant[]>([]);
  const [selectedGroupId, setSelectedGroupId] = useState("");
  const [members, setMembers] = useState<AccessGroupMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ShownError | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const [groupForm, setGroupForm] = useState({
    name: "",
    description: "",
    workspace_id: "",
  });
  const [memberForm, setMemberForm] = useState({ user_id: "", role: "member" });
  const [grantForm, setGrantForm] = useState({
    resource_kind: "kb_scope",
    resource_id: "",
    principal_type: "group",
    principal_id: "",
    permission: "read",
    effect: "allow",
    expires_at: "",
  });
  const [explainForm, setExplainForm] = useState({
    workspace_id: "",
    resource_kind: "kb_scope",
    resource_id: "",
    permission: "read",
  });
  const [explainResult, setExplainResult] = useState<PermissionExplainResult | null>(null);
  const selectedGroup = useMemo(
    () => groups.find((g) => g.id === selectedGroupId) ?? groups[0] ?? null,
    [groups, selectedGroupId],
  );

  const reload = useCallback(() => setRefreshTick((t) => t + 1), []);

  useEffect(() => {
    if (auth.status !== "authed") return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([fetchAccessGroups(), fetchResourceGrants()])
      .then(([groupResp, grantResp]) => {
        if (cancelled) return;
        setGroups(groupResp.items);
        setGrants(grantResp.items);
        setSelectedGroupId((prev) => prev || groupResp.items[0]?.id || "");
      })
      .catch((e: unknown) => {
        if (!cancelled) showFailure(e);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [auth.status, refreshTick]);

  useEffect(() => {
    const groupId = selectedGroup?.id;
    if (!groupId) {
      setMembers([]);
      return;
    }
    let cancelled = false;
    fetchGroupMembers(groupId)
      .then((resp) => {
        if (!cancelled) setMembers(resp.items);
      })
      .catch(() => {
        if (!cancelled) setMembers([]);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedGroup?.id, refreshTick]);

  async function onCreateGroup(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await createAccessGroup({
        name: groupForm.name.trim(),
        description: groupForm.description.trim(),
        workspace_id: groupForm.workspace_id.trim() || null,
      });
      setGroupForm({ name: "", description: "", workspace_id: "" });
      reload();
    } catch (err) {
      showFailure(err);
    }
  }

  async function onAddMember(e: FormEvent) {
    e.preventDefault();
    if (!selectedGroup) return;
    setError(null);
    try {
      await addGroupMember(selectedGroup.id, {
        user_id: memberForm.user_id.trim(),
        role: memberForm.role,
      });
      setMemberForm({ user_id: "", role: "member" });
      reload();
    } catch (err) {
      showFailure(err);
    }
  }

  async function onCreateGrant(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await createResourceGrant({
        ...grantForm,
        resource_id: grantForm.resource_id.trim(),
        principal_id: grantForm.principal_id.trim(),
        expires_at: grantForm.expires_at.trim() || null,
      });
      setGrantForm((f) => ({ ...f, resource_id: "", principal_id: "", expires_at: "" }));
      reload();
    } catch (err) {
      showFailure(err);
    }
  }

  async function onExplain(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const result = await explainPermission({
        ...explainForm,
        workspace_id: explainForm.workspace_id.trim(),
        resource_id: explainForm.resource_id.trim(),
      });
      setExplainResult(result);
    } catch (err) {
      showFailure(err);
    }
  }

  if (auth.status === "loading" || auth.status === "redirecting") {
    return (
      <AdminShell current="access" mobileTitle={t("title")}>
        <AdminRouteState title={t("title")}>{t("loading")}</AdminRouteState>
      </AdminShell>
    );
  }

  if (auth.status !== "authed") {
    return (
      <AdminShell current="access" mobileTitle={t("title")}>
        <AdminRouteState title={t("title")} tone="forbidden" aside={<AccessWhoCanHelpWhenEnabled scope="team" />}>
          {t("not_admin")}
        </AdminRouteState>
      </AdminShell>
    );
  }

  return (
    <AdminShell current="access" mobileTitle={t("title")}>
      <div className="admin-config-page access-page">
        <header className="admin-config-page__header access-header">
          <div>
            <h1 className="ui-page-title">{t("title")}</h1>
            <p className="ui-page-desc">{t("subtitle")}</p>
          </div>
          <button type="button" className={REFRESH_BUTTON_CLASS} onClick={reload}>
            {t("refresh")}
          </button>
        </header>

        {error ? <div className="access-error" role="alert">{error.message}</div> : null}
        {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
        {loading ? (
          <LoadingSkeleton rows={3} label={t("loading")} />
        ) : null}

        <section className="access-grid">
          <div className="access-panel">
            <div className="access-panel-head">
              <h2>{t("groups")}</h2>
              <span>{groups.length}</span>
            </div>
            <form className="access-form" onSubmit={onCreateGroup}>
              <Field label={t("name")}>
                <input
                  required
                  className={INPUT_CLASS}
                  value={groupForm.name}
                  onChange={(e) => setGroupForm((f) => ({ ...f, name: e.target.value }))}
                />
              </Field>
              <Field label={t("space_id")}>
                <input
                  className={INPUT_CLASS}
                  value={groupForm.workspace_id}
                  onChange={(e) => setGroupForm((f) => ({ ...f, workspace_id: e.target.value }))}
                />
              </Field>
              <Field label={t("description")}>
                <input
                  className={INPUT_CLASS}
                  value={groupForm.description}
                  onChange={(e) => setGroupForm((f) => ({ ...f, description: e.target.value }))}
                />
              </Field>
              <button type="submit" className={BUTTON_CLASS}>{t("create")}</button>
            </form>
            <div className="access-list">
              {groups.map((g) => (
                <button
                  key={g.id}
                  type="button"
                  className={selectedGroup?.id === g.id ? "selected" : ""}
                  onClick={() => setSelectedGroupId(g.id)}
                >
                  <strong>{g.name}</strong>
                  <span>{shortId(g.workspace_id ?? "team-wide")} · {t("users_suffix", { count: g.member_count })}</span>
                </button>
              ))}
              {groups.length === 0 ? <div className="access-muted">{t("no_groups")}</div> : null}
            </div>
          </div>

          <div className="access-panel">
            <div className="access-panel-head">
              <h2 ref={membersHeadingRef} tabIndex={-1}>{t("members")}</h2>
              <span>{selectedGroup ? selectedGroup.name : "-"}</span>
            </div>
            <form className="access-form" onSubmit={onAddMember}>
              <Field label={t("user_id")}>
                <input
                  required
                  className={INPUT_CLASS}
                  value={memberForm.user_id}
                  onChange={(e) => setMemberForm((f) => ({ ...f, user_id: e.target.value }))}
                />
              </Field>
              <Field label={t("role")}>
                <select
                  className={INPUT_CLASS}
                  value={memberForm.role}
                  onChange={(e) => setMemberForm((f) => ({ ...f, role: e.target.value }))}
                >
                  <option value="member">{t("role_member")}</option>
                  <option value="manager">{t("role_manager")}</option>
                </select>
              </Field>
              <button type="submit" className={BUTTON_CLASS} disabled={!selectedGroup}>{t("add")}</button>
            </form>
            <div className="access-table-wrap ui-table-frame">
              {listLayout ? (
                <ListRows label={t("members")} className="access-list-rows">
                  {members.map((m) => (
                    <ListRow
                      key={m.user_id}
                      title={m.email || shortId(m.user_id)}
                      subtitle={m.display_name || m.user_id}
                      status={<span className="access-role-badge">{t(m.role === "manager" ? "role_manager" : "role_member")}</span>}
                      actions={
                        <RowActionsMenu
                          label={t("more_actions_for", { name: m.email || shortId(m.user_id) })}
                          actions={[{
                            id: "remove",
                            label: t("remove"),
                            danger: true,
                            onSelect: () => setPendingRemoval({ userId: m.user_id, name: m.email || shortId(m.user_id) }),
                          }]}
                        />
                      }
                    />
                  ))}
                </ListRows>
              ) : (
              <table className="access-table ui-table">
                <thead>
                  <tr>
                    <th>{t("col_user")}</th>
                    <th>{t("col_role")}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.user_id}>
                      <td>
                        <strong>{m.email || shortId(m.user_id)}</strong>
                        <span>{m.display_name || m.user_id}</span>
                      </td>
                      <td>{t(m.role === "manager" ? "role_manager" : "role_member")}</td>
                      <td className="ui-table-actions">
                        <RowActionsMenu
                          label={t("more_actions_for", { name: m.email || shortId(m.user_id) })}
                          actions={[{
                            id: "remove",
                            label: t("remove"),
                            danger: true,
                            onSelect: () => setPendingRemoval({ userId: m.user_id, name: m.email || shortId(m.user_id) }),
                          }]}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              )}
              {members.length === 0 ? <div className="access-muted">{t("no_members")}</div> : null}
            </div>
          </div>
        </section>

        <section className="access-panel access-wide">
          <div className="access-panel-head">
            <h2 ref={grantsHeadingRef} tabIndex={-1}>{t("resource_grants")}</h2>
            <span>{grants.length}</span>
          </div>
          <form className="grant-form" onSubmit={onCreateGrant}>
            <select
              className={INPUT_CLASS}
              value={grantForm.resource_kind}
              onChange={(e) => {
                const resourceKind = e.target.value;
                setGrantForm((f) => ({
                  ...f,
                  resource_kind: resourceKind,
                  permission: f.permission === "invoke" && !SKILL_RESOURCE_KINDS.has(resourceKind) ? "use" : f.permission,
                }));
              }}
            >
              {RESOURCE_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <input
              required
              className={INPUT_CLASS}
              placeholder="resource_id"
              title={resourcePlaceholder(grantForm.resource_kind, t)}
              list={grantForm.resource_kind === "builtin_capability" ? "builtin-capability-options" : undefined}
              value={grantForm.resource_id}
              onChange={(e) => setGrantForm((f) => ({ ...f, resource_id: e.target.value }))}
            />
            <datalist id="builtin-capability-options">
              {BUILTIN_CAPABILITIES.map((capability) => (
                <option key={capability} value={capability} />
              ))}
            </datalist>
            <select
              className={INPUT_CLASS}
              value={grantForm.principal_type}
              onChange={(e) => setGrantForm((f) => ({ ...f, principal_type: e.target.value }))}
            >
              {PRINCIPAL_TYPES.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <input
              required
              className={INPUT_CLASS}
              placeholder="principal_id"
              value={grantForm.principal_id}
              onChange={(e) => setGrantForm((f) => ({ ...f, principal_id: e.target.value }))}
            />
            <select
              className={INPUT_CLASS}
              value={grantForm.permission}
              onChange={(e) => setGrantForm((f) => ({ ...f, permission: e.target.value }))}
            >
              {permissionsForResource(grantForm.resource_kind).map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <select
              className={INPUT_CLASS}
              value={grantForm.effect}
              onChange={(e) => setGrantForm((f) => ({ ...f, effect: e.target.value }))}
            >
              <option value="allow">{t("effect_allow")}</option>
              <option value="deny">{t("effect_deny")}</option>
            </select>
            <input
              className={INPUT_CLASS}
              placeholder="expires_at"
              value={grantForm.expires_at}
              onChange={(e) => setGrantForm((f) => ({ ...f, expires_at: e.target.value }))}
            />
            <button type="submit" className={BUTTON_CLASS}>{t("save")}</button>
          </form>
          <div className="access-table-wrap ui-table-frame">
            {listLayout ? (
              <ListRows label={t("resource_grants")} className="access-list-rows">
                {grants.map((g) => (
                  <ListRow
                    key={g.id}
                    title={`${g.resource_kind} · ${shortId(g.resource_id)}`}
                    subtitle={`${g.principal_type} ${shortId(g.principal_id)}`}
                    meta={g.permission}
                    status={
                      <span className={g.effect === "deny" ? "pill deny" : "pill allow"}>
                        {t(g.effect === "deny" ? "effect_deny" : "effect_allow")}
                      </span>
                    }
                    actions={
                      <RowActionsMenu
                        label={t("more_actions_for", { name: `${g.resource_kind} ${shortId(g.resource_id)}` })}
                        actions={[{
                          id: "revoke",
                          label: t("revoke"),
                          danger: true,
                          onSelect: () => setPendingRevoke(grantConfirmTarget(g)),
                        }]}
                      />
                    }
                  />
                ))}
              </ListRows>
            ) : (
            <table className="access-table ui-table">
              <thead>
                <tr>
                  <th>{t("col_resource")}</th>
                  <th>{t("col_principal")}</th>
                  <th>{t("col_permission")}</th>
                  <th>{t("col_effect")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {grants.map((g) => (
                  <tr key={g.id}>
                    <td>
                      <strong>{g.resource_kind}</strong>
                      <span>{shortId(g.resource_id)}</span>
                    </td>
                    <td>
                      <strong>{g.principal_type}</strong>
                      <span>{shortId(g.principal_id)}</span>
                    </td>
                    <td>{g.permission}</td>
                    <td>
                      <span className={g.effect === "deny" ? "pill deny" : "pill allow"}>
                        {t(g.effect === "deny" ? "effect_deny" : "effect_allow")}
                      </span>
                    </td>
                    <td className="ui-table-actions">
                      <RowActionsMenu
                        label={t("more_actions_for", { name: `${g.resource_kind} ${shortId(g.resource_id)}` })}
                        actions={[{
                          id: "revoke",
                          label: t("revoke"),
                          danger: true,
                          onSelect: () => setPendingRevoke(grantConfirmTarget(g)),
                        }]}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            )}
          </div>
        </section>

        <section className="access-panel access-wide">
          <div className="access-panel-head">
            <h2>{t("explain")}</h2>
            <span>{explainResult ? explainResult.reason : t("idle")}</span>
          </div>
          <form className="grant-form explain-form" onSubmit={onExplain}>
            <input
              required
              className={INPUT_CLASS}
              placeholder={t("space_id_placeholder")}
              value={explainForm.workspace_id}
              onChange={(e) => setExplainForm((f) => ({ ...f, workspace_id: e.target.value }))}
            />
            <select
              className={INPUT_CLASS}
              value={explainForm.resource_kind}
              onChange={(e) => {
                const resourceKind = e.target.value;
                setExplainForm((f) => ({
                  ...f,
                  resource_kind: resourceKind,
                  permission: f.permission === "invoke" && !SKILL_RESOURCE_KINDS.has(resourceKind) ? "use" : f.permission,
                }));
              }}
            >
              {RESOURCE_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <input
              required
              className={INPUT_CLASS}
              placeholder="resource_id"
              title={resourcePlaceholder(explainForm.resource_kind, t)}
              list={explainForm.resource_kind === "builtin_capability" ? "builtin-capability-options" : undefined}
              value={explainForm.resource_id}
              onChange={(e) => setExplainForm((f) => ({ ...f, resource_id: e.target.value }))}
            />
            <select
              className={INPUT_CLASS}
              value={explainForm.permission}
              onChange={(e) => setExplainForm((f) => ({ ...f, permission: e.target.value }))}
            >
              {permissionsForResource(explainForm.resource_kind).map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <button type="submit" className={BUTTON_CLASS}>{t("explain")}</button>
          </form>
          {explainResult ? (
            <div className="explain-result">
              <span className={explainResult.allowed ? "status-ok" : "status-stop"}>
                {explainResult.allowed ? t("allowed") : t("denied")}
              </span>
              <code>{explainResult.reason}</code>
              {explainResult.shadow ? <span className="pill shadow">{t("shadow_badge")}</span> : null}
              <span>{t("role_meta", { role: explainResult.access_context.workspace_role || "-" })}</span>
              <span>{t("groups_meta", { count: (explainResult.access_context.group_ids || []).length })}</span>
              <span>{t("epoch_meta", { epoch: explainResult.access_context.policy_epoch ?? 0 })}</span>
            </div>
          ) : null}
        </section>
      </div>

      {pendingRemoval && selectedGroup ? (
        <ConfirmDialog
          title={t("remove_member_confirm_title", { name: pendingRemoval.name })}
          description={t("remove_member_confirm_body")}
          confirmLabel={t("remove")}
          cancelLabel={t("cancel")}
          busy={confirmBusy}
          onCancel={() => setPendingRemoval(null)}
          onConfirm={() =>
            void runConfirmed(
              () => removeGroupMember(selectedGroup.id, pendingRemoval.userId),
              () => setPendingRemoval(null),
              () => membersHeadingRef.current,
            )
          }
        />
      ) : null}
      {pendingRevoke ? (
        <ConfirmDialog
          title={t("revoke_grant_confirm_title")}
          description={t(REVOKE_BODY_KEY[pendingRevoke.kind], {
            principal: pendingRevoke.principal,
            resource: pendingRevoke.resource,
            permission: pendingRevoke.permission,
          })}
          confirmLabel={t("revoke")}
          cancelLabel={t("cancel")}
          busy={confirmBusy}
          onCancel={() => setPendingRevoke(null)}
          onConfirm={() =>
            void runConfirmed(
              () => revokeResourceGrant(pendingRevoke.id),
              () => setPendingRevoke(null),
              () => grantsHeadingRef.current,
            )
          }
        />
      ) : null}

      <style jsx>{`
        .access-page {
          box-sizing: border-box;
          width: 100%;
          min-width: 0;
          color: var(--c-ink);
        }
        .access-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 16px;
          margin-bottom: 18px;
        }
        .access-grid {
          display: grid;
          grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
          gap: 14px;
          margin-bottom: 14px;
        }
        .access-panel {
          border: 1px solid var(--c-line);
          border-radius: 8px;
          background: var(--c-surface);
          padding: 14px;
          min-width: 0;
        }
        .access-wide {
          margin-bottom: 14px;
        }
        .access-panel-head {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 12px;
          margin-bottom: 12px;
        }
        .access-panel-head h2 {
          margin: 0;
          font-size: 0.9375rem;
          font-weight: 620;
          letter-spacing: 0;
        }
        .access-panel-head span,
        .access-muted {
          color: var(--c-ink-3);
          font-size: 0.75rem;
        }
        .access-form,
        .grant-form {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 8px;
          align-items: end;
          margin-bottom: 12px;
        }
        .grant-form {
          grid-template-columns: 140px minmax(160px, 1fr) 130px minmax(160px, 1fr) 110px 90px minmax(120px, 1fr) auto;
        }
        .explain-form {
          grid-template-columns: minmax(140px, 1fr) 140px minmax(160px, 1fr) 110px auto;
        }
        .access-field {
          display: grid;
          gap: 4px;
          min-width: 0;
        }
        .access-field span {
          font-size: 0.6875rem;
          color: var(--c-ink-3);
        }
        .access-list {
          display: grid;
          gap: 6px;
          max-height: 280px;
          overflow: auto;
        }
        .access-list button {
          display: flex;
          justify-content: space-between;
          gap: 10px;
          width: 100%;
          text-align: left;
          border: 1px solid var(--c-line);
          border-radius: 6px;
          padding: 8px 10px;
          background: transparent;
          color: var(--c-ink);
          cursor: pointer;
          min-width: 0;
        }
        .access-list button.selected {
          background: var(--c-surface-2);
        }
        .access-list strong,
        .access-list span,
        .access-table span,
        .access-table strong {
          overflow-wrap: anywhere;
        }
        .access-list span {
          color: var(--c-ink-3);
          font-size: 0.75rem;
        }
        .access-table-wrap {
          overflow: auto;
          border: 1px solid var(--c-line);
          border-radius: 8px;
        }
        .access-table {
          width: 100%;
          border-collapse: collapse;
          min-width: 560px;
          font-size: 0.8125rem;
        }
        .access-table th {
          text-align: left;
          color: var(--c-ink-3);
          font-weight: 500;
          padding: 8px 10px;
          border-bottom: 1px solid var(--c-line);
          white-space: nowrap;
        }
        .access-table td {
          padding: 9px 10px;
          border-bottom: 1px solid var(--c-line);
          vertical-align: top;
        }
        .access-table tr:last-child td {
          border-bottom: 0;
        }
        .access-table td > span {
          display: block;
          color: var(--c-ink-3);
          font-size: 0.75rem;
          margin-top: 2px;
        }
        .text-button {
          border: 0;
          background: transparent;
          color: var(--c-accent);
          cursor: pointer;
          font-size: 0.75rem;
          padding: 0;
        }
        .pill {
          display: inline-flex;
          align-items: center;
          height: 22px;
          padding: 0 8px;
          border-radius: 999px;
          font-size: 0.75rem;
          border: 1px solid var(--c-line);
        }
        .pill.allow {
          color: var(--c-pos-ink);
          background: var(--c-pos-soft);
        }
        .pill.deny,
        .status-stop {
          color: var(--c-err-ink);
        }
        .pill.deny {
          background: var(--c-err-soft);
        }
        .pill.shadow {
          color: var(--c-ink-3);
          background: var(--c-surface-2);
        }
        .explain-result {
          display: flex;
          flex-wrap: wrap;
          gap: 10px;
          align-items: center;
          font-size: 0.8125rem;
          padding-top: 2px;
        }
        .explain-result code {
          border: 1px solid var(--c-line);
          border-radius: 6px;
          padding: 3px 6px;
          background: var(--c-surface-2);
        }
        .status-ok {
          color: var(--c-pos-ink);
          font-weight: 650;
        }
        .access-error {
          border: 1px solid var(--c-err-line);
          background: var(--c-err-soft);
          color: var(--c-err-ink);
          border-radius: 8px;
          padding: 10px 12px;
          margin-bottom: 14px;
          font-size: 0.8125rem;
        }
        .access-placeholder {
          padding: 40px;
          text-align: center;
          color: var(--c-ink-3);
        }
        @media (max-width: 900px) {
          .access-header {
            flex-direction: column;
            align-items: stretch;
          }
          .access-header > button {
            align-self: flex-start;
          }
          .access-grid {
            grid-template-columns: minmax(0, 1fr);
          }
          .access-form,
          .grant-form,
          .explain-form {
            grid-template-columns: minmax(0, 1fr);
          }
          .access-form > *,
          .grant-form > *,
          .explain-form > * {
            min-width: 0;
            max-width: 100%;
          }
        }
      `}</style>
    </AdminShell>
  );
}
