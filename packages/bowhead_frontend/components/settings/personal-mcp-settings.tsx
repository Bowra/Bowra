"use client";

import { useCallback, useEffect, useId, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  personalMcpApi,
  type PersonalMcpConnection,
  type PersonalMcpAgent,
} from "@/lib/personal-mcp-api";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { ChevronDown } from "@/components/icons";
import { appPath } from "@/lib/base-path";
import { formatDateTime } from "@/lib/locale";
import { Tab, TabList, TabPanel, Tabs } from "@/components/ui/tabs";
import { codedErrorMessage } from "@/lib/user-facing-error";

type PersonalMcpTab = "connections" | "agents" | "history";
type SlackNotice =
  | "connected"
  | "authorizationDenied"
  | "authorizationFailed"
  | "sessionExpired"
  | "connectionUnavailable"
  | "oauthExchangeFailed"
  | "toolDiscoveryFailed"
  | "failed";

type OAuthNotice =
  | "signedIn"
  | "signedInAgain"
  | "authorizationDenied"
  | "sessionExpired"
  | "oauthExchangeFailed"
  | "toolDiscoveryFailed"
  | "connectionUnavailable"
  | "unavailable"
  | "failed";

const OAUTH_FAILURE_NOTICES: Record<string, OAuthNotice> = {
  authorization_denied: "authorizationDenied",
  session_expired: "sessionExpired",
  oauth_exchange_failed: "oauthExchangeFailed",
  tool_discovery_failed: "toolDiscoveryFailed",
  connection_unavailable: "connectionUnavailable",
  unavailable: "unavailable",
};

/** The return from a service's sign-in page (`/personal-mcp/oauth/callback`). */
function oauthNoticeFromQuery(result: string | null, reason: string | null): OAuthNotice | null {
  if (result === "connected") return reason === "signed_in_again" ? "signedInAgain" : "signedIn";
  if (result !== "failed") return null;
  return OAUTH_FAILURE_NOTICES[reason ?? ""] ?? "failed";
}

/** Coded personal MCP errors carry catalog copy; anything else keeps the generic message. */
function personalMcpError(cause: unknown, fallback: string): string {
  return codedErrorMessage(cause, fallback, ["personal_mcp_"]);
}

function errorCode(cause: unknown): string | undefined {
  const code = cause && typeof cause === "object" ? (cause as { code?: unknown }).code : undefined;
  return typeof code === "string" ? code : undefined;
}

/** Notices that name the service or the connection the member just signed in to. */
const OAUTH_NOTICES_NAMING_CONNECTION = new Set<OAuthNotice>([
  "authorizationDenied",
  "oauthExchangeFailed",
  "toolDiscoveryFailed",
]);

function OAuthReturnNotice({
  notice,
  connection,
  loaded,
}: {
  notice: OAuthNotice;
  connection: PersonalMcpConnection | undefined;
  loaded: boolean;
}) {
  const t = useTranslations("personalMcp");
  const namesConnection = OAUTH_NOTICES_NAMING_CONNECTION.has(notice);
  if (namesConnection && !loaded) return null;
  // The connection may be gone by now; say what happened without naming it.
  const shown: OAuthNotice = namesConnection && !connection ? "failed" : notice;
  return (
    <p role={shown === "signedIn" || shown === "signedInAgain" ? "status" : "alert"}>
      {t(`oauthNotice.${shown}`, { host: connection?.provider_host ?? "", name: connection?.name ?? "" })}
    </p>
  );
}

function slackNoticeFromQuery(result: string | null, reason: string | null): SlackNotice | null {
  if (result === "connected") return "connected";
  if (result !== "failed") return null;
  if (reason === "authorization_denied") return "authorizationDenied";
  if (reason === "authorization_failed") return "authorizationFailed";
  if (reason === "session_expired") return "sessionExpired";
  if (reason === "connection_unavailable") return "connectionUnavailable";
  if (reason === "oauth_exchange_failed") return "oauthExchangeFailed";
  if (reason === "tool_discovery_failed") return "toolDiscoveryFailed";
  return "failed";
}

