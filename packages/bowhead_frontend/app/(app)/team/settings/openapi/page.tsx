"use client";

import { ADDITIONAL_OPENAPI_SCOPES } from "@/edition/openapi-scopes";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { ErrorDiagnosticsInline } from "@/components/error-notice";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";
import { Tab, TabList, TabPanel, Tabs } from "@/components/ui/tabs";
import { appPath, publicBaseUrl, unprefixAppPath } from "@/lib/base-path";
import { copyTextWithFallback } from "@/lib/clipboard";
import { responseRequestId } from "@/lib/error-presentation";
import { keepFocusAfterRemoval } from "@/lib/focus-after-removal";
import {
  AGENT_CI_SCOPES,
  defaultKeyExpiry,
  keyFormPreset,
  localDateTime,
  withoutKeyFormPreset,
} from "@/lib/openapi-key-form";
import { useAppRouter } from "@/lib/use-app-router";

type OpenApiTab = "keys" | "docs" | "logs";
type Key = {
  id: string;
  name: string;
  token_prefix: string;
  scopes: string[];
  expires_at: string | null;
  status: "active" | "disabled";
  last_used_at: string | null;
};
type Log = {
  key_name: string | null;
  key_prefix: string | null;
  http_method: string;
  path: string;
  status_code: number;
  request_summary: string | null;
  response_summary: string | null;
  created_at: string;
};
const SCOPES: readonly string[] = [
  ...ADDITIONAL_OPENAPI_SCOPES,
  "agent_versions:read",
  "agent_versions:write",
];
type T = ReturnType<typeof useTranslations<"team_openapi">>;

async function api(path: string, init?: RequestInit) {
  const response = await fetch(`/api/v1/team-openapi${path}`, {
    credentials: "include",
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: { code?: string };
    } | null;
    // Error/status codes and request ID go only in diagnostics (collapsed section/Copy Diagnostics); page localizes by error code.
    throw Object.assign(new Error(body?.detail?.code ?? "request_failed"), {
      status: response.status,
      code: body?.detail?.code,
      requestId: responseRequestId(response),
    });
  }
  return response.json();
}

// Each known backend error code has actionable copy; generic only for unrecognized errors.
const KNOWN_ERROR_CODES = ["invalid_expiry", "invalid_scopes", "key_not_found", "openapi_unavailable"] as const;
function errorMessage(t: T, error: unknown): string {
  const code = error && typeof error === "object" && "code" in error ? (error as { code?: unknown }).code : undefined;
  return typeof code === "string" && (KNOWN_ERROR_CODES as readonly string[]).includes(code)
    ? t(`error_${code as (typeof KNOWN_ERROR_CODES)[number]}`)
    : t("error");
}

/** datetime-local min: current local time, minute precision. */
function localDateTimeNow(): string {
  return localDateTime(new Date());
}
function displayDate(value: string | null, locale: string, fallback: string) {
  if (!value) return fallback;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale);
}
function scopeName(scope: string, t: T) {
  const key = `scope_${scope.replace(":", "_")}`;
  return t.has(key) ? t(key) : scope;
}
function displayPathParameters(value: string | undefined, t: T) {
  if (!value) return "—";
  return value
    .split(", ")
    .map((parameter) => {
      if (parameter === "space_id") return `${t("space_id")} (${parameter})`;
      if (parameter === "user_id") return `${t("user_id")} (${parameter})`;
      return parameter;
    })
    .join(", ");
}

export default function TeamOpenApiPage() {
  const t = useTranslations("team_openapi");
  const [tab, setTab] = useState<OpenApiTab>("keys");
  const tabs: Array<[OpenApiTab, string]> = [
    ["keys", t("tab_keys")],
    ["docs", t("tab_docs")],
    ["logs", t("tab_logs")],
  ];
  return (
    <TeamSettingsRoute active="openapi">
      <main className="team-openapi-page">
        <header className="team-openapi-page__header">
          <h1 className="ui-page-title">{t("title")}</h1>
          <p className="ui-page-desc">{t("subtitle")}</p>
          <p className="ui-page-desc">{t("pipeline_stop_note")}</p>
        </header>
        <Tabs value={tab} onValueChange={setTab}>
          <TabList appearance="underline" className="team-openapi-tabs">
            {tabs.map(([id, label]) => (
              <Tab className={tab === id ? "is-active" : undefined} key={id} value={id}>
                {label}
              </Tab>
            ))}
          </TabList>
          <TabPanel value="keys" as="section" className="team-openapi-panel">
            <KeysPanel t={t} />
          </TabPanel>
          <TabPanel value="docs" as="section" className="team-openapi-panel">
            <DocsPanel t={t} />
          </TabPanel>
          <TabPanel value="logs" as="section" className="team-openapi-panel">
            <LogsPanel t={t} />
          </TabPanel>
        </Tabs>
      </main>
    </TeamSettingsRoute>
  );
}

