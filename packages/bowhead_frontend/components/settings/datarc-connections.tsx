"use client";

/**
 * Settings/Data Connections · team datarc connection management (ws-datarc-decouple P2b).
 *
 * team-admin manages the team's datarc BI connections (the registry the gateway resolves at
 * turn-start). Each card = one connection (name / base_url / key_hint / account / status) with
 * rotate-key + archive actions. Add/rotate modals offer a "Test connection" dry-run (/validate).
 * The auth key is write-only — the backend never echoes it back (only key_hint = last 4).
 *
 * Backend gate is team_admin; a non-admin caller gets a 403 surfaced in the error banner.
 */

import { isNetworkFailure } from "@/lib/transport-error";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { useLocale, useTranslations } from "next-intl";

import {
  type DatarcConnectionErrorCode,
  type DatarcConnection,
  type ValidateResult,
  createConnection,
  isDatarcConnectionErrorCode,
  listConnections,
  rotateConnectionKey,
  setConnectionStatus,
  validateConnection,
} from "@/lib/admin-datarc-connections-api";
import { ApiError } from "@/lib/api-client";
import { ErrorNotice } from "@/components/error-notice";
import { DialogShell } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { DatarcMark } from "@/components/settings/data-source-icons";
import {
  projectTransportFailure,
  toErrorPresentation,
} from "@/lib/error-presentation";
import type { ErrorPresentation } from "@/lib/error-presentation";
import type { Locale } from "@/lib/locale";

type Modal =
  | { kind: "none" }
  | { kind: "add-datarc" }
  | { kind: "rotate"; conn: DatarcConnection };

type ConnectionOperation =
  | "load"
  | "update"
  | "create"
  | "rotate"
  | "validate";

const ERROR_MESSAGE_KEYS: Record<
  DatarcConnectionErrorCode,
  `errors.${DatarcConnectionErrorCode}`
> = {
  datarc_connection_auth_rejected:
    "errors.datarc_connection_auth_rejected",
  datarc_connection_configuration_invalid:
    "errors.datarc_connection_configuration_invalid",
  datarc_connection_create_failed:
    "errors.datarc_connection_create_failed",
  datarc_connection_load_failed:
    "errors.datarc_connection_load_failed",
  datarc_connection_not_found:
    "errors.datarc_connection_not_found",
  datarc_connection_secret_storage_unavailable:
    "errors.datarc_connection_secret_storage_unavailable",
  datarc_connection_store_unavailable:
    "errors.datarc_connection_store_unavailable",
  datarc_connection_unreachable:
    "errors.datarc_connection_unreachable",
  datarc_connection_update_failed:
    "errors.datarc_connection_update_failed",
  datarc_connection_url_rejected:
    "errors.datarc_connection_url_rejected",
  datarc_connection_validation_failed:
    "errors.datarc_connection_validation_failed",
};

const SHARED_ERROR_MESSAGE_KEYS: Readonly<Record<string, string>> = {
  insufficient_role: "errors.insufficient_role",
  entitlement_unavailable: "errors.entitlement_unavailable",
  over_grace: "errors.over_grace",
  suspended: "errors.suspended",
  needs_license: "errors.needs_license",
  tamper: "errors.tamper",
  clock_tamper: "errors.clock_tamper",
};

function useDatarcConnectionErrorPresentation() {
  const t = useTranslations("settings.datarc");
  // Pass the UI language unchanged: authorization-error explanations use that language's catalog (German no longer falls back to Chinese).
  const locale = useLocale() as Locale;

  return useCallback(
    (
      error: unknown,
      operation: ConnectionOperation,
    ): ErrorPresentation => {
      const genericMessage = t(`errors.${operation}_failed`);
      let presentation: ErrorPresentation;
      if (error instanceof ApiError) {
        presentation = toErrorPresentation(error, {
          locale,
          surface: "connection",
          allowedDisclosures: ["admin"],
          genericMessage,
          messageForCode: (code) => {
            if (isDatarcConnectionErrorCode(code)) {
              return t(ERROR_MESSAGE_KEYS[code]);
            }
            const sharedKey = SHARED_ERROR_MESSAGE_KEYS[code];
            return sharedKey ? t(sharedKey) : null;
          },
        });
      } else if (isNetworkFailure(error)) {
        presentation =
          projectTransportFailure(
            {
              phase: operation === "load" ? "receive" : "send",
              timeoutTriggered: false,
              userAborted: false,
              onlineHint:
                typeof navigator === "undefined"
                  ? undefined
                  : navigator.onLine,
            },
            {
              requestTimedOut: t("errors.request_timed_out"),
              serviceUnreachable: t("errors.service_unreachable"),
              retry: t("retry"),
            },
          ) ?? { message: genericMessage };
      } else {
        presentation = { message: genericMessage };
      }

      if (operation === "load") {
        return {
          ...presentation,
          action: {
            kind: "retry",
            label: t("retry"),
          },
        };
      }
      const { action: _discardedAction, ...withoutAction } = presentation;
      return withoutAction;
    },
    [locale, t],
  );
}