export function PersonalMcpSettings() {
  const t = useTranslations("personalMcp");
  const [tab, setTab] = useState<PersonalMcpTab>("connections");
  const [connections, setConnections] = useState<PersonalMcpConnection[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [slackConnectable, setSlackConnectable] = useState(false);
  const [oauthConnectable, setOauthConnectable] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<PersonalMcpConnection | null>(null);
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [authType, setAuthType] =
    useState<PersonalMcpConnection["auth_type"]>("none");
  const [value, setValue] = useState("");
  const [header, setHeader] = useState("X-API-Key");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [formMessage, setFormMessage] = useState<{ text: string; tone: "info" | "error" } | null>(null);
  const [notice, setNotice] = useState("");
  const [slackNotice, setSlackNotice] = useState<SlackNotice | null>(null);
  const [oauthNotice, setOauthNotice] = useState<OAuthNotice | null>(null);
  const [oauthNoticeConnection, setOauthNoticeConnection] = useState<string | null>(null);
  const [connectionsLoaded, setConnectionsLoaded] = useState(false);
  const [changes, setChanges] = useState<Record<string, string[]> | null>(null);
  const load = useCallback(async () => {
    const data = await personalMcpApi<{ connections: PersonalMcpConnection[] }>(
      "",
    );
    setConnections(data.connections);
  }, []);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const nextNotice = slackNoticeFromQuery(params.get("slack"), params.get("slack_reason"));
    const nextOAuthNotice = oauthNoticeFromQuery(params.get("mcp_oauth"), params.get("mcp_oauth_reason"));
    if (!nextNotice && !nextOAuthNotice) return;
    setSlackNotice(nextNotice);
    setOauthNotice(nextOAuthNotice);
    setOauthNoticeConnection(params.get("connection"));
    const url = new URL(window.location.href);
    for (const key of ["slack", "slack_reason", "mcp_oauth", "mcp_oauth_reason", "connection"]) {
      url.searchParams.delete(key);
    }
    window.history.replaceState({}, "", url.toString());
  }, []);
  useEffect(() => {
    let active = true;
    void personalMcpApi<{ enabled: boolean; slack_connectable?: boolean; oauth_connectable?: boolean }>("/capabilities")
      .then((capability) => {
        if (!active || !capability.enabled) return null;
        setEnabled(true);
        setSlackConnectable(Boolean(capability.slack_connectable));
        setOauthConnectable(Boolean(capability.oauth_connectable));
        return personalMcpApi<{ connections: PersonalMcpConnection[] }>("");
      })
      .then((data) => {
        if (active && data) {
          setConnections(data.connections);
          setConnectionsLoaded(true);
        }
      })
      .catch(() => {
        if (active) setError(t("error"));
      });
    return () => {
      active = false;
    };
  }, [t]);
  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      await action();
      await load();
    } catch (cause) {
      setError(personalMcpError(cause, t("error")));
    } finally {
      setBusy(false);
    }
  }
  function resetForm() {
    setFormOpen(false);
    setEditing(null);
    setValue("");
    setName("");
    setEndpoint("");
    setAuthType("none");
    setHeader("X-API-Key");
    setFormMessage(null);
  }
  function closeForm() {
    if (!busy) resetForm();
  }
  function addConnection() {
    if (busy) return;
    resetForm();
    setFormOpen(true);
  }
  function connectSlack() {
    if (busy) return;
    setBusy(true);
    setError(null);
    void personalMcpApi<{ authorize_url: string }>("/slack/connect", "POST")
      .then(({ authorize_url }) => window.location.assign(authorize_url))
      .catch(() => {
        setBusy(false);
        setError(t("error"));
      });
  }
  /** New connection ({name, endpoint}) or an existing signed-in one ({connection_id, version}). */
  function startSignIn(body: Record<string, unknown>, inForm: boolean) {
    if (busy) return;
    setBusy(true);
    setError(null);
    setFormMessage(null);
    void personalMcpApi<{ authorize_url: string }>("/oauth/authorize", "POST", body)
      .then(({ authorize_url }) => window.location.assign(authorize_url))
      .catch((cause) => {
        setBusy(false);
        const text = personalMcpError(cause, t("error"));
        if (inForm) setFormMessage({ text, tone: "error" });
        else setError(text);
      });
  }
  async function submitConnection() {
    setBusy(true);
    setError(null);
    setFormMessage(null);
    setNotice("");
    try {
      const discovered = await personalMcpApi<{
        changes: Record<string, string[]>;
      }>(editing ? `/${editing.id}` : "", editing ? "PUT" : "POST", {
        name,
        endpoint,
        auth_type: authType,
        ...(value && authType !== "oauth" ? { value, header } : {}),
        ...(editing ? { version: editing.version } : {}),
      });
      setChanges(discovered.changes);
      setNotice(t("discovered"));
      resetForm();
      await load();
    } catch (cause) {
      if (errorCode(cause) === "personal_mcp_needs_oauth" && oauthConnectable && !editing) {
        // The service wants a sign-in: keep the member's input and offer it in place.
        setAuthType("oauth");
        setValue("");
        setFormMessage({ text: personalMcpError(cause, t("signInHint")), tone: "info" });
      } else {
        setFormMessage({ text: personalMcpError(cause, t("error")), tone: "error" });
      }
    } finally {
      setBusy(false);
    }
  }
  function edit(connection: PersonalMcpConnection) {
    setEditing(connection);
    setName(connection.name);
    setEndpoint(connection.endpoint);
    setAuthType(connection.auth_type);
    setValue("");
    setHeader("X-API-Key");
    setFormMessage(null);
    setFormOpen(true);
  }
  function refresh(connection: PersonalMcpConnection) {
    void act(async () => {
      const discovered = await personalMcpApi<{
        changes: Record<string, string[]>;
      }>(`/${connection.id}`, "PUT", {
        name: connection.name,
        endpoint: connection.endpoint,
        auth_type: connection.auth_type,
        version: connection.version,
      });
      setChanges(discovered.changes);
      setNotice(t("discovered"));
    });
  }
  if (!enabled) return error ? <p role="alert">{error}</p> : null;
  const hasConnections = connections.length > 0;
  const tabs: Array<[PersonalMcpTab, string]> = [
    ["connections", t("title")],
    ["agents", t("agentBindings")],
    ["history", t("history")],
  ];
  return (
    <section className="personal-mcp-panel">
      <header className="personal-mcp-page-header">
        {/* The route owns the page H1 (My Connections); this panel is one of its sections. */}
        <h2 className="personal-mcp-section-title">{t("settingsTitle")}</h2>
        <p className="ui-page-desc">{t("privacy")}</p>
      </header>
      <Tabs value={tab} onValueChange={setTab} idBase="personal-mcp">
      <TabList appearance="underline" className="personal-mcp-tabs">
        {tabs.map(([id, label]) => (
          <Tab key={id} value={id} className={tab === id ? "is-active" : undefined}>
            {label}
          </Tab>
        ))}
      </TabList>
      <TabPanel value="connections" className="personal-mcp-tab-panel">
          <div className="personal-mcp-risk-notice">
            <strong>{t("riskTitle")}</strong>
            <p>{t("riskNotice")}</p>
          </div>
          {error ? <p role="alert">{error}</p> : null}
          {oauthNotice ? (
            <OAuthReturnNotice
              notice={oauthNotice}
              connection={connections.find((item) => item.id === oauthNoticeConnection)}
              loaded={connectionsLoaded}
            />
          ) : null}
          {slackNotice ? (
            <p role={slackNotice === "connected" ? "status" : "alert"}>
              {t(`slackNotice.${slackNotice}`)}
            </p>
          ) : null}
          {notice ? <p role="status">{notice}</p> : null}
          {changes ? (
            <ul>
              {["added", "removed", "changed"].map((kind) => (
                <li key={kind}>
                  {t(kind)}: {(changes[kind] ?? []).join(", ") || t("noToolChanges")}
                </li>
              ))}
            </ul>
          ) : null}
          <section className="personal-mcp-connections-list">
            <header className="personal-mcp-list-heading">
              <div>
                <h3>{t("savedConnections")}</h3>
                <p>{t("savedConnectionsHint")}</p>
              </div>
              <div className="personal-mcp-list-actions">
                {hasConnections ? <span>{connections.length}</span> : null}
                {slackConnectable ? <button type="button" className="personal-mcp-add-connection" onClick={connectSlack} disabled={busy}>{t("connectSlack")}</button> : null}
                {hasConnections ? (
                  <button
                    type="button"
                    className="personal-mcp-add-connection"
                    onClick={addConnection}
                    disabled={busy}
                  >
                    <span aria-hidden="true">+</span>
                    {t("addConnection")}
                  </button>
                ) : null}
              </div>
            </header>
            <div className="personal-mcp-connection-stack">
              {connections.map((connection) => (
                <ConnectionEditor
                  key={`${connection.id}:${connection.version}`}
                  connection={connection}
                  busy={busy}
                  signInAvailable={oauthConnectable}
                  onSignIn={() => startSignIn({ connection_id: connection.id, version: connection.version }, false)}
                  onEdit={() => edit(connection)}
                  onRefresh={() => refresh(connection)}
                  onSave={(status, selected) =>
                    void act(() =>
                      personalMcpApi(`/${connection.id}`, "PATCH", {
                        version: connection.version,
                        status,
                        selected_tool_ids: selected,
                      }),
                    )
                  }
                />
              ))}
              {!hasConnections ? (
                <div className="personal-mcp-connections-empty">
                  <p>{t("noConnections")}</p>
                  {slackConnectable ? <button type="button" className="personal-mcp-add-connection" onClick={connectSlack} disabled={busy}>{t("connectSlack")}</button> : null}
                  <button
                    type="button"
                    className="personal-mcp-add-connection"
                    onClick={addConnection}
                    disabled={busy}
                  >
                    <span aria-hidden="true">+</span>
                    {t("addConnection")}
                  </button>
                </div>
              ) : null}
            </div>
          </section>
          {formOpen ? (
            <Dialog
              title={editing ? t("editConnection") : t("addConnection")}
              onClose={closeForm}
              busy={busy}
            >
              <form
                className="personal-mcp-dialog-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (authType === "oauth" && !editing) {
                    startSignIn({ name, endpoint }, true);
                    return;
                  }
                  void submitConnection();
                }}
              >
                <p className="personal-mcp-dialog-hint">
                  {authType === "oauth" ? t("signInHint") : t("connectionFormHint")}
                </p>
                {formMessage ? (
                  <p role={formMessage.tone === "error" ? "alert" : "status"} className={`personal-mcp-dialog-message is-${formMessage.tone}`}>
                    {formMessage.text}
                  </p>
                ) : null}
                <fieldset disabled={busy} className="personal-mcp-form-fields">
                  <label>
                    {t("name")}
                    <input
                      required
                      name="connectionName"
                      autoComplete="off"
                      maxLength={40}
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                    />
                  </label>
                  <label>
                    {t("endpoint")}
                    <input
                      required
                      type="url"
                      name="endpoint"
                      autoComplete="off"
                      spellCheck={false}
                      value={endpoint}
                      // A signed-in connection keeps its address; another address is a new connection.
                      readOnly={editing?.auth_type === "oauth"}
                      aria-describedby={editing?.auth_type === "oauth" ? "personal-mcp-address-locked" : undefined}
                      onChange={(event) => setEndpoint(event.target.value)}
                    />
                    {editing?.auth_type === "oauth" ? (
                      <small id="personal-mcp-address-locked">{t("addressLocked")}</small>
                    ) : null}
                  </label>
                  <label>
                    {t("auth")}
                    <select
                      name="authType"
                      value={authType}
                      disabled={editing?.auth_type === "oauth"}
                      onChange={(event) => {
                        setAuthType(event.target.value as typeof authType);
                        setFormMessage(null);
                      }}
                    >
                      <option value="none">{t("none")}</option>
                      <option value="bearer">Bearer Token</option>
                      <option value="api_key">API Key</option>
                      {oauthConnectable || editing?.auth_type === "oauth" ? <option value="oauth">{t("authSignIn")}</option> : null}
                    </select>
                  </label>
                  {authType === "api_key" ? (
                    <label>
                      {t("header")}
                      <input
                        name="credentialHeader"
                        required
                        autoComplete="off"
                        spellCheck={false}
                        value={header}
                        onChange={(event) => setHeader(event.target.value)}
                      />
                    </label>
                  ) : null}
                  {authType !== "none" && authType !== "oauth" ? (
                    <label>
                      {t("credential")}
                      <input
                        type="password"
                        name="credential"
                        autoComplete="new-password"
                        required={!editing || authType !== editing.auth_type}
                        value={value}
                        onChange={(event) => setValue(event.target.value)}
                        placeholder={
                          editing && authType === editing.auth_type
                            ? t("keepCredential")
                            : ""
                        }
                      />
                    </label>
                  ) : null}
                  <div className="personal-mcp-dialog-actions">
                    <button type="button" onClick={closeForm} disabled={busy}>
                      {t("cancel")}
                    </button>
                    <button type="submit" disabled={busy}>
                      {busy
                        ? t("working")
                        : editing
                          ? t("refreshSave")
                          : authType === "oauth"
                            ? t("continueSignIn")
                            : t("connect")}
                    </button>
                  </div>
                </fieldset>
              </form>
            </Dialog>
          ) : null}
      </TabPanel>
      <TabPanel value="agents" className="personal-mcp-tab-panel">
        <PersonalMcpAgentBindings
          connections={connections}
          onManageConnections={() => {
            setTab("connections");
            if (!hasConnections) addConnection();
          }}
        />
      </TabPanel>
      <TabPanel value="history" className="personal-mcp-tab-panel">
        <PersonalMcpHistory />
      </TabPanel>
      </Tabs>
    </section>
  );
}