function KeysPanel({ t }: { t: T }) {
  const locale = useLocale();
  const [keys, setKeys] = useState<Key[]>([]);
  const [dialog, setDialog] = useState<"create" | Key | null>(null);
  const [name, setName] = useState("");
  // No permission is preselected: the admin chooses what the integration needs.
  const [scopes, setScopes] = useState<string[]>([]);
  const [expiresAt, setExpiresAt] = useState("");
  const [permanent, setPermanent] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const [tokenCopied, setTokenCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ error: unknown } | null>(null);
  // Confirm Revoke/Regenerate: both invalidate the old key immediately and disconnect integrations using it.
  // On failure close confirmation, show error on page (background unreadable while confirmation is open).
  const [pending, setPending] = useState<{ action: "revoke" | "rotate"; key: Key } | null>(null);
  const keysHeadingRef = useRef<HTMLHeadingElement>(null);
  const load = useCallback(async () => setKeys((await api("/keys")).items), []);
  useEffect(() => {
    void load().catch((reason: unknown) =>
      setError({ error: reason }),
    );
  }, [load]);
  const router = useAppRouter();
  // `?preset=agent_ci` starts the key form with the Agent CI preset. It applies once per visit and leaves
  // the URL, so a locale change or a reload
  // doesn't reopen the form over a name the admin already typed.
  const presetApplied = useRef(false);
  useEffect(() => {
    if (presetApplied.current) return;
    presetApplied.current = true;
    const search = window.location.search;
    if (keyFormPreset(search) === "agent_ci") {
      setName(t("agent_ci_key_name"));
      setScopes([...AGENT_CI_SCOPES]);
      setExpiresAt(defaultKeyExpiry());
      setPermanent(false);
      setDialog("create");
      router.replace(`${unprefixAppPath(window.location.pathname)}${withoutKeyFormPreset(search)}${window.location.hash}`, { scroll: false });
    }
  }, [router, t]);
  const request = async (path: string, init?: RequestInit) => {
    setBusy(true);
    setError(null);
    try {
      const result = await api(path, init);
      if (result.token) {
        setToken(result.token);
        setTokenCopied(false);
      }
      await load();
      return true;
    } catch (reason) {
      setError({ error: reason });
      return false;
    } finally {
      setBusy(false);
    }
  };
  const toggleKey = (key: Key) =>
    request(`/keys/${key.id}/${key.status === "active" ? "disable" : "enable"}`, { method: "POST" });
  const confirmPending = async () => {
    if (!pending || busy) return;
    const { action, key } = pending;
    const ok = action === "revoke"
      ? await request(`/keys/${key.id}`, { method: "DELETE" })
      : await request(`/keys/${key.id}/rotate`, { method: "POST" });
    // Successful revocation removes the card; return focus to Existing Keys heading.
    if (ok && action === "revoke") keepFocusAfterRemoval(() => keysHeadingRef.current);
    setPending(null);
  };
  const openCreate = () => {
    setName("");
    setScopes([]);
    setExpiresAt(defaultKeyExpiry());
    setPermanent(false);
    setDialog("create");
  };
  const openEdit = (key: Key) => {
    setName(key.name);
    setScopes(key.scopes);
    setExpiresAt(key.expires_at?.slice(0, 16) ?? "");
    setPermanent(!key.expires_at);
    setDialog(key);
  };
  const save = async () => {
    const created = dialog === "create";
    const path = created ? "/keys" : `/keys/${(dialog as Key).id}`;
    const body = created
      ? {
          name,
          scopes,
          permanent,
          expires_at: expiresAt ? new Date(expiresAt).toISOString() : undefined,
        }
      : {
          scopes,
          permanent,
          expires_at: permanent ? undefined : new Date(expiresAt).toISOString(),
        };
    if (
      await request(path, {
        method: created ? "POST" : "PATCH",
        body: JSON.stringify(body),
      })
    )
      setDialog(null);
  };
  const allSelected = scopes.length === SCOPES.length;
  return (
    <>
      {error ? (
        <Notice tone="error">
          {errorMessage(t, error.error)}
        </Notice>
      ) : null}
      {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
      {token ? (
        <Notice tone="success">
          <strong>{t("token_once_title")}</strong>
          <br />
          {t("token_once_hint")}
          <code className="team-openapi-token">{token}</code>
          <Button
            onClick={() => {
              void copyTextWithFallback(token, t("token_copy_prompt")).then((copied) => {
                if (copied) setTokenCopied(true);
              });
            }}
            size="sm"
          >
            {tokenCopied ? t("token_copied") : t("token_copy")}
          </Button>
        </Notice>
      ) : null}
      <section className="team-openapi-card">
        <div className="team-openapi-card__heading team-openapi-card__heading--action">
          <div>
            <h2 ref={keysHeadingRef} tabIndex={-1}>{t("keys_title")}</h2>
            <p>{t("keys_hint")}</p>
          </div>
          <Button onClick={openCreate} variant="primary">
            {t("add_key")}
          </Button>
        </div>
        {keys.length === 0 ? (
          <p className="team-openapi-empty">{t("keys_empty")}</p>
        ) : (
          <div className="team-openapi-key-grid">
            {keys.map((key) => (
              <article className="team-openapi-key-card" key={key.id}>
                <div className="team-openapi-key-card__top">
                  <div>
                    <h3>{key.name}</h3>
                    <code>{key.token_prefix}…</code>
                  </div>
                  <div className="ui-card-head-end">
                    <span
                      className={`team-openapi-status team-openapi-status--${key.status}`}
                    >
                      {key.status === "active"
                        ? t("status_active")
                        : t("status_disabled")}
                    </span>
                    <RowActionsMenu
                      label={t("more_actions_for", { name: key.name })}
                      pending={busy}
                      actions={[
                        { id: "edit", label: t("edit"), disabled: busy, onSelect: () => openEdit(key) },
                        {
                          id: "toggle",
                          label: key.status === "active" ? t("disable") : t("enable"),
                          disabled: busy,
                          onSelect: () => void toggleKey(key),
                        },
                        {
                          id: "rotate",
                          label: t("regenerate"),
                          danger: true,
                          disabled: busy,
                          onSelect: () => setPending({ action: "rotate", key }),
                        },
                        {
                          id: "revoke",
                          label: t("revoke"),
                          danger: true,
                          disabled: busy,
                          onSelect: () => setPending({ action: "revoke", key }),
                        },
                      ]}
                    />
                  </div>
                </div>
                <dl>
                  <div>
                    <dt>{t("expiry_label")}</dt>
                    <dd>{displayDate(key.expires_at, locale, t("never"))}</dd>
                  </div>
                  <div>
                    <dt>{t("last_used_label")}</dt>
                    <dd>
                      {displayDate(key.last_used_at, locale, t("not_used"))}
                    </dd>
                  </div>
                </dl>
                <div>
                  {key.scopes.map((scope) => (
                    <span className="team-openapi-scope" key={scope}>
                      {scope}
                    </span>
                  ))}
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
      {pending ? (
        <ConfirmDialog
          title={t(pending.action === "revoke" ? "revoke_confirm_title" : "rotate_confirm_title", { name: pending.key.name })}
          description={t(pending.action === "revoke" ? "revoke_confirm_body" : "rotate_confirm_body")}
          confirmLabel={t(pending.action === "revoke" ? "revoke" : "regenerate")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={() => void confirmPending()}
        />
      ) : null}
      {dialog ? (
        <KeyDialog
          allSelected={allSelected}
          dialog={dialog}
          expiresAt={expiresAt}
          name={name}
          onClose={() => setDialog(null)}
          onSave={() => void save()}
          onSetExpiresAt={setExpiresAt}
          onSetName={setName}
          onSetPermanent={setPermanent}
          onSetScopes={setScopes}
          permanent={permanent}
          scopes={scopes}
          t={t}
          busy={busy}
        />
      ) : null}
    </>
  );
}

function KeyDialog({
  allSelected,
  busy,
  dialog,
  expiresAt,
  name,
  onClose,
  onSave,
  onSetExpiresAt,
  onSetName,
  onSetPermanent,
  onSetScopes,
  permanent,
  scopes,
  t,
}: {
  allSelected: boolean;
  busy: boolean;
  dialog: "create" | Key;
  expiresAt: string;
  name: string;
  onClose: () => void;
  onSave: () => void;
  onSetExpiresAt: (value: string) => void;
  onSetName: (value: string) => void;
  onSetPermanent: (value: boolean) => void;
  onSetScopes: (value: string[]) => void;
  permanent: boolean;
  scopes: string[];
  t: T;
}) {
  const creating = dialog === "create";
  const toggle = (scope: string, checked: boolean) =>
    onSetScopes(
      checked ? [...scopes, scope] : scopes.filter((item) => item !== scope),
    );
  return (
    <Dialog
      busy={busy}
      footer={
        <>
          <Button disabled={busy} onClick={onClose}>
            {t("cancel")}
          </Button>
          <Button
            disabled={
              busy ||
              !scopes.length ||
              (creating && !name.trim()) ||
              // A new key with no date expires after 30 days; an edited key needs an explicit date.
              (!creating && !permanent && !expiresAt)
            }
            onClick={onSave}
            variant="primary"
          >
            {creating ? t("create") : t("save")}
          </Button>
        </>
      }
      onClose={onClose}
      title={creating ? t("create_title") : t("edit_title")}
    >
      <div className="team-openapi-form">
        <Field id="openapi-key-name" label={t("name_label")}>
          <input
            className="settings-input"
            disabled={!creating}
            id="openapi-key-name"
            onChange={(event) => onSetName(event.target.value)}
            value={name}
          />
        </Field>
        <fieldset className="team-openapi-scopes">
          <div className="team-openapi-scope-heading">
            <legend>{t("scopes_label")}</legend>
            {creating ? (
              <Button
                onClick={() => {
                  onSetScopes([...AGENT_CI_SCOPES]);
                  if (!name.trim()) onSetName(t("agent_ci_key_name"));
                }}
                size="sm"
              >
                {t("preset_agent_ci")}
              </Button>
            ) : null}
            <Button
              onClick={() => onSetScopes(allSelected ? [] : [...SCOPES])}
              size="sm"
            >
              {allSelected ? t("clear_all") : t("select_all")}
            </Button>
          </div>
          <p>{t("scopes_hint")}</p>
          {creating ? <p>{t("preset_agent_ci_hint")}</p> : null}
          <div>
            {SCOPES.map((scope) => (
              <label key={scope}>
                <input
                  checked={scopes.includes(scope)}
                  onChange={(event) => toggle(scope, event.target.checked)}
                  type="checkbox"
                />
                <span>{scopeName(scope, t)}</span>
                <code>{scope}</code>
              </label>
            ))}
          </div>
        </fieldset>
        <Field
          hint={permanent ? t("permanent_expiry_hint") : creating ? t("expiry_hint") : undefined}
          id="openapi-key-expiry"
          label={t("expiry_label")}
        >
          <input
            className="settings-input"
            disabled={permanent}
            id="openapi-key-expiry"
            min={localDateTimeNow()}
            onChange={(event) => onSetExpiresAt(event.target.value)}
            type="datetime-local"
            value={expiresAt}
          />
        </Field>
        <label className="team-openapi-permanent">
          <input
            checked={permanent}
            onChange={(event) => onSetPermanent(event.target.checked)}
            type="checkbox"
          />{" "}
          {t("permanent")}
        </label>
      </div>
    </Dialog>
  );
}

function DocsPanel({ t }: { t: T }) {
  const [items, setItems] = useState<Array<{
    method: string;
    path: string;
    scope: string;
    summary: string;
    body?: string;
    path_params?: string;
    response_example?: Record<string, unknown>;
    notes?: string[];
  }>>([]);
  const [referenceMarkdownUrl, setReferenceMarkdownUrl] = useState("");
  const [referenceUrlCopied, setReferenceUrlCopied] = useState(false);
  useEffect(() => {
    void api("/documentation").then((result) => setItems(result.endpoints));
    const path = appPath("/api/openapi/v1/reference.md");
    const baseUrl = publicBaseUrl();
    setReferenceMarkdownUrl(baseUrl ? `${baseUrl}/api/openapi/v1/reference.md` : new URL(path, window.location.origin).toString());
  }, []);
  const downloadMarkdown = () => {
    window.location.assign(appPath("/api/openapi/v1/reference.md"));
  };
  const copyReferenceMarkdownUrl = () => {
    void copyTextWithFallback(referenceMarkdownUrl, t("reference_markdown_copy_prompt")).then((copied) => {
      if (copied) setReferenceUrlCopied(true);
    });
  };
  return (
    <>
      <section className="team-openapi-card">
        <div className="team-openapi-card__heading team-openapi-card__heading--action">
          <div>
            <h2>{t("docs_title")}</h2>
            <p>{t("docs_hint")}</p>
          </div>
          <Button disabled={items.length === 0} onClick={downloadMarkdown}>
            {t("download_markdown")}
          </Button>
        </div>
        <section className="team-openapi-reference__identity">
          <h3>{t("base_path_label")}</h3>
          <p>{t("base_path_hint")}</p>
          <code>{t("base_path_description")}</code>
          <p>{t("reference_markdown_hint")}</p>
          <div className="team-openapi-reference__download-url">
            <code>{referenceMarkdownUrl}</code>
            <Button disabled={!referenceMarkdownUrl} onClick={copyReferenceMarkdownUrl} size="sm">
              {referenceUrlCopied ? t("reference_markdown_copied") : t("reference_markdown_copy")}
            </Button>
          </div>
        </section>
        <div className="team-openapi-auth">
          <code>Authorization: Bearer &lt;token&gt;</code>
          <span>{t("write_implies_read")}</span>
        </div>
        <section className="team-openapi-reference__identity">
          <h3>{t("identity_title")}</h3>
          <p>{t("identity_description")}</p>
          <ul>
            <li>{t("identity_email_rule")}</li>
            <li>{t("identity_user_id_rule")}</li>
            <li>{t("identity_display_name_rule")}</li>
          </ul>
        </section>
        <section className="team-openapi-reference__identity">
          <h3>{t("sync_title")}</h3>
          <ol>
            {[t("sync_step_1"), t("sync_step_2"), t("sync_step_3"), t("sync_step_4")].map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </section>
        <div className="team-openapi-reference">
          {items.map((item) => (
            <article className="team-openapi-reference__endpoint" key={`${item.method}${item.path}`}>
              <div className="team-openapi-reference__route">
                <span className={`team-openapi-method team-openapi-method--${item.method.toLowerCase()}`}>
                  {item.method}
                </span>
                <code>{item.path}</code>
              </div>
              <p>{item.summary}</p>
              <dl>
                <div>
                  <dt>{t("required_scope")}</dt>
                  <dd><code className="team-openapi-reference__scope">{item.scope}</code></dd>
                </div>
                <div>
                  <dt>{t("path_parameters")}</dt>
                  <dd>{displayPathParameters(item.path_params, t)}</dd>
                </div>
                <div>
                  <dt>{t("request_label")}</dt>
                  <dd>{item.body ? <code>{item.body}</code> : "—"}</dd>
                </div>
                {item.notes?.length ? (
                  <div>
                    <dt>{t("rules_label")}</dt>
                    <dd>
                      <ul>
                        {item.notes.map((note) => (
                          <li key={note}>{note}</li>
                        ))}
                      </ul>
                    </dd>
                  </div>
                ) : null}
                <div className="team-openapi-reference__response-example">
                  <dt>{t("response_example")}</dt>
                  <dd><pre>{JSON.stringify(item.response_example, null, 2)}</pre></dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      </section>
    </>
  );
}
function LogsPanel({ t }: { t: T }) {
  const locale = useLocale();
  const [items, setItems] = useState<Log[]>([]);
  useEffect(() => {
    void api("/logs").then((result) => setItems(result.items));
  }, []);
  return (
    <>
      <p className="team-openapi-logs__hint">{t("logs_hint")}</p>
      {items.length === 0 ? (
        <section className="team-openapi-card">
          <p className="team-openapi-empty">{t("logs_empty")}</p>
        </section>
      ) : (
        <div className="team-openapi-logs">
          {items.map((item, index) => (
            <article className="team-openapi-log" key={`${item.created_at}-${index}`}>
              <header className="team-openapi-log__header">
                <div className="team-openapi-log__route">
                  <span className={`team-openapi-method team-openapi-method--${item.http_method.toLowerCase()}`}>
                    {item.http_method}
                  </span>
                  <code>{item.path}</code>
                </div>
                <span className={`team-openapi-log__status team-openapi-log__status--${logStatusTone(item.status_code)}`}>
                  {item.status_code}
                </span>
              </header>
              <dl className="team-openapi-log__meta">
                <div>
                  <dt>{t("time_label")}</dt>
                  <dd>
                    <time dateTime={item.created_at}>{displayDate(item.created_at, locale, "—")}</time>
                  </dd>
                </div>
                <div>
                  <dt>{t("key_label")}</dt>
                  <dd>
                    <strong>{item.key_name ?? t("deleted_key")}</strong>
                    {item.key_prefix ? <code>{item.key_prefix}…</code> : null}
                  </dd>
                </div>
              </dl>
              <dl className="team-openapi-log__summaries">
                <div>
                  <dt>{t("request_summary_label")}</dt>
                  <dd>{item.request_summary ?? "—"}</dd>
                </div>
                <div>
                  <dt>{t("response_summary_label")}</dt>
                  <dd>{item.response_summary ?? "—"}</dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      )}
    </>
  );
}

function logStatusTone(statusCode: number): "success" | "warning" | "error" {
  if (statusCode >= 500) return "error";
  if (statusCode >= 400) return "warning";
  return "success";
}
