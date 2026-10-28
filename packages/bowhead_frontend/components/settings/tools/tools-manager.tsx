"use client";

/**
 * /settings/tools — unified Tools management (tools-management-page sprint WP3)
 *
 * Manage team external tool sources available to agents (OpenAPI sources + remote MCP servers).
 * User terms (Q3 decision 2026-07-14): Data sources managed in team settings; currently Datarc supported.
 * Tool source = team_tool_source (this page); never conflate the terms.
 * Mental model: one source = N operations; admin manages source/selects imported operations.
 * OpenAPI/MCP cards share actions: enable/disable / validate / edit / delete / expand operation list.
 * MCP cards also offer Refresh Tool List (source-refresh-dialog.tsx: preview the server diff, then apply)
 * and Choose Tools (source-tool-selection-dialog.tsx: re-pick imported tools from the latest snapshot).
 * OAuth MCP also has Authorize (WP2b-3): click → backend initiates authorization → browser to OAuth provider
 * → callback redirects here with ?oauth=success|error (top banner; success refreshes tools).
 *
 * Covers list / patch(enable·rename·credentials) / validate / delete / authorize; creation uses existing CreateToolCard
 * (OpenAPI/open MCP connected; OAuth MCP create-and-authorize wizard awaiting design).
 *
 * Page footer also shows read-only **built-in Tools** (capability-surface-audit 2026-09-06 step 2):
 * previously external sources only, no place to see Bowhead's own capabilities,
 * so zero built-in usage was no demand signal (7 of 35 had zero calls over 120 days).
 * Data from `tool_aliases.yaml` generated table, shared with steps/chips/agent creation options;
 * only manifest entries with `kind: "tool"` are listed (system parts are hidden).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { builtinTools, type BuiltinTool } from "@/lib/tool-display";
import { fetchMe } from "@/lib/auth-api";
import { DatarcMark, PowerBiMark, TavilyMark } from "@/components/settings/data-source-icons";
import { ShopifyToolSection } from "@/components/settings/tools/shopify-tool-section";
import { PowerBiToolSection } from "@/components/settings/tools/powerbi-tool-section";
import { GoogleMark, GoogleWorkspaceToolSection } from "@/components/settings/tools/google-workspace-tool-section";
import { SourceRefreshDialog } from "@/components/settings/tools/source-refresh-dialog";
import {
  SourceToolSelectionDialog,
  type ToolSelectionSaved,
} from "@/components/settings/tools/source-tool-selection-dialog";
import type { ToolSourceRefreshResult } from "@/lib/team-tool-source-refresh-api";
import {
  configureTavily,
  createDatarcTool,
  deleteDatarcTool,
  listBuiltinTools,
  refreshDatarcScope,
  setDatarcEnabled,
  setTavilyEnabled,
  updateDatarcTool,
  type TeamBuiltinTool,
} from "@/lib/team-builtin-tools-api";

import {
  authorizeSource,
  deleteSource,
  getSourceUsage,
  listSources,
  patchSource,
  previewSourceImpact,
  validateSource,
  TeamToolApiError,
  type AuthType,
  type TeamToolSourcePublic,
  type ResourceChangeImpact,
  type ValidateResult,
} from "@/lib/team-tool-sources-api";
import {
  ResourceUsageDrawer,
  UsageSummaryInline,
} from "@/components/settings/resource-usage";
import { CapabilityRepairContext } from "@/components/settings/capability-repair-context";
import { CreateToolCard } from "@/components/canvas/sections/create-tool-card";
import { useConnectorPresets } from "@/lib/use-connector-presets";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field } from "@/components/ui/field";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { formatDateYMD } from "@/lib/format";
import { DialogShell, Dialog } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import {
  AlertTriangle,
  Check,
  ChevronRight,
  Lock,
  RefreshCw,
  Search,
} from "@/components/icons";
import { useAppRouter } from "@/lib/use-app-router";
import { randomUuid } from "@/lib/random-id";
import { CharIcon, CharIconText } from "@/components/char-icon";

// Semantic colors (mockup: MCP blue/OpenAPI purple, low-saturation strokes). All user-facing technical identifiers mono.
// Kind badges (MCP --c-info, OpenAPI --c-accent) moved to styles/ui-controls-legacy.css .legacy-badge-tool-kind.

type SourceEditPatch = { name?: string; auth_type?: AuthType; auth?: Record<string, string> | null };

type PendingImpact = {
  source: TeamToolSourcePublic;
  // "remove_auth": an edit that switches credentials to none, which the server protects.
  action: "disable" | "delete" | "remove_auth";
  impact: ResourceChangeImpact;
  idempotencyKey: string;
  patch?: SourceEditPatch;
};

function newIdempotencyKey(): string {
  return randomUuid();
}

function formatScopeTimestamp(value: string | null | undefined, locale: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function ScopeChips({ values, empty }: { values: string[]; empty: string }) {
  if (values.length === 0) return <span className="legacy-tools-scope-empty">{empty}</span>;
  return (
    <div className="legacy-tools-scope-chips">
      {values.map((value, index) => (
        <span
          key={`${value}-${index}`}
          translate="no"
          className="legacy-badge-tool-scope"
        >
          {value}
        </span>
      ))}
    </div>
  );
}

function DatarcScopePanel({
  tool,
  refreshing,
  onRefresh,
}: {
  tool: TeamBuiltinTool;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const t = useTranslations("settings.tools");
  const locale = useLocale();
  const verified = tool.scope_state === "verified";
  const permissions = tool.scope.permissions?.length
    ? tool.scope.permissions
    : [t("datarc_scope_unrestricted")];
  const toolNames = tool.supported_tools.map((name) => t(`datarc_tool_${name}`));
  const workspaces = tool.scope.workspaces ?? [];
  const visibleWorkspaces = workspaces.slice(0, 50);
  const hiddenWorkspaceCount = workspaces.length - visibleWorkspaces.length;
  const expiry = formatScopeTimestamp(tool.scope.expired_at, locale);
  const checked = formatScopeTimestamp(tool.scope_checked_at, locale);

  return (
    <section
      aria-labelledby="datarc-scope-title" className="legacy-tools-scope"
    >
      <div className={`legacy-tools-scope-head${verified ? " is-verified" : ""}`}>
        <div className="legacy-tools-scope-headmain">
          <span aria-hidden="true" className={`legacy-tools-scope-mark${verified ? " is-verified" : ""}`}>
            {verified ? <Check size={14} /> : <Lock size={14} />}
          </span>
          <div className="legacy-tools-scope-headtext">
            <strong id="datarc-scope-title" className="legacy-tools-scope-title">{t("datarc_scope_title")}</strong>
            <span aria-live="polite" className={`legacy-tools-scope-status${verified ? " is-verified" : ""}`}>
              {verified ? t("datarc_scope_verified") : t(`datarc_scope_status_${tool.scope_state}`)}
            </span>
          </div>
        </div>
        <Button variant="secondary" onClick={onRefresh} disabled={refreshing}>
          <span className="legacy-tools-inline-icon"><RefreshCw size={12} aria-hidden="true" />{refreshing ? t("datarc_scope_refreshing") : t("datarc_scope_refresh")}</span>
        </Button>
      </div>

      {!verified ? (
        <p className="legacy-tools-scope-status-text">
          {t(`datarc_scope_${tool.scope_state}`)}
        </p>
      ) : (
        <div className="legacy-tools-scope-body">
          <div className="legacy-tools-scope-grid">
            <div>
              <div className="legacy-tools-scope-label">{t("datarc_scope_permissions")}</div>
              <ScopeChips values={permissions} empty={t("datarc_scope_no_tools")} />
            </div>
            <div>
              <div className="legacy-tools-scope-label">{t("datarc_scope_tools")}</div>
              <ScopeChips values={toolNames} empty={t("datarc_scope_no_tools")} />
            </div>
          </div>

          <details className="legacy-tools-scope-ws">
            <summary className="legacy-tools-scope-ws-summary">
              {t("datarc_scope_workspace_count", { count: workspaces.length })}
            </summary>
            <div className="legacy-tools-scope-ws-list">
              <ScopeChips values={visibleWorkspaces.map((workspace) => workspace.name)} empty={t("datarc_scope_no_workspaces")} />
              {hiddenWorkspaceCount > 0 ? <p className="legacy-tools-scope-ws-more">{t("datarc_scope_workspace_more", { count: hiddenWorkspaceCount })}</p> : null}
            </div>
          </details>

          <dl className="legacy-tools-scope-meta">
            {expiry ? <div><dt className="legacy-tools-scope-meta-term">{t("datarc_scope_expiry")}</dt><dd className="legacy-tools-scope-meta-value">{expiry}</dd></div> : null}
            {checked ? <div><dt className="legacy-tools-scope-meta-term">{t("datarc_scope_checked")}</dt><dd className="legacy-tools-scope-meta-value">{checked}</dd></div> : null}
          </dl>
        </div>
      )}
    </section>
  );
}

function KindBadge({ kind }: { kind: string }) {
  const t = useTranslations("settings.tools");
  const isMcp = kind === "mcp";
  return (
    <span className={`legacy-badge-tool-kind ${isMcp ? "is-mcp" : "is-openapi"}`}>
      {isMcp ? t("kind_mcp") : t("kind_openapi")}
    </span>
  );
}

function AuthBadge({ source }: { source: TeamToolSourcePublic }) {
  const t = useTranslations("settings.tools");
  if (source.auth_type === "none" || !source.has_auth) {
    return <span className="legacy-ai-tools-auth ai-tools-auth">{t("auth_none")}</span>;
  }
  const label = source.auth_type === "oauth" ? t("auth_oauth") : t("auth_configured");
  return (
    <span className="legacy-ai-tools-auth is-set ai-tools-auth">
      <Lock size={10} /> {label}
    </span>
  );
}

function StatusToggle({
  on,
  busy,
  disabled,
  label,
  buttonRef,
  onToggle,
}: {
  on: boolean;
  busy?: boolean;
  disabled?: boolean;
  label: string;
  buttonRef?: (node: HTMLButtonElement | null) => void;
  onToggle: () => void;
}) {
  return (
    <Switch
      ref={buttonRef}
      checked={on}
      aria-label={label}
      busy={busy}
      disabled={disabled}
      onCheckedChange={() => onToggle()}
      className={`legacy-switch-tools${on ? " is-on" : ""}${busy || disabled ? " is-inert" : ""}${disabled ? " is-disabled" : ""}`}
    >
      <span className={`legacy-switch-tools-knob${on ? " is-on" : ""}`} />
    </Switch>
  );
}

function SourceCard({
  source,
  expanded,
  matchingOperationIds,
  validateState,
  busy,
  toggleRef,
  onToggleExpand,
  onToggleStatus,
  onOpenUsage,
  onOpenOperationUsage,
  onValidate,
  onRefresh,
  onChooseTools,
  onAuthorize,
  onEdit,
  onDelete,
}: {
  source: TeamToolSourcePublic;
  expanded: boolean;
  matchingOperationIds: string[];
  validateState?: "pending" | ValidateResult;
  busy: boolean;
  toggleRef: (node: HTMLButtonElement | null) => void;
  onToggleExpand: () => void;
  onToggleStatus: () => void;
  onOpenUsage: () => void;
  onOpenOperationUsage: (operationId: string) => void;
  onValidate: () => void;
  /** Open the refresh preview; `returnFocusTo` is the control that gets focus back on close. */
  onRefresh: (returnFocusTo: HTMLElement | null) => void;
  /** Open Choose Tools; `returnFocusTo` is the control that gets focus back on close. */
  onChooseTools: (returnFocusTo: HTMLElement | null) => void;
  onAuthorize: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const t = useTranslations("settings.tools");
  const failed = source.status === "failed";
  const off = source.status === "disabled";
  // WP2b: an oauth source needs the browser authorize flow. tools_count===0 ⇒ never authorized
  // (a real OAuth server refuses an unauthenticated tools/list, so create left it empty) → prompt
  // authorization; once tools exist it's established → offer re-authorization.
  const isOauth = source.auth_type === "oauth";
  const needsAuth = isOauth && source.tools_count === 0;
  // Only MCP sources can re-list tools; an OAuth source must be authorized before it can list anything.
  const canRefresh = source.kind === "mcp" && !needsAuth;
  // Choosing needs a saved tool list to pick from.
  const canChooseTools = source.kind === "mcp" && source.tools_count > 0;
  const managementActionsRef = useRef<HTMLDivElement>(null);
  const operationsId = `tool-source-operations-${source.id}`;
  const hasOperationMatches = matchingOperationIds.length > 0;
  const visibleOperationIds = expanded ? source.selected_op_ids : matchingOperationIds;
  return (
    <div className={`legacy-ai-tools-card ai-tools-card${failed ? " is-failed" : ""}${off ? " is-off" : ""}`}>
      <div className="tool-source-card-top legacy-ai-tools-card-top">
        <div className="tool-source-card-main legacy-ai-tools-card-main">
          <div className="legacy-ai-tools-card-head">
            <span className="legacy-ai-tools-card-name ai-tools-card-name">{source.name}</span>
            <KindBadge kind={source.kind} />
            <AuthBadge source={source} />
            {needsAuth ? (
              <span className="legacy-ai-tools-pill is-accent ai-tools-pill">
                {t("oauth_needs_auth")}
              </span>
            ) : null}
            {failed ? (
              <span className="legacy-ai-tools-pill is-danger ai-tools-pill">
                {t("status_failed")}
              </span>
            ) : null}
          </div>
          <div className="tool-source-card-metadata legacy-ai-tools-card-meta ai-tools-card-meta">
            <span className="legacy-ai-tools-card-url ai-tools-card-url">
              {source.base_url}
            </span>
            <span>·</span>
            {/* WP2b-3c: show imported/total when more tools exist than were selected for import (e.g.
                an authorized OAuth/MCP source with 5 selected of 11 enumerated); otherwise show the
                imported count. The translation keys retain their legacy names to avoid churn. */}
            <span>
              {needsAuth
                ? t("exposed_pending")
                : source.tools_count > source.op_count
                  ? t("exposed_of", { n: source.op_count, total: source.tools_count })
                  : t("exposed", { n: source.op_count })}
            </span>
            <span>·</span>
            <span>{t("updated", { date: formatDateYMD(source.updated_at) })}</span>
          </div>
          <UsageSummaryInline summary={source.usage_summary} onOpen={onOpenUsage} />
          {validateState ? (
            <div className="legacy-ai-tools-validate">
              {validateState === "pending" ? (
                <span className="legacy-ai-tools-vstate">{t("validating")}</span>
              ) : validateState.reachable === false ? (
                <span className="legacy-ai-tools-vstate is-icon is-err">
                  <AlertTriangle size={13} /> {t("unreachable")}
                </span>
              ) : validateState.reachable === true && validateState.result?.error_kind === "upstream_auth" ? (
                // credential-readiness §19 follow-up: an auth-rejected credential DID reach
                // the upstream (reachable), but a green check would hide exactly what the
                // admin came here to find out — split it into an amber credential state.
                <span
                  data-testid="validate-auth-rejected"
                  className="legacy-ai-tools-vstate is-icon is-warn"
                >
                  <AlertTriangle size={13} /> {t("reachable_auth_rejected")}
                </span>
              ) : validateState.reachable === true ? (
                <span className="legacy-ai-tools-vstate is-icon is-ok">
                  <Check size={13} /> {t("reachable")}
                </span>
              ) : (
                // reachable === null: distinguish write-only (skipped) from inconclusive
                // (needs_params / tool_error / protocol — probe never touched the wire).
                <span className="legacy-ai-tools-vstate">
                  {validateState.result?.error_kind === "skipped_write" ? t("validate_skipped") : t("validate_inconclusive")}
                </span>
              )}
            </div>
          ) : null}
        </div>
        <div className="tool-source-card-actions legacy-ai-tools-card-actions">
          <div ref={managementActionsRef} className="tool-source-card-management-actions">
            {/* Initial authorization is card's next step, remains on card; other actions move into “⋯”. */}
            {isOauth && needsAuth ? (
              <Button className="tool-source-card-action" size="sm" variant="primary" onClick={onAuthorize} disabled={busy}>
                <Lock size={13} /> {t("action_authorize")}
              </Button>
            ) : null}
            <RowActionsMenu
              label={t("more_actions_for", { name: source.name })}
              pending={busy}
              actions={[
                ...(isOauth && !needsAuth ? [{ id: "reauthorize", label: t("action_reauthorize"), disabled: busy, onSelect: onAuthorize }] : []),
                { id: "validate", label: t("action_validate"), disabled: busy, onSelect: onValidate },
                ...(canRefresh
                  ? [{
                    id: "refresh",
                    label: t("action_refresh_tools"),
                    disabled: busy,
                    // The menu item unmounts with the menu; return focus to the "⋯" trigger.
                    onSelect: () => onRefresh(managementActionsRef.current?.querySelector<HTMLElement>(".ui-row-more") ?? null),
                  }]
                  : []),
                ...(canChooseTools
                  ? [{
                    id: "choose-tools",
                    label: t("action_choose_tools"),
                    disabled: busy,
                    onSelect: () => onChooseTools(managementActionsRef.current?.querySelector<HTMLElement>(".ui-row-more") ?? null),
                  }]
                  : []),
                { id: "edit", label: t("action_edit"), disabled: busy, onSelect: onEdit },
                { id: "delete", label: t("action_delete"), danger: true, disabled: busy, onSelect: onDelete },
              ]}
            />
          </div>
          {/* Availability is independent information column, never crowded with edit/delete/expand actions. */}
          <div
            className="tool-source-card-availability legacy-ai-tools-avail ai-tools-avail"
          >
            <span className="legacy-ai-tools-avail-label ai-tools-avail-label">
              {off || failed || needsAuth ? t("status_disabled") : t("status_enabled")}
            </span>
            {/* WP2b-3c: a never-authorized OAuth source has 0 tools → can't be enabled. */}
            <StatusToggle
              on={!off && !failed && !needsAuth}
              busy={busy}
              disabled={needsAuth}
              label={t("toggle_aria", { name: source.name })}
              buttonRef={toggleRef}
              onToggle={onToggleStatus}
            />
          </div>
          <button
            className={`tool-source-card-expand legacy-ai-icon-btn legacy-ai-tools-expand ai-tools-expand${expanded ? " is-expanded" : ""}`}
            type="button"
            aria-label={expanded ? t("collapse") : t("expand")}
            aria-expanded={expanded}
            aria-controls={operationsId}
            onClick={onToggleExpand}
          >
            <ChevronRight size={15} />
          </button>
        </div>
      </div>
      {expanded || hasOperationMatches ? (
        <div id={operationsId} className="legacy-ai-tools-ops ai-tools-ops">
          <div className="legacy-ai-tools-ops-label ai-tools-ops-label">
            {expanded
              ? t("expand_exposed", { n: source.op_count })
              : t("search_operation_matches", { n: matchingOperationIds.length })}
          </div>
          {visibleOperationIds.length ? (
            <div className="legacy-ai-chip-row">
              {visibleOperationIds.map((op) => (
                source.usage_summary !== undefined ? (
                  <button
                    type="button"
                    key={op}
                    onClick={() => onOpenOperationUsage(op)}
                    className={`legacy-ai-tools-op is-button ai-tools-op${matchingOperationIds.includes(op) ? " is-match" : ""}`}
                  >
                    {op}
                  </button>
                ) : (
                  <span
                    key={op}
                    className={`legacy-ai-tools-op ai-tools-op${matchingOperationIds.includes(op) ? " is-match" : ""}`}
                  >
                    {op}
                  </span>
                )
              ))}
            </div>
          ) : (
            <div className="legacy-ai-tools-ops-empty">{t("expand_empty")}</div>
          )}
        </div>
      ) : null}
    </div>
  );
}