function connectionStatusText(
  t: ReturnType<typeof useTranslations>,
  connection: PersonalMcpConnection,
  signInAvailable: boolean,
): string {
  if (connection.auth_type === "oauth") {
    if (!signInAvailable && connection.status !== "disabled") return t("signInOff");
    if (connection.status === "ready") return t("signedInAt", { host: connection.provider_host ?? "" });
    if (connection.status === "authorizing") return t("signInNotFinished");
    if (connection.status === "needs_reauth") return t("signInEnded");
  }
  if (connection.status === "ready") return t("ready");
  if (connection.status === "needs_reauth") return t("needsReauth");
  if (connection.status === "unreachable") return t("unreachable");
  return t("disabled");
}

function ConnectionEditor({
  connection,
  busy,
  signInAvailable,
  onSignIn,
  onEdit,
  onRefresh,
  onSave,
}: {
  connection: PersonalMcpConnection;
  busy: boolean;
  signInAvailable: boolean;
  onSignIn: () => void;
  onEdit: () => void;
  onRefresh: () => void;
  onSave: (status: string, selected: string[]) => void;
}) {
  const t = useTranslations("personalMcp");
  const [selected, setSelected] = useState(
    () =>
      new Set(
        connection.tools.filter((tool) => tool.selected).map((tool) => tool.id),
      ),
  );
  const [deleting, setDeleting] = useState(false);
  const [affectedAgents, setAffectedAgents] = useState<string[]>([]);
  useEffect(() => {
    if (!deleting) return;
    let active = true;
    const toolIds = new Set(connection.tools.map((tool) => tool.id));
    void personalMcpApi<{ agents: PersonalMcpAgent[] }>("/agents/bindings")
      .then(({ agents }) => {
        if (!active) return;
        setAffectedAgents(
          agents
            .filter((agent) =>
              [...selectedIds(agent.tool_ids_json)].some((id) =>
                toolIds.has(id),
              ),
            )
            .map(personalMcpAgentName),
        );
      })
      // Bound-agent list is supplementary guidance; failure retains the generic deletion explanation.
      .catch(() => undefined);
    return () => {
      active = false;
      setAffectedAgents([]);
    };
  }, [deleting, connection.tools]);
  const signedIn = connection.auth_type === "oauth";
  const authorizing = signedIn && connection.status === "authorizing";
  // A signed-in connection whose sign-in ended, or that never finished signing in, waits for the owner.
  const waitingForSignIn = authorizing || (signedIn && connection.status === "needs_reauth");
  const offerSignIn = signedIn && signInAvailable;
  // Enabling cannot stand in for signing in again: a connection waiting for sign-in can only be turned off.
  const canEnable = connection.status !== "ready" && !waitingForSignIn;
  return (
    <fieldset disabled={busy} className="personal-mcp-connection">
      <legend>{connection.name}</legend>
      <p>{connection.endpoint}</p>
      <p className={`personal-mcp-connection-status is-${connection.status}`}>
        {connectionStatusText(t, connection, signInAvailable)}
      </p>
      {connection.tools.map((tool) => (
        <label key={tool.id}>
          <input
            type="checkbox"
            disabled={!tool.schema_digest}
            checked={selected.has(tool.id)}
            onChange={() =>
              setSelected((old) => {
                const next = new Set(old);
                if (next.has(tool.id)) next.delete(tool.id);
                else next.add(tool.id);
                return next;
              })
            }
          />
          <span>
            {tool.remote_name} ·{" "}
            {tool.schema_digest ? t("confirmationRequired") : t("unavailable")}
            <small>{tool.description}</small>
          </span>
        </label>
      ))}
      {waitingForSignIn ? null : (
        <div className="personal-mcp-tool-selection-actions">
          <button
            className="personal-mcp-connection-save"
            type="button"
            onClick={() => onSave("ready", [...selected])}
          >
            {t("saveTools")}
          </button>
        </div>
      )}
      <div className="personal-mcp-connection-actions">
        {offerSignIn && waitingForSignIn ? (
          <button className="personal-mcp-connection-sign-in" type="button" onClick={onSignIn}>
            {authorizing ? t("continueSignInAction") : t("signInAgain")}
          </button>
        ) : null}
        {waitingForSignIn ? null : (
          <button type="button" onClick={onRefresh}>
            {t("refreshTools")}
          </button>
        )}
        {offerSignIn && !waitingForSignIn ? (
          <button type="button" onClick={onSignIn}>
            {t("signInAgain")}
          </button>
        ) : null}
        {authorizing ? null : (
          <>
            <button
              type="button"
              className={
                canEnable
                  ? "personal-mcp-connection-enable"
                  : "personal-mcp-connection-disable"
              }
              onClick={() =>
                onSave(canEnable ? "ready" : "disabled", [...selected])
              }
            >
              {canEnable ? t("enable") : t("disable")}
            </button>
            <button
              className="personal-mcp-connection-edit"
              type="button"
              onClick={onEdit}
            >
              {t("editConnectionAction")}
            </button>
          </>
        )}
        <button
          className="personal-mcp-connection-delete"
          type="button"
          onClick={() => setDeleting(true)}
        >
          {t("delete")}
        </button>
      </div>
      {deleting ? (
        <div>
          <p>{t("deleteNotice")}</p>
          {signedIn ? <p>{t("deleteSignInNotice")}</p> : null}
          {affectedAgents.length ? (
            <p>
              {t("deleteAffectsAgents", {
                count: affectedAgents.length,
                names: affectedAgents.join(t("agentNameSeparator")),
              })}
            </p>
          ) : null}
          <button type="button" onClick={() => onSave("deleted", [])}>
            {t("confirmDelete")}
          </button>
          <button type="button" onClick={() => setDeleting(false)}>
            {t("cancel")}
          </button>
        </div>
      ) : null}
    </fieldset>
  );
}