export function SettingsDatarcConnections() {
  const t = useTranslations("settings.datarc");
  const presentError = useDatarcConnectionErrorPresentation();
  const presentErrorRef = useRef(presentError);
  presentErrorRef.current = presentError;
  const [items, setItems] = useState<DatarcConnection[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ErrorPresentation | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [modal, setModal] = useState<Modal>({ kind: "none" });

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    setLoadFailed(false);
    try {
      setItems(await listConnections(showArchived));
    } catch (e) {
      setError(presentErrorRef.current(e, "load"));
      setLoadFailed(true);
    } finally {
      setLoading(false);
    }
  }, [showArchived]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const showActiveRule = !loading && !loadFailed && items.some((item) => item.status === "active");

  async function handleArchiveToggle(conn: DatarcConnection) {
    setError(null);
    setLoadFailed(false);
    try {
      await setConnectionStatus(conn.id, conn.status === "active" ? "archived" : "active");
      await refresh();
    } catch (e) {
      setError(presentError(e, "update"));
    }
  }

  const addButton = (
    <Button
      variant="primary"
      onClick={() => setModal({ kind: "add-datarc" })}
      disabled={loading}
    >
      {t("add_btn")}
    </Button>
  );
  const pageTitle = (
    <>
      <h1 className="ui-page-title legacy-datarc-title">
        {t("title")}
      </h1>
      <p className="ui-page-desc legacy-datarc-desc">
        {t("subtitle")}
      </p>
    </>
  );

  return (
    <div className="legacy-ai-ds-root ai-ds">
      {/* Add Data Source is the only primary action at the page header's right. */}
      <div className="ui-page-head ai-page-head">
        <div className="ai-page-head-text">{pageTitle}</div>
        <div className="ai-page-head-actions">{addButton}</div>
      </div>
      {showActiveRule ? (
        <Notice tone="info">
          {t("active_rule")}
        </Notice>
      ) : null}

      <div className="legacy-ai-ds-toolbar ai-toolbar">
        <label className="legacy-ai-ds-archived ai-check">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
          />
          {t("show_archived")}
        </label>
      </div>

      <h2 className="legacy-ai-ds-h2 ui-section-title ai-section-title">{t("current_list")}</h2>

      {error && (
        <ErrorNotice
          message={error.message}
          action={error.action}
          diagnostics={error.diagnostics}
          surface="connection"
          onAction={loadFailed ? () => void refresh() : undefined}
        />
      )}

      <div className="legacy-ai-ds-list ai-card-list">
        {loading ? (
          <LoadingSkeleton label={t("loading")} />
        ) : loadFailed && items.length === 0 ? (
          null
        ) : items.length === 0 ? (
          <div className="legacy-ai-ds-empty ui-empty">
            {t("empty")}
          </div>
        ) : (
          items.map((c) => (
            <ConnectionCard
              key={c.id}
              conn={c}
              onRotate={() => setModal({ kind: "rotate", conn: c })}
              onArchiveToggle={() => handleArchiveToggle(c)}
            />
          ))
        )}
      </div>

      {modal.kind === "add-datarc" && (
        <AddConnectionModal
          onCancel={() => setModal({ kind: "none" })}
          onSaved={() => { setModal({ kind: "none" }); refresh(); }}
          hasActiveConnection={items.some((item) => item.status === "active")}
        />
      )}
      {modal.kind === "rotate" && (
        <RotateKeyModal
          conn={modal.conn}
          onCancel={() => setModal({ kind: "none" })}
          onSaved={() => { setModal({ kind: "none" }); refresh(); }}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Connection card
// ─────────────────────────────────────────────────────────────

function ConnectionCard({
  conn, onRotate, onArchiveToggle,
}: {
  conn: DatarcConnection;
  onRotate: () => void;
  onArchiveToggle: () => void;
}) {
  const t = useTranslations("settings.datarc");
  const isActive = conn.status === "active";
  return (
    <div className={`legacy-ai-ds-card ai-source-card${isActive ? "" : " is-archived"}`}>
      <div className="legacy-ai-ds-card-mark"><DatarcMark /></div>
      <div className="legacy-ai-ds-card-body">
        <div className="legacy-ai-ds-card-head">
          <span className="legacy-ai-ds-card-name ai-source-name">
            {conn.name}
          </span>
          <span className={`legacy-ai-ds-status ai-source-status${isActive ? " is-active" : ""}`}>
            {isActive ? t("status_active") : t("status_archived")}
          </span>
        </div>
        <div className="legacy-ai-ds-card-meta ai-source-meta">
          {t("key_hint_label")}: <code className="legacy-ai-mono">
            {conn.key_hint ? `••••${conn.key_hint}` : "—"}
          </code>
          {" · "}
          {t("account_label")}: <code className="legacy-ai-mono">
            {conn.account_uid || "—"}
          </code>
        </div>
        <div className="legacy-ai-ds-card-url ai-source-url">
          {conn.base_url}
        </div>
      </div>
      {/* Card Rotate Key and Archive actions live in the row-end “⋯” (decisions 3, 5). */}
      <RowActionsMenu
        label={t("more_actions_for", { name: conn.name })}
        actions={[
          { id: "rotate", label: t("rotate"), onSelect: onRotate },
          { id: "archive", label: isActive ? t("archive") : t("unarchive"), onSelect: onArchiveToggle },
        ]}
      />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Datarc connection modal
// ─────────────────────────────────────────────────────────────

function AddConnectionModal({
  onCancel, onSaved, hasActiveConnection,
}: { onCancel: () => void; onSaved: () => void; hasActiveConnection: boolean }) {
  const t = useTranslations("settings.datarc");
  const presentError = useDatarcConnectionErrorPresentation();
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [authKey, setAuthKey] = useState("");
  const [accountUid, setAccountUid] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ErrorPresentation | null>(null);
  const [validateMsg, setValidateMsg] = useState<string | null>(null);
  const [activation, setActivation] = useState<"active" | "archived" | null>(
    hasActiveConnection ? null : "active",
  );

  const canSubmit = name.trim().length > 0 && baseUrl.trim().length > 0 && authKey.trim().length > 0 && activation !== null;

  async function onTest() {
    setErr(null);
    setValidateMsg(null);
    setBusy(true);
    try {
      const r: ValidateResult = await validateConnection({
        base_url: baseUrl, auth_key: authKey, account_uid: accountUid || undefined,
      });
      if (r.ok) {
        const count = r.project_count ?? r.workspace_count ?? 0;
        // embed-via-openapi: advisory suffix — older gateways omit `embed` (no suffix then)
        const embedSuffix =
          r.embed == null
            ? ""
            : ` · ${t(r.embed.available ? "validate_embed_on" : "validate_embed_off")}`;
        setValidateMsg(t("validate_ok", { count }) + embedSuffix);
      }
    } catch (e) {
      setErr(presentError(e, "validate"));
    } finally {
      setBusy(false);
    }
  }

  async function onSave() {
    setErr(null);
    setBusy(true);
    try {
      await createConnection({
        name, base_url: baseUrl, auth_key: authKey, account_uid: accountUid || undefined,
        status: activation ?? "active",
      });
      onSaved();
    } catch (e) {
      setErr(presentError(e, "create"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <ModalShell title={t("modal.add_title")} onCancel={onCancel} busy={busy}>
      <div className="legacy-datarc-modal-form">
        {hasActiveConnection ? (
          <fieldset className="legacy-datarc-modal-choice">
            <legend className="legacy-datarc-modal-choice-title">
              {t("modal.active_choice_title")}
            </legend>
            <p className="legacy-datarc-modal-choice-hint">
              {t("modal.active_choice_hint")}
            </p>
            <label className="legacy-datarc-modal-option">
              <input type="radio" name="activation" checked={activation === "archived"} onChange={() => setActivation("archived")} /> {t("modal.active_choice_preserve")}
              <span className="legacy-datarc-modal-option-hint">{t("modal.active_choice_preserve_hint")}</span>
            </label>
            <label className="legacy-datarc-modal-option-last">
              <input type="radio" name="activation" checked={activation === "active"} onChange={() => setActivation("active")} /> {t("modal.active_choice_replace")}
              <span className="legacy-datarc-modal-option-hint">{t("modal.active_choice_replace_hint")}</span>
            </label>
          </fieldset>
        ) : null}
        <Field id="settings-datarc-name" label={t("modal.name")}>
          <Input id="settings-datarc-name" name="connection_name" value={name} onChange={setName} placeholder="datarc-prod" />
        </Field>
        <Field id="settings-datarc-base-url" label={t("modal.base_url")}>
          <Input
            id="settings-datarc-base-url"
            name="base_url"
            type="url"
            value={baseUrl}
            onChange={setBaseUrl}
            placeholder="https://your-datarc.example"
          />
        </Field>
        <Field id="settings-datarc-auth-key" label={t("modal.auth_key")} hint={t("modal.auth_key_hint")}>
          <Input
            id="settings-datarc-auth-key"
            name="auth_key"
            type="password"
            autoComplete="off"
            value={authKey}
            onChange={setAuthKey}
          />
        </Field>
        <Field id="settings-datarc-account-uid" label={t("modal.account_uid")}>
          <Input
            id="settings-datarc-account-uid"
            name="account_uid"
            value={accountUid}
            onChange={setAccountUid}
          />
        </Field>

        {validateMsg && (
          <Notice tone="success">
            {validateMsg}
          </Notice>
        )}
        {err && (
          <ErrorNotice
            message={err.message}
            diagnostics={err.diagnostics}
            surface="connection"
          />
        )}
      </div>

      <div className="legacy-datarc-modal-actions-split">
        <Button
          variant="secondary"
          onClick={onTest}
          disabled={busy || baseUrl.trim().length === 0 || authKey.trim().length === 0}
        >
          {t("modal.validate")}
        </Button>
        <div className="legacy-datarc-modal-actions-group">
          <Button onClick={onCancel}>
            {t("modal.cancel")}
          </Button>
          <Button variant="primary" onClick={onSave} disabled={!canSubmit || busy}>
            {t("modal.save")}
          </Button>
        </div>
      </div>
    </ModalShell>
  );
}

// ─────────────────────────────────────────────────────────────
// Rotate key modal
// ─────────────────────────────────────────────────────────────

function RotateKeyModal({
  conn, onCancel, onSaved,
}: { conn: DatarcConnection; onCancel: () => void; onSaved: () => void }) {
  const t = useTranslations("settings.datarc");
  const presentError = useDatarcConnectionErrorPresentation();
  const [authKey, setAuthKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ErrorPresentation | null>(null);

  async function onSave() {
    setErr(null);
    setBusy(true);
    try {
      await rotateConnectionKey(conn.id, authKey);
      onSaved();
    } catch (e) {
      setErr(presentError(e, "rotate"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <ModalShell title={t("modal.rotate_title", { name: conn.name })} onCancel={onCancel} busy={busy}>
      <div className="legacy-datarc-modal-form">
        <Field id="settings-datarc-rotate-auth-key" label={t("modal.auth_key")} hint={t("modal.auth_key_hint")}>
          <Input
            id="settings-datarc-rotate-auth-key"
            name="auth_key"
            type="password"
            autoComplete="off"
            value={authKey}
            onChange={setAuthKey}
          />
        </Field>
      {err && (
        <ErrorNotice
          message={err.message}
          diagnostics={err.diagnostics}
          surface="connection"
        />
      )}
      </div>
      <div className="legacy-datarc-modal-actions">
        <Button onClick={onCancel}>
          {t("modal.cancel")}
        </Button>
        <Button variant="primary" onClick={onSave} disabled={authKey.trim().length === 0 || busy}>
          {t("modal.save")}
        </Button>
      </div>
    </ModalShell>
  );
}

// ─────────────────────────────────────────────────────────────
// Shared primitives (mirror components/settings/llm.tsx)
// ─────────────────────────────────────────────────────────────

function ModalShell({
  title, children, onCancel, busy = false,
}: { title: string; children: React.ReactNode; onCancel: () => void; busy?: boolean }) {
  const titleId = useId();
  return (
    <DialogShell
      onClose={onCancel}
      busy={busy}
      labelledBy={titleId}
      backdropClassName="legacy-datarc-modal-backdrop"
      panelClassName="legacy-datarc-modal"
    >
      <h3 id={titleId} className="legacy-datarc-modal-title">
        {title}
      </h3>
      {children}
    </DialogShell>
  );
}

function Field({
  id, label, hint, children,
}: { id: string; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="legacy-datarc-field-label">
        {label}
      </label>
      {children}
      {hint ? (
        <div className="legacy-datarc-field-hint">
          {hint}
        </div>
      ) : null}
    </div>
  );
}

function Input({
  id, name, value, onChange, placeholder, type, autoComplete,
}: {
  id: string;
  name: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: "text" | "password" | "url";
  autoComplete?: string;
}) {
  return (
    <input
      id={id}
      name={name}
      type={type ?? "text"}
      autoComplete={autoComplete}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="ui-input legacy-input-datarc"
    />
  );
}