// ── Edit dialog (WP3: rename/credentials; MCP tool reselection lives in Choose Tools) ──
function EditDialog({
  source,
  busy,
  onClose,
  onSave,
}: {
  source: TeamToolSourcePublic;
  busy: boolean;
  onClose: () => void;
  onSave: (patch: { name?: string; auth_type?: AuthType; auth?: Record<string, string> | null }) => void;
}) {
  const t = useTranslations("settings.tools");
  const [name, setName] = useState(source.name);
  const [rotate, setRotate] = useState(false);
  const [authType, setAuthType] = useState<AuthType>((source.auth_type as AuthType) || "none");
  const [secret, setSecret] = useState("");

  const submit = () => {
    const patch: { name?: string; auth_type?: AuthType; auth?: Record<string, string> | null } = {};
    if (name.trim() && name.trim() !== source.name) patch.name = name.trim();
    if (rotate) {
      patch.auth_type = authType;
      patch.auth = authType === "none" ? null : { value: secret };
    }
    onSave(patch);
  };

  return (
    <Dialog
      title={t("edit_title", { name: source.name })}
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            {t("cancel")}
          </Button>
          <Button variant="primary" onClick={submit} disabled={busy || (rotate && authType !== "none" && !secret)}>
            {t("save")}
          </Button>
        </>
      }
    >
      <Field id="edit-tool-name" label={t("edit_name_label")}>
        <input id="edit-tool-name" name="edit_tool_name" autoComplete="organization" value={name} onChange={(e) => setName(e.target.value)} className="ui-input legacy-input-tools" />
      </Field>

      {/* review P3: an oauth source's creds are managed by the Authorize button, NOT rotate — offering
          the rotate control would let Save send auth_type="oauth" (backend 422). Show a hint instead. */}
      {source.auth_type === "oauth" ? (
        <div className="legacy-tools-edit-box">
          <p className="legacy-tools-edit-oauth-note">
            <Lock size={13} /> {t("edit_oauth_note")}
          </p>
        </div>
      ) : (
        <div className="legacy-tools-edit-box">
          <label className="legacy-tools-edit-rotate">
            <input type="checkbox" checked={rotate} onChange={(e) => setRotate(e.target.checked)} />
            <span className="legacy-tools-inline-icon">
              <RefreshCw size={13} /> {t("edit_rotate_creds")}
            </span>
          </label>
          <p className="legacy-tools-edit-creds-note">{t("edit_creds_note")}</p>
          {rotate ? (
            <div className="legacy-tools-edit-rotate-fields">
              <Field id="edit-tool-auth" label={t("edit_auth_label")}>
                <select id="edit-tool-auth" name="edit_tool_auth" autoComplete="off" value={authType} onChange={(e) => setAuthType(e.target.value as AuthType)} className="ui-input legacy-input-tools">
                  <option value="none">{t("auth_opt_none")}</option>
                  <option value="bearer">Bearer</option>
                  <option value="api_key">API Key</option>
                </select>
              </Field>
              {authType !== "none" ? (
                <Field id="edit-tool-secret" label={t("edit_secret_label")}>
                  <input
                    id="edit-tool-secret"
                    name="edit_tool_secret"
                    type="password"
                    autoComplete="new-password"
                    value={secret}
                    onChange={(e) => setSecret(e.target.value)}
                    placeholder={t("secret_ph")}
                    className="ui-input legacy-input-tools legacy-tools-input-mono"
                  />
                </Field>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
    </Dialog>
  );
}

/** System built-in capabilities: read-only display.
 *
 * Display does not mean currently usable by this team/role; permission remains in respective configuration (subagent tools,
 * team tool-source switches). Names/descriptions come from generated table; **never author wording here**.
 */
function BuiltinCapabilities({ query }: { query: string }) {
  const t = useTranslations("settings.tools");
  // Google write capabilities only appear once the team's Google connection is enabled.
  const [googleWorkspace, setGoogleWorkspace] = useState(false);
  useEffect(() => {
    let active = true;
    void fetchMe()
      .then((me) => {
        if (active) setGoogleWorkspace(Boolean(me.capabilities?.google_workspace));
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);
  const all = useMemo(() => builtinTools({ googleWorkspace }), [googleWorkspace]);
  const q = query.trim().toLowerCase();
  const shown = q
    ? all.filter((c) => `${c.name}${c.blurb}${c.ref}`.toLowerCase().includes(q))
    : all;

  const groups = useMemo(() => {
    const by = new Map<string, BuiltinTool[]>();
    for (const c of shown) {
      const list = by.get(c.category) ?? [];
      list.push(c);
      by.set(c.category, list);
    }
    return [...by.entries()];
  }, [shown]);

  return (
    <section className="legacy-ai-tools-caps ai-tools-caps" data-testid="builtin-capabilities">
      <h2 className="legacy-ai-tools-caps-title ui-section-title">
        {t("builtin_title")}
      </h2>
      <p className="legacy-ai-tools-caps-desc ui-section-desc">
        {/* Filtering must show matched N / total M (116 dogfood 2026-09-09):
            search “知识库” left 4 cards, subtitle still “自带的 33 项能力”, appearing miscounted. */}
        {q
          ? t("builtin_filtered", { shown: shown.length, total: all.length })
          : t("builtin_subtitle", { count: all.length })}
      </p>
      {groups.length === 0 ? (
        <div className="legacy-ai-tools-nomatch ai-tools-nomatch">{t("builtin_no_match")}</div>
      ) : (
        <div className="legacy-ai-tools-cap-groups">
          {groups.map(([category, items]) => (
            <div key={category}>
              <div className="legacy-ai-tools-cap-cat ai-tools-cap-cat">
                {t(`cap_cat_${category}` as never)}
              </div>
              <div className="legacy-ai-tools-cap-grid ai-tools-cap-grid">
                {items.map((c) => (
                  <div
                    key={c.ref}
                    className="legacy-ai-tools-cap ai-tools-cap"
                  >
                    <span aria-hidden className="legacy-ai-tools-cap-sigil"><CharIcon ch={c.sigil} aria-hidden /></span>
                    <div className="legacy-ai-min0">
                      <div className="legacy-ai-tools-cap-name ai-tools-cap-name">{c.name}</div>
                      <div className="legacy-ai-tools-cap-blurb ai-tools-cap-blurb">{c.blurb}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// Built-in third-party save failure: backend HTTPException(detail="<code>") returns machine code;
// map to readable text, unknown code uses generic fallback; never display raw machine codes.
const BUILTIN_SAVE_ERROR_KEYS = {
  tavily_api_key_invalid: "builtin_save_error_tavily_api_key_invalid",
  tavily_verification_unreachable: "builtin_save_error_tavily_verification_unreachable",
  tavily_verification_failed: "builtin_save_error_tavily_verification_failed",
  datarc_endpoint_rejected: "builtin_save_error_datarc_endpoint_rejected",
  datarc_endpoint_must_be_service_root: "builtin_save_error_datarc_endpoint_must_be_service_root",
  datarc_name_required: "builtin_save_error_datarc_name_required",
  datarc_name_conflict: "builtin_save_error_datarc_name_conflict",
  datarc_connection_key_required: "builtin_save_error_datarc_connection_key_required",
  datarc_connection_key_invalid: "builtin_save_error_datarc_connection_key_invalid",
  datarc_verification_unreachable: "builtin_save_error_datarc_verification_unreachable",
  datarc_verification_failed: "builtin_save_error_datarc_verification_failed",
  datarc_endpoint_unsupported: "builtin_save_error_datarc_endpoint_unsupported",
  datarc_tool_not_found: "builtin_save_error_datarc_tool_not_found",
  builtin_tool_encryption_required: "builtin_save_error_builtin_tool_encryption_required",
} as const;

type BuiltinSaveErrorKey = (typeof BUILTIN_SAVE_ERROR_KEYS)[keyof typeof BUILTIN_SAVE_ERROR_KEYS] | "builtin_save_error_generic";

function builtinSaveErrorKey(error: unknown): BuiltinSaveErrorKey {
  const candidates: unknown[] = [];
  if (error && typeof error === "object") {
    candidates.push((error as { code?: unknown }).code, (error as { message?: unknown }).message);
  }
  for (const candidate of candidates) {
    if (typeof candidate === "string" && Object.hasOwn(BUILTIN_SAVE_ERROR_KEYS, candidate)) {
      return BUILTIN_SAVE_ERROR_KEYS[candidate as keyof typeof BUILTIN_SAVE_ERROR_KEYS];
    }
  }
  return "builtin_save_error_generic";
}

export function ToolsManager() {
  const t = useTranslations("settings.tools");
  // Chinese UI alone uses Chinese; others (English/German/French) use English.
  const english = !useLocale().startsWith("zh");
  const router = useAppRouter();
  const [data, setData] = useState<TeamToolSourcePublic[] | null>(null);
  const [builtinTools, setBuiltinTools] = useState<TeamBuiltinTool[]>([]);
  const [builtinPickerOpen, setBuiltinPickerOpen] = useState(false);
  const shopifyText = useTranslations("shopify_tool");
  const [shopifyEnabled, setShopifyEnabled] = useState(false);
  const [shopifyCreateRequest, setShopifyCreateRequest] = useState(0);
  useEffect(() => {
    let active = true;
    void fetchMe().then((me) => { if (active) setShopifyEnabled(Boolean(me.capabilities?.shopify_plugin)); }).catch(() => undefined);
    return () => { active = false; };
  }, []);
  const [powerbiCreateRequest, setPowerbiCreateRequest] = useState(0);
  const [googleCreateRequest, setGoogleCreateRequest] = useState(0);
  // Google Workspace is a single team connection; offer it in the picker only while available and not yet configured.
  const [googlePickable, setGooglePickable] = useState(false);
  const onGoogleAvailability = useCallback((available: boolean, configured: boolean) => setGooglePickable(available && !configured), []);
  const [builtinPickerQuery, setBuiltinPickerQuery] = useState("");
  const [tavilyConfigOpen, setTavilyConfigOpen] = useState(false);
  const [tavilyKey, setTavilyKey] = useState("");
  const [datarcConfigOpen, setDatarcConfigOpen] = useState(false);
  const [datarcEditTarget, setDatarcEditTarget] = useState<TeamBuiltinTool | null>(null);
  const [datarcDeleteTarget, setDatarcDeleteTarget] = useState<TeamBuiltinTool | null>(null);
  const [datarcName, setDatarcName] = useState("");
  const [datarcEndpoint, setDatarcEndpoint] = useState("");
  const [datarcKey, setDatarcKey] = useState("");
  // Save errors inside dialog (local display); page error banner reserved for actual load failures.
  const [builtinSaveError, setBuiltinSaveError] = useState<BuiltinSaveErrorKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const closeCreate = useCallback(() => setCreateOpen(false), []);
  // external-capability-onboarding §3.1 — curated preset catalog, fetched when the card opens.
  const connectorPresets = useConnectorPresets(createOpen);
  const [editTarget, setEditTarget] = useState<TeamToolSourcePublic | null>(null);
  const [pendingImpact, setPendingImpact] = useState<PendingImpact | null>(null);
  const [legacyDeleteTarget, setLegacyDeleteTarget] = useState<TeamToolSourcePublic | null>(null);
  const [validateStates, setValidateStates] = useState<Record<string, "pending" | ValidateResult>>({});
  const [rowBusy, setRowBusy] = useState<string | null>(null);
  const [refreshTarget, setRefreshTarget] = useState<TeamToolSourcePublic | null>(null);
  const refreshReturnFocusRef = useRef<HTMLElement | null>(null);
  const [refreshNotice, setRefreshNotice] = useState<{
    sourceId: string;
    name: string;
    result: ToolSourceRefreshResult;
    /** The source imports a chosen subset, so newly added tools are not imported yet. */
    offerChooseTools: boolean;
  } | null>(null);
  const [chooseTarget, setChooseTarget] = useState<{ source: TeamToolSourcePublic; highlight: string[] } | null>(null);
  const chooseReturnFocusRef = useRef<HTMLElement | null>(null);
  const [chooseNotice, setChooseNotice] = useState<{ name: string; result: ToolSelectionSaved } | null>(null);
  const [oauthNotice, setOauthNotice] = useState<"success" | "error" | null>(null);
  const [usageTarget, setUsageTarget] = useState<{
    sourceId: string;
    operationId?: string;
    title: string;
  } | null>(null);
  const sourceToggleRefs = useRef(new Map<string, HTMLButtonElement>());
  const bindSourceToggle = useCallback((id: string, node: HTMLButtonElement | null) => {
    if (node) sourceToggleRefs.current.set(id, node);
    else sourceToggleRefs.current.delete(id);
  }, []);

  // WP2b: the OAuth callback redirects back here with ?oauth=success|error. Read it client-side
  // (avoids a useSearchParams Suspense boundary), show a banner, and strip the query so a refresh
  // doesn't re-show it. On success the source's tools were just re-enumerated → reload the list.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const o = new URLSearchParams(window.location.search).get("oauth");
    if (o !== "success" && o !== "error") return;
    setOauthNotice(o);
    const url = new URL(window.location.href);
    url.searchParams.delete("oauth");
    url.searchParams.delete("source");
    window.history.replaceState({}, "", url.toString());
  }, []);

  const refresh = useCallback(async () => {
    try {
      setBusy(true);
      const [sources, builtin] = await Promise.all([listSources(), listBuiltinTools()]);
      setData(sources);
      setBuiltinTools(builtin);
      setError(null);
    } catch (e) {
      setError(t("load_failed_safe"));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openImpactConfirmation = async (
    s: TeamToolSourcePublic,
    action: PendingImpact["action"],
    patch?: SourceEditPatch,
  ): Promise<boolean> => {
    setRowBusy(s.id);
    try {
      const impact = await previewSourceImpact(s.id, {
        action: action === "delete" ? "delete" : "patch",
        proposed_change: action === "disable" ? { status: "disabled" } : action === "remove_auth" ? { ...patch } : {},
      });
      if (impact.blocking || !impact.impact_token) {
        setError(t(
          action === "delete"
            && impact.reason_codes.includes("resource_delete_has_live_dependencies")
            ? "impact_delete_blocked"
            : "impact_unavailable",
        ));
        return false;
      }
      setPendingImpact({
        source: s,
        action,
        impact,
        idempotencyKey: newIdempotencyKey(),
        patch,
      });
      setError(null);
      return true;
    } catch (e) {
      setError(t("impact_failed_safe"));
      return false;
    } finally {
      setRowBusy(null);
    }
  };

  const onToggleStatus = async (s: TeamToolSourcePublic) => {
    if (s.status === "enabled" && s.impact_guard_enabled) {
      await openImpactConfirmation(s, "disable");
      return;
    }
    const nextStatus = s.status === "enabled" ? "disabled" : "enabled";
    const restoreFocus = document.activeElement === sourceToggleRefs.current.get(s.id);
    setRowBusy(s.id);
    try {
      await patchSource(s.id, { status: nextStatus });
      await refresh();
    } catch (e) {
      setError(t("change_failed_safe"));
    } finally {
      setRowBusy(null);
      if (restoreFocus) {
        window.requestAnimationFrame(() => sourceToggleRefs.current.get(s.id)?.focus());
      }
    }
  };

  const onValidate = async (s: TeamToolSourcePublic) => {
    setValidateStates((v) => ({ ...v, [s.id]: "pending" }));
    try {
      const res = await validateSource(s.id);
      setValidateStates((v) => ({ ...v, [s.id]: res }));
    } catch {
      setValidateStates((v) => ({ ...v, [s.id]: { operation_id: "", reachable: false, result: null } }));
    }
  };

  const onAuthorize = async (s: TeamToolSourcePublic) => {
    // WP2b: begin the OAuth flow, then hand the browser to the provider. It returns to
    // /oauth/callback (server-side) which redirects back here with ?oauth=success|error.
    setRowBusy(s.id);
    try {
      const { authorization_url } = await authorizeSource(s.id);
      window.location.href = authorization_url; // full-page navigation → we don't return here
    } catch (e) {
      setError(t("connect_failed_safe"));
      setRowBusy(null);
    }
  };

  const onRefreshApplied = async (source: TeamToolSourcePublic, result: ToolSourceRefreshResult) => {
    setRefreshTarget(null);
    setRefreshNotice({
      sourceId: source.id,
      name: source.name,
      result,
      offerChooseTools: result.added.length > 0 && source.selected_op_ids.length > 0,
    });
    await refresh();
  };

  const openChooseTools = (
    source: TeamToolSourcePublic,
    returnFocusTo: HTMLElement | null,
    highlight: string[] = [],
  ) => {
    // The refresh notice stays until a save so its Choose Tools button can take focus back on Cancel.
    chooseReturnFocusRef.current = returnFocusTo;
    setChooseNotice(null);
    setChooseTarget({ source, highlight });
  };

  const onChooseToolsSaved = async (source: TeamToolSourcePublic, result: ToolSelectionSaved) => {
    setChooseTarget(null);
    setRefreshNotice(null);
    setChooseNotice({ name: source.name, result });
    await refresh();
  };

  const onSaveEdit = async (patch: SourceEditPatch) => {
    if (!editTarget) return;
    // The server protects removing credentials (operations start being called without auth):
    // with the Impact Guard on, it needs the same impact preview and confirmation as disabling.
    if (editTarget.impact_guard_enabled && patch.auth_type === "none" && editTarget.auth_type !== "none") {
      if (await openImpactConfirmation(editTarget, "remove_auth", patch)) setEditTarget(null);
      return;
    }
    setBusy(true);
    try {
      await patchSource(editTarget.id, patch);
      setEditTarget(null);
      await refresh();
    } catch (e) {
      setError(t("change_failed_safe"));
    } finally {
      setBusy(false);
    }
  };

  const onConfirmImpact = async () => {
    const impactToken = pendingImpact?.impact.impact_token;
    if (!pendingImpact || !impactToken) return;
    const { source, action, idempotencyKey, patch } = pendingImpact;
    const guard = { impactToken, idempotencyKey };
    setBusy(true);
    try {
      if (action === "delete") {
        await deleteSource(source.id, guard);
      } else if (action === "remove_auth") {
        await patchSource(source.id, patch ?? { auth_type: "none", auth: null }, guard);
      } else {
        await patchSource(source.id, { status: "disabled" }, guard);
      }
      setPendingImpact(null);
      await refresh();
    } catch (e) {
      if (e instanceof TeamToolApiError && e.status === 409) {
        setPendingImpact(null);
        const refreshed = await openImpactConfirmation(source, action, patch);
        if (refreshed) setError(t("impact_changed"));
      } else {
        setError(t("change_failed_safe"));
      }
    } finally {
      setBusy(false);
    }
  };

  const onConfirmLegacyDelete = async () => {
    if (!legacyDeleteTarget) return;
    setBusy(true);
    try {
      await deleteSource(legacyDeleteTarget.id);
      setLegacyDeleteTarget(null);
      await refresh();
    } catch (e) {
      setError(t("delete_failed_safe"));
    } finally {
      setBusy(false);
    }
  };

  const normalizedQuery = query.trim().toLowerCase();
  const operationMatches = useMemo(() => {
    const matches = new Map<string, string[]>();
    if (!normalizedQuery) return matches;
    for (const source of data ?? []) {
      const operations = source.selected_op_ids.filter((operation) =>
        operation.toLowerCase().includes(normalizedQuery),
      );
      if (operations.length) matches.set(source.id, operations);
    }
    return matches;
  }, [data, normalizedQuery]);

  const filtered = useMemo(() => {
    if (!data) return null;
    if (!normalizedQuery) return data;
    return data.filter(
      (s) => s.name.toLowerCase().includes(normalizedQuery) || s.base_url.toLowerCase().includes(normalizedQuery) || operationMatches.has(s.id),
    );
  }, [data, normalizedQuery, operationMatches]);

  const importedOperations = useMemo(() => (data ?? []).reduce((total, source) => total + source.op_count, 0), [data]);
  const tavily = builtinTools.find((tool) => tool.provider === "tavily");
  const datarcTools = builtinTools.filter((tool) => tool.provider === "datarc");
  const shopifyTools = builtinTools.filter((tool) => tool.provider === "shopify");
  const shopifyMatchesPicker = shopifyEnabled && (!builtinPickerQuery.trim() || `shopify ${shopifyText("picker_desc")}`.toLowerCase().includes(builtinPickerQuery.trim().toLowerCase()));
  const powerbiTools = builtinTools.filter((tool) => tool.provider === "powerbi");
  const tavilyMatchesPicker = !builtinPickerQuery.trim() || `tavily ${t("tavily_desc")}`.toLowerCase().includes(builtinPickerQuery.trim().toLowerCase());
  const datarcMatchesPicker = !builtinPickerQuery.trim() || `北极九章 datarc ${t("datarc_desc")}`.toLowerCase().includes(builtinPickerQuery.trim().toLowerCase());
  const powerbiMatchesPicker = !builtinPickerQuery.trim() || "power bi powerbi 语义模型 semantic model".includes(builtinPickerQuery.trim().toLowerCase());
  const googleMatchesPicker = googlePickable && (!builtinPickerQuery.trim() || `google workspace gmail drive docs sheets slides calendar ${t("google_desc")}`.toLowerCase().includes(builtinPickerQuery.trim().toLowerCase()));
  const saveTavily = async () => {
    if (!tavilyKey.trim()) return;
    setBusy(true);
    setBuiltinSaveError(null);
    try {
      await configureTavily(tavilyKey.trim(), true);
    } catch (e) {
      setBuiltinSaveError(builtinSaveErrorKey(e));
      setBusy(false);
      return;
    }
    setTavilyKey("");
    setTavilyConfigOpen(false);
    try { await refresh(); } finally { setBusy(false); }
  };
  const toggleTavily = async () => {
    if (!tavily) return;
    setRowBusy("tavily");
    try { await setTavilyEnabled(tavily.status !== "enabled"); await refresh(); }
    catch (e) { setError(t("change_failed_safe")); } finally { setRowBusy(null); }
  };
  const refreshScope = async (tool: TeamBuiltinTool) => {
    setRowBusy(tool.id);
    try {
      const refreshed = await refreshDatarcScope(tool.id);
      setBuiltinTools((items) => items.map((item) => item.id === refreshed.id ? refreshed : item));
      setDatarcEditTarget((current) => current?.id === refreshed.id ? refreshed : current);
      setError(null);
    } catch (e) {
      setError(t("scope_failed_safe"));
    } finally {
      setRowBusy(null);
    }
  };
  const openDatarcConfig = (tool?: TeamBuiltinTool) => {
    setDatarcEditTarget(tool ?? null);
    setDatarcName(tool?.name ?? "");
    setDatarcEndpoint(tool?.endpoint ?? "");
    setDatarcKey("");
    setBuiltinSaveError(null);
    setDatarcConfigOpen(true);
    if (tool) void refreshScope(tool);
  };
  const closeDatarcConfig = () => {
    setDatarcConfigOpen(false);
    setDatarcEditTarget(null);
    setDatarcName("");
    setDatarcEndpoint("");
    setDatarcKey("");
    setBuiltinSaveError(null);
  };
  const saveDatarc = async () => {
    if (!datarcName.trim() || !datarcEndpoint.trim() || (!datarcEditTarget && !datarcKey.trim())) return;
    setBusy(true);
    setBuiltinSaveError(null);
    try {
      if (datarcEditTarget) {
        await updateDatarcTool(datarcEditTarget.id, {
          name: datarcName.trim(),
          endpoint: datarcEndpoint.trim(),
          ...(datarcKey.trim() ? { connection_key: datarcKey.trim() } : {}),
        });
      } else {
        await createDatarcTool({
          name: datarcName.trim(),
          endpoint: datarcEndpoint.trim(),
          connection_key: datarcKey.trim(),
          enabled: true,
        });
      }
    } catch (e) {
      setBuiltinSaveError(builtinSaveErrorKey(e));
      setBusy(false);
      return;
    }
    closeDatarcConfig();
    try { await refresh(); } finally { setBusy(false); }
  };
  const toggleDatarc = async (tool: TeamBuiltinTool) => {
    setRowBusy(tool.id);
    try { await setDatarcEnabled(tool.id, tool.status !== "enabled"); await refresh(); }
    catch (e) { setError(t("change_failed_safe")); } finally { setRowBusy(null); }
  };
  const confirmDeleteDatarc = async () => {
    if (!datarcDeleteTarget) return;
    setBusy(true);
    try { await deleteDatarcTool(datarcDeleteTarget.id); setDatarcDeleteTarget(null); await refresh(); }
    catch (e) { setError(t("delete_failed_safe")); } finally { setBusy(false); }
  };
  const loadUsage = useCallback(() => {
    if (!usageTarget) return Promise.reject(new Error("resource_usage_target_missing"));
    return getSourceUsage(usageTarget.sourceId, usageTarget.operationId);
  }, [usageTarget]);

  return (
    <div data-testid="tools-manager" className="legacy-ai-tools-root ai-tools">
      <header className="ui-page-head legacy-tools-head ai-page-head">
        <div className="legacy-ai-flex1 ai-page-head-text">
          <h1 className="ui-page-title legacy-tools-title">{t("title")}</h1>
          <p className="ui-page-desc legacy-tools-desc">
            {data ? t("subtitle_count", { sources: data.length, tools: importedOperations }) : t("subtitle")}
          </p>
        </div>
        <Button variant="primary" onClick={() => setCreateOpen(true)}>
          <CharIconText ch="＋">{t("new_source")}</CharIconText>
        </Button>
        <Button variant="secondary" onClick={() => setBuiltinPickerOpen(true)}>
          <CharIconText ch="＋">{t("new_builtin")}</CharIconText>
        </Button>
      </header>

      <CapabilityRepairContext kind="tool" onDependency={setQuery} />

      {tavily ? <div className="legacy-ai-tools-builtin ai-tools-builtin">
        <TavilyMark />
        <div className="legacy-ai-flex1"><strong>Tavily</strong><div className="legacy-ai-tools-builtin-desc ai-tools-builtin-desc">{t("tavily_desc")}</div></div>
        <RowActionsMenu
          label={t("more_actions_for", { name: "Tavily" })}
          actions={[{ id: "edit", label: t("action_edit"), onSelect: () => setTavilyConfigOpen(true) }]}
        />
        <StatusToggle on={tavily.status === "enabled"} busy={rowBusy === "tavily"} label={t("toggle_tavily")} onToggle={() => void toggleTavily()} />
      </div> : null}

      {datarcTools.map((tool) => <div key={tool.id} className={`legacy-ai-tools-builtin ai-tools-builtin${tool.status === "enabled" ? "" : " is-off"}`}>
        <DatarcMark />
        <div className="legacy-ai-flex1 legacy-ai-min0">
          <strong>{tool.display_name}</strong>
          <div className="legacy-ai-tools-builtin-desc is-clip ai-tools-builtin-desc">{tool.endpoint}{tool.key_hint ? ` · ${t("datarc_key_hint", { hint: tool.key_hint })}` : ""}</div>
        </div>
        <RowActionsMenu
          label={t("more_actions_for", { name: tool.display_name })}
          actions={[
            { id: "edit", label: t("action_edit"), onSelect: () => openDatarcConfig(tool) },
            { id: "delete", label: t("action_delete"), danger: true, onSelect: () => setDatarcDeleteTarget(tool) },
          ]}
        />
        <StatusToggle on={tool.status === "enabled"} busy={rowBusy === tool.id} label={t("toggle_datarc", { name: tool.display_name })} onToggle={() => void toggleDatarc(tool)} />
      </div>)}

      {shopifyEnabled ? <ShopifyToolSection tools={shopifyTools} createRequest={shopifyCreateRequest} onChanged={refresh} /> : null}
      <PowerBiToolSection tools={powerbiTools} createRequest={powerbiCreateRequest} onChanged={refresh} />
      <GoogleWorkspaceToolSection createRequest={googleCreateRequest} onAvailableChange={onGoogleAvailability} />

      {data && data.length > 0 ? (
        <div className="legacy-ai-tools-search ai-tools-search">
          <span className="legacy-ai-tools-search-icon ai-tools-search-icon">
            <Search size={15} />
          </span>
          <input
            type="search"
            name="tool_source_search"
            autoComplete="off"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("search_ph")}
            aria-label={t("search_ph")}
            className="ui-input legacy-input-tools legacy-ai-tools-search-input ai-tools-search-input"
          />
        </div>
      ) : null}

      {oauthNotice ? (
        <div className="legacy-ai-mb12">
          <Notice tone={oauthNotice}>{oauthNotice === "success" ? t("oauth_success") : t("oauth_error")}</Notice>
        </div>
      ) : null}

      {refreshNotice ? (
        <div className="legacy-ai-mb12">
          <Notice tone="success">
            {t("refresh_success", {
              name: refreshNotice.name,
              added: refreshNotice.result.added.length,
              removed: refreshNotice.result.removed.length,
            })}
            {(refreshNotice.result.changed?.length ?? 0) > 0
              ? ` ${t("refresh_success_changed", { n: refreshNotice.result.changed?.length ?? 0 })}`
              : ""}
            {refreshNotice.result.paused_schedule_ids.length > 0
              ? ` ${t("refresh_success_paused", { n: refreshNotice.result.paused_schedule_ids.length })}`
              : ""}
            {refreshNotice.offerChooseTools ? (
              <span className="legacy-tools-notice-action">
                {t("refresh_success_choose_hint")}{" "}
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={(event) => {
                    const current = data?.find((item) => item.id === refreshNotice.sourceId);
                    if (current) openChooseTools(current, event.currentTarget, refreshNotice.result.added);
                  }}
                >
                  {t("action_choose_tools")}
                </Button>
              </span>
            ) : null}
          </Notice>
        </div>
      ) : null}

      {chooseNotice ? (
        <div className="legacy-ai-mb12">
          <Notice tone="success">
            {t("choose_tools_success", { name: chooseNotice.name, n: chooseNotice.result.imported })}
            {chooseNotice.result.paused_schedule_ids.length > 0
              ? ` ${t("refresh_success_paused", { n: chooseNotice.result.paused_schedule_ids.length })}`
              : ""}
          </Notice>
        </div>
      ) : null}

      {error ? <Notice tone="error">{error}</Notice> : null}

      {busy && !data ? (
        <LoadingSkeleton label={t("loading")} />
      ) : filtered && filtered.length === 0 && (data?.length ?? 0) === 0 && builtinTools.length === 0 ? (
        <EmptyState onNew={() => setCreateOpen(true)} />
      ) : filtered ? (
        <div className="legacy-ai-ds-list ai-card-list">
          {filtered.map((s) => (
            <SourceCard
              key={s.id}
              source={s}
              expanded={expanded === s.id}
              matchingOperationIds={operationMatches.get(s.id) ?? []}
              validateState={validateStates[s.id]}
              busy={rowBusy === s.id}
              toggleRef={(node) => bindSourceToggle(s.id, node)}
              onToggleExpand={() => setExpanded((e) => (e === s.id ? null : s.id))}
              onToggleStatus={() => void onToggleStatus(s)}
              onOpenUsage={() =>
                setUsageTarget({ sourceId: s.id, title: s.name })
              }
              onOpenOperationUsage={(operationId) =>
                setUsageTarget({
                  sourceId: s.id,
                  operationId,
                  title: `${s.name} / ${operationId}`,
                })
              }
              onValidate={() => void onValidate(s)}
              onRefresh={(returnFocusTo) => {
                refreshReturnFocusRef.current = returnFocusTo;
                setRefreshNotice(null);
                setChooseNotice(null);
                setRefreshTarget(s);
              }}
              onChooseTools={(returnFocusTo) => openChooseTools(s, returnFocusTo)}
              onAuthorize={() => void onAuthorize(s)}
              onEdit={() => setEditTarget(s)}
              onDelete={() => {
                if (s.impact_guard_enabled) void openImpactConfirmation(s, "delete");
                else setLegacyDeleteTarget(s);
              }}
            />
          ))}
          {filtered.length === 0 ? <div className="legacy-ai-tools-nomatch ai-tools-nomatch">{t("no_match")}</div> : null}
        </div>
      ) : null}

      {/* Search filters built-ins too; customers need not know built-in versus external-source origin to find capability. */}
      <BuiltinCapabilities query={query} />

      {createOpen ? (
        <DialogShell
          onClose={closeCreate}
          label={t("new_source_dialog_label")}
          mount="inline"
          backdropClassName="legacy-tools-create-backdrop"
          panelClassName="legacy-tools-create-panel"
        >
          <CreateToolCard
            presets={connectorPresets}
            manageStepFocus
            onClose={closeCreate}
            onCreated={() => {
              closeCreate();
              void refresh();
            }}
          />
        </DialogShell>
      ) : null}

      {builtinPickerOpen ? <Dialog title={t("builtin_picker_title")} onClose={() => { setBuiltinPickerOpen(false); setBuiltinPickerQuery(""); }}>
        <p className="legacy-tools-dialog-lead">{t("builtin_picker_subtitle")}</p>
        <input type="search" value={builtinPickerQuery} onChange={(event) => setBuiltinPickerQuery(event.target.value)} placeholder={t("builtin_picker_search")} aria-label={t("builtin_picker_search")} className="ui-input legacy-input-tools legacy-tools-picker-search" autoComplete="off" />
        <div className="legacy-tools-picker-list">
        {tavilyMatchesPicker ? <button type="button" className="legacy-ai-tools-picker-option" onClick={() => { setBuiltinPickerOpen(false); setBuiltinPickerQuery(""); setBuiltinSaveError(null); setTavilyConfigOpen(true); }}>
          <span className="legacy-ai-tools-picker-icon"><TavilyMark /></span><span><strong>Tavily</strong><span className="legacy-ai-tools-picker-desc">{t("tavily_desc")}</span></span>
        </button> : null}
        {datarcMatchesPicker ? <button type="button" className="legacy-ai-tools-picker-option" onClick={() => { setBuiltinPickerOpen(false); setBuiltinPickerQuery(""); openDatarcConfig(); }}>
          <span className="legacy-ai-tools-picker-icon"><DatarcMark /></span><span><strong>{t("datarc_name")}</strong><span className="legacy-ai-tools-picker-desc">{t("datarc_desc")}</span></span>
        </button> : null}
        {powerbiMatchesPicker ? <button type="button" className="legacy-ai-tools-picker-option" onClick={() => { setBuiltinPickerOpen(false); setBuiltinPickerQuery(""); setPowerbiCreateRequest((value) => value + 1); }}>
          <span className="legacy-ai-tools-picker-icon"><PowerBiMark /></span><span><strong>Power BI</strong><span className="legacy-ai-tools-picker-desc">{english ? "Connect a semantic model for selected Agents." : "连接语义模型，按需分配给 Agent。"}</span></span>
        </button> : null}
        {shopifyMatchesPicker ? <button type="button" className="legacy-ai-tools-picker-option" onClick={() => { setBuiltinPickerOpen(false); setBuiltinPickerQuery(""); setShopifyCreateRequest((value) => value + 1); }}>
          <span><strong>Shopify</strong><span className="legacy-ai-tools-picker-desc">{shopifyText("picker_desc")}</span></span>
        </button> : null}
        {googleMatchesPicker ? <button type="button" className="legacy-ai-tools-picker-option" onClick={() => { setBuiltinPickerOpen(false); setBuiltinPickerQuery(""); setGoogleCreateRequest((value) => value + 1); }}>
          <span className="legacy-ai-tools-picker-icon"><GoogleMark /></span><span><strong>Google Workspace</strong><span className="legacy-ai-tools-picker-desc">{t("google_desc")}</span></span>
        </button> : null}
        {!tavilyMatchesPicker && !datarcMatchesPicker && !powerbiMatchesPicker && !googleMatchesPicker && !shopifyMatchesPicker ? <div className="legacy-tools-picker-empty">{t("builtin_picker_empty")}</div> : null}
        </div>
      </Dialog> : null}

      {tavilyConfigOpen ? <Dialog title={t("tavily_title")} onClose={() => { setTavilyConfigOpen(false); setBuiltinSaveError(null); }}>
        <p className="legacy-tools-dialog-lead">{t("tavily_desc")}</p>
        <Field id="tavily-api-key" label={t("tavily_key_label")}><input id="tavily-api-key" type="password" value={tavilyKey} onChange={(event) => setTavilyKey(event.target.value)} placeholder={t("tavily_key_placeholder")} className="ui-input legacy-input-tools" autoComplete="off" /></Field>
        <p className="legacy-tools-dialog-note">{t("tavily_key_note")}</p>
        {builtinSaveError ? <Notice tone="error" className="legacy-tools-dialog-error">{t(builtinSaveError)}</Notice> : null}
        <div className="legacy-tools-dialog-actions"><Button variant="secondary" onClick={() => { setTavilyConfigOpen(false); setBuiltinSaveError(null); }}>{t("cancel")}</Button><Button variant="primary" onClick={() => void saveTavily()} disabled={busy || !tavilyKey.trim()}>{tavily ? t("save") : t("verify_save")}</Button></div>
      </Dialog> : null}

      {datarcConfigOpen ? <Dialog title={datarcEditTarget ? t("datarc_edit_title") : t("datarc_title")} onClose={closeDatarcConfig}>
        <p className="legacy-tools-dialog-lead">{t("datarc_desc")}</p>
        <Field id="datarc-name" label={t("datarc_config_name_label")}><input id="datarc-name" value={datarcName} onChange={(event) => setDatarcName(event.target.value)} placeholder={t("datarc_config_name_placeholder")} className="ui-input legacy-input-tools" autoComplete="off" maxLength={60} /></Field>
        <Field id="datarc-endpoint" label={t("datarc_endpoint_label")}><input id="datarc-endpoint" value={datarcEndpoint} onChange={(event) => setDatarcEndpoint(event.target.value)} placeholder={t("datarc_endpoint_placeholder")} className="ui-input legacy-input-tools" autoComplete="url" /></Field>
        <Field id="datarc-key" label={t("datarc_key_label")}><input id="datarc-key" type="password" value={datarcKey} onChange={(event) => setDatarcKey(event.target.value)} placeholder={datarcEditTarget ? t("datarc_key_keep_placeholder") : t("datarc_key_placeholder")} className="ui-input legacy-input-tools" autoComplete="off" /></Field>
        <p className="legacy-tools-dialog-note">{t("datarc_key_note")}</p>
        {datarcEditTarget ? <DatarcScopePanel tool={datarcEditTarget} refreshing={rowBusy === datarcEditTarget.id} onRefresh={() => void refreshScope(datarcEditTarget)} /> : null}
        {builtinSaveError ? <Notice tone="error" className="legacy-tools-dialog-error">{t(builtinSaveError)}</Notice> : null}
        <div className="legacy-tools-dialog-actions"><Button variant="secondary" onClick={closeDatarcConfig}>{t("cancel")}</Button><Button variant="primary" onClick={() => void saveDatarc()} disabled={busy || !datarcName.trim() || !datarcEndpoint.trim() || (!datarcEditTarget && !datarcKey.trim())}>{t("verify_save")}</Button></div>
      </Dialog> : null}

      {datarcDeleteTarget ? <ConfirmDialog
        title={t("datarc_delete_title", { name: datarcDeleteTarget.display_name })}
        description={t("datarc_delete_desc")}
        confirmLabel={t("action_delete")}
        cancelLabel={t("cancel")}
        busy={busy}
        onCancel={() => setDatarcDeleteTarget(null)}
        onConfirm={() => void confirmDeleteDatarc()}
      /> : null}

      {refreshTarget ? (
        <SourceRefreshDialog
          source={refreshTarget}
          returnFocusRef={refreshReturnFocusRef}
          onClose={() => setRefreshTarget(null)}
          onApplied={(result) => void onRefreshApplied(refreshTarget, result)}
        />
      ) : null}

      {chooseTarget ? (
        <SourceToolSelectionDialog
          source={chooseTarget.source}
          highlight={chooseTarget.highlight}
          returnFocusRef={chooseReturnFocusRef}
          onClose={() => setChooseTarget(null)}
          onSaved={(result) => void onChooseToolsSaved(chooseTarget.source, result)}
        />
      ) : null}

      {editTarget ? <EditDialog source={editTarget} busy={busy} onClose={() => setEditTarget(null)} onSave={(p) => void onSaveEdit(p)} /> : null}

      {pendingImpact ? (
        <ConfirmDialog
          title={pendingImpact.action === "delete"
            ? t("confirm_delete_title", { name: pendingImpact.source.name })
            : pendingImpact.action === "remove_auth"
              ? t("confirm_remove_auth_title", { name: pendingImpact.source.name })
              : t("confirm_disable_title", { name: pendingImpact.source.name })}
          description={<ImpactDescription pending={pendingImpact} />}
          confirmLabel={pendingImpact.action === "delete"
            ? t("action_delete")
            : pendingImpact.action === "remove_auth"
              ? t("action_remove_auth_confirm")
              : t("action_disable_confirm")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setPendingImpact(null)}
          onConfirm={() => void onConfirmImpact()}
        />
      ) : null}

      {legacyDeleteTarget ? (
        <ConfirmDialog
          title={t("confirm_delete_title", { name: legacyDeleteTarget.name })}
          description={t("confirm_delete_desc", { n: legacyDeleteTarget.op_count })}
          confirmLabel={t("action_delete")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setLegacyDeleteTarget(null)}
          onConfirm={() => void onConfirmLegacyDelete()}
        />
      ) : null}

      {usageTarget ? (
        <ResourceUsageDrawer
          title={usageTarget.title}
          load={loadUsage}
          onClose={() => setUsageTarget(null)}
          onNavigate={(href) => router.push(href)}
        />
      ) : null}
    </div>
  );
}

function ImpactDescription({ pending }: { pending: PendingImpact }) {
  const t = useTranslations("settings.tools");
  const { summary } = pending.impact;
  const hasActiveImpact = summary.current_agent_count > 0
    || summary.draft_agent_count > 0
    || summary.running_task_count > 0
    || summary.scheduled_binding_count > 0;
  return (
    <div className="legacy-tools-impact">
      <div>
        {pending.action === "delete"
          ? t("impact_delete_source_lead", { n: pending.source.op_count })
          : pending.action === "remove_auth"
            ? t("impact_remove_auth_lead", { n: pending.source.op_count })
            : t("impact_disable_lead", { n: pending.source.op_count })}
      </div>
      {hasActiveImpact ? (
        <div className="legacy-tools-impact-grid"
        >
          {[
            t("impact_current_agents", { n: summary.current_agent_count }),
            t("impact_draft_agents", { n: summary.draft_agent_count }),
            t("impact_running_tasks", { n: summary.running_task_count }),
            t("impact_schedules", { n: summary.scheduled_binding_count }),
          ].map((label, index) => (
            <div
              key={label}
              className={`legacy-tools-impact-cell${index % 2 ? " is-col2" : ""}${index >= 2 ? " is-row2" : ""}`}
            >
              {label}
            </div>
          ))}
        </div>
      ) : (
        <div className="legacy-tools-impact-none">
          {t("impact_no_active_dependencies")}
        </div>
      )}
      <div className="legacy-tools-impact-note">
        {t("impact_runtime_note")}
        {summary.scheduled_binding_count > 0 ? ` ${t("impact_schedules_note")}` : ""}
      </div>
    </div>
  );
}

function EmptyState({ onNew }: { onNew: () => void }) {
  const t = useTranslations("settings.tools");
  return (
    <div className="legacy-ai-tools-empty ai-tools-empty">
      <div className="legacy-ai-tools-empty-title ai-tools-empty-title">{t("empty_title")}</div>
      <div className="legacy-ai-tools-empty-sub ai-tools-empty-sub">{t("empty_sub")}</div>
      <Button variant="primary" onClick={onNew}>
        <CharIconText ch="＋">{t("new_source")}</CharIconText>
      </Button>
    </div>
  );
}