export function PersonalMcpAgentBindings({
  connections: supplied,
  agentId,
  onManageConnections,
}: {
  connections?: PersonalMcpConnection[];
  agentId?: string;
  onManageConnections?: () => void;
}) {
  const t = useTranslations("personalMcp");
  const agentGroup = useId();
  const [connections, setConnections] = useState<PersonalMcpConnection[]>([]);
  // null = binding list has not returned; do not render the No Bindable Agents empty state yet.
  const [agents, setAgents] = useState<PersonalMcpAgent[] | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [error, setError] = useState(false);
  const load = useCallback(async () => {
    const [catalog, bindings] = await Promise.all([
      personalMcpApi<{ connections: PersonalMcpConnection[] }>(""),
      personalMcpApi<{ agents: PersonalMcpAgent[] }>("/agents/bindings"),
    ]);
    setConnections(catalog.connections);
    setAgents(bindings.agents);
  }, []);
  useEffect(() => {
    let active = true;
    void personalMcpApi<{ enabled: boolean }>("/capabilities")
      .then(async (capability) => {
        if (!active || !capability.enabled) return;
        setEnabled(true);
      })
      .catch(() => {
        if (active) setError(true);
      });
    return () => {
      active = false;
    };
  }, [load]);
  const connectionVersions = (supplied ?? [])
    .map((connection) => `${connection.id}:${connection.version}`)
    .join(",");
  useEffect(() => {
    if (enabled) void load().catch(() => setError(true));
  }, [enabled, connectionVersions, load]);
  if (!enabled) return error ? <p role="alert">{t("error")}</p> : null;
  const usable = (supplied ?? connections)
    .filter((connection) => connection.status === "ready")
    .map((connection) => ({
      id: connection.id,
      name: connection.name,
      endpoint: connection.endpoint,
      tools: connection.tools.filter((tool) => tool.selected),
    }))
    .filter((connection) => connection.tools.length > 0);
  const loadingAgents = agents === null;
  const visibleAgents = (agents ?? []).filter((agent) => !agentId || agent.id === agentId);
  const editableAgents = usable.length
    ? visibleAgents
    : visibleAgents.filter((agent) => selectedIds(agent.tool_ids_json).size > 0);
  return (
    <section
      className="personal-mcp-panel personal-mcp-agent-bindings"
      aria-busy={loadingAgents || undefined}
    >
      {loadingAgents || usable.length > 0 ? (
        <header>
          <h3>{t("agentMcpTitle")}</h3>
          <p>{t("agentMcpDescription")}</p>
        </header>
      ) : null}
      {error ? <p role="alert">{t("error")}</p> : null}
      {loadingAgents && !error ? <LoadingSkeleton rows={2} /> : null}
      {!loadingAgents && !usable.length ? (
        <div className="personal-mcp-binding-empty">
          <div>
            <h3>{t("bindingEmptyTitle")}</h3>
            <p>{t("bindingEmptyHint")}</p>
          </div>
          {onManageConnections ? (
            <Button variant="primary" onClick={onManageConnections}>
              {(supplied ?? connections).length ? t("manageConnections") : t("addConnection")}
            </Button>
          ) : (
            <a className="settings-button settings-button--primary settings-button--md" href={appPath("/connections/")}>{t("manageConnections")}</a>
          )}
        </div>
      ) : null}
      {usable.length > 0 && !loadingAgents && !visibleAgents.length ? (
        <p>{t("noPrivateAgents")}</p>
      ) : null}
      {editableAgents.length ? (
        <div className="personal-mcp-binding-list">
          {editableAgents.map((agent) => {
            const editor = <AgentBindingEditor
              key={`${agent.id}:${agent.revision_id}:${agent.binding_version}`}
              agent={agent}
              connections={usable}
              onSave={async (toolIds) => {
                setError(false);
                try {
                  await personalMcpApi(`/agents/${agent.id}/binding`, "PUT", {
                    revision_id: agent.revision_id,
                    tool_ids: toolIds,
                    version: agent.binding_version,
                  });
                  await load();
                } catch {
                  setError(true);
                }
              }}
            />;
            return agentId ? editor : (
              <details key={agent.id} name={agentGroup}>
                <summary>{personalMcpAgentName(agent)} <ChevronDown size={16} aria-hidden="true" /></summary>
                {editor}
              </details>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

function AgentBindingEditor({
  agent,
  connections,
  onSave,
}: {
  agent: PersonalMcpAgent;
  connections: {
    id: string;
    name: string;
    endpoint: string;
    tools: PersonalMcpConnection["tools"];
  }[];
  onSave: (toolIds: string[]) => Promise<void>;
}) {
  const t = useTranslations("personalMcp");
  const editorId = useId();
  const selectedToolIds = agent.tool_ids_json;
  const [selected, setSelected] = useState<Set<string>>(() => selectedIds(selectedToolIds));
  const [busy, setBusy] = useState(false);
  useEffect(() => setSelected(selectedIds(selectedToolIds)), [selectedToolIds]);
  const availableToolIds = new Set(
    connections.flatMap((connection) =>
      connection.tools.map((tool) => tool.id),
    ),
  );
  const missing = [...selected].filter((id) => !availableToolIds.has(id));
  return (
    <fieldset
      disabled={busy}
      className="personal-mcp-connection personal-mcp-agent-binding is-embedded"
    >
      <div className="personal-mcp-server-list">
        {connections.map((connection) => (
          <section
            key={connection.id}
            className="personal-mcp-server"
            aria-labelledby={`${editorId}-${connection.id}`}
          >
            <header>
              <div>
                <h4 id={`${editorId}-${connection.id}`}>
                  {connection.name}
                </h4>
                <p>{connection.endpoint}</p>
              </div>
              <small>
                {t("availableTools", { count: connection.tools.length })}
              </small>
            </header>
            <div className="personal-mcp-server-tools">
              {connection.tools.map((tool) => (
                <label key={tool.id}>
                  <input
                    type="checkbox"
                    checked={selected.has(tool.id)}
                    onChange={() =>
                      setSelected((old) => {
                        const next = new Set(old);
                        if (next.has(tool.id)) next.delete(tool.id);
                        else next.add(tool.id);
                        return next;
                      })
                    }
                  />
                  <span>
                    <strong>{tool.remote_name}</strong>
                    {tool.description ? (
                      <small>{tool.description}</small>
                    ) : null}
                  </span>
                </label>
              ))}
            </div>
          </section>
        ))}
      </div>
      {missing.length ? (
        <div className="personal-mcp-missing-tools">
          <strong>{t("unavailableBindings")}</strong>
          <p>{t("unavailableBindingsHint")}</p>
          {missing.map((id) => (
            <label key={id}>
              <input
                type="checkbox"
                checked
                onChange={() =>
                  setSelected((old) => {
                    const next = new Set(old);
                    next.delete(id);
                    return next;
                  })
                }
              />
              <span>{t("unavailable")}</span>
            </label>
          ))}
        </div>
      ) : null}
      <div className="personal-mcp-binding-actions">
        <button
          className="personal-mcp-connection-save"
          type="button"
          onClick={() => {
            setBusy(true);
            void onSave([...selected]).finally(() => setBusy(false));
          }}
        >
          {t("saveBinding")}
        </button>
        <button
          className="personal-mcp-connection-delete"
          type="button"
          onClick={() => {
            setBusy(true);
            void onSave([]).finally(() => setBusy(false));
          }}
        >
          {t("removeBindings")}
        </button>
      </div>
    </fieldset>
  );
}

/** Display name is optional (creation wizard uses slug if blank); an empty name must not render a blank heading. */
function personalMcpAgentName(agent: PersonalMcpAgent): string {
  return agent.display_name.trim() || agent.slug || agent.id;
}

function selectedIds(value: string): Set<string> {
  try {
    const parsed: unknown = JSON.parse(value);
    return new Set(Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === "string") : []);
  } catch {
    return new Set();
  }
}

function PersonalMcpHistory() {
  const t = useTranslations("personalMcp");
  const locale = useLocale();
  const [calls, setCalls] = useState<
    {
      id: string;
      connection_name: string;
      tool_name: string;
      status: string;
      created_at: string;
      duration_ms: number | null;
    }[]
  >([]);
  const [error, setError] = useState(false);
  const load = useCallback(async () => {
    try {
      const data = await personalMcpApi<{ calls: typeof calls }>(
        "/calls/history",
      );
      setCalls(data.calls);
      setError(false);
    } catch {
      setError(true);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <section className="personal-mcp-panel personal-mcp-history">
      <header className="personal-mcp-history-heading">
        <div>
          <h3>{t("history")}</h3>
          <p>{t("historyDescription")}</p>
        </div>
        <button type="button" onClick={() => void load()}>
          {t("refreshHistory")}
        </button>
      </header>
      {error ? <p role="alert">{t("error")}</p> : null}
      {!calls.length ? (
        <p className="personal-mcp-history-empty">{t("noCalls")}</p>
      ) : null}
      <ul className="personal-mcp-history-list">
        {calls.map((call) => (
          <li key={call.id}>
            <div className="personal-mcp-history-row">
              <div className="personal-mcp-history-call">
                <p>
                  <span>{call.connection_name}</span>
                  <span aria-hidden="true"> · </span>
                  <strong>{call.tool_name}</strong>
                </p>
                <time dateTime={call.created_at}>
                  {formatDateTime(call.created_at, locale, { dateStyle: "medium" }) || call.created_at}
                  {call.duration_ms !== null ? ` · ${call.duration_ms} ms` : ""}
                </time>
              </div>
              <span className={`personal-mcp-call-status is-${call.status}`}>
                {t(`callStatus.${call.status}`)}
              </span>
            </div>
            {call.status === "unknown" ? (
              <p className="personal-mcp-history-unknown">
                {t("historyUnknown")}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
