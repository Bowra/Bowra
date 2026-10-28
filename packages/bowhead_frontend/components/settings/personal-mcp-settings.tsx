"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  personalMcpApi,
  type PersonalMcpConnection,
  type PersonalMcpAgent,
} from "@/lib/personal-mcp-api";
import { Dialog } from "@/components/ui/dialog";
import { Tab, TabList, TabPanel, Tabs } from "@/components/ui/tabs";

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
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<PersonalMcpConnection | null>(null);
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [authType, setAuthType] =
    useState<PersonalMcpConnection["auth_type"]>("none");
  const [value, setValue] = useState("");
  const [header, setHeader] = useState("X-API-Key");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [notice, setNotice] = useState("");
  const [slackNotice, setSlackNotice] = useState<SlackNotice | null>(null);
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
    const result = params.get("slack");
    const nextNotice = slackNoticeFromQuery(result, params.get("slack_reason"));
    if (!nextNotice) return;
    setSlackNotice(nextNotice);
    const url = new URL(window.location.href);
    url.searchParams.delete("slack");
    url.searchParams.delete("slack_reason");
    window.history.replaceState({}, "", url.toString());
  }, []);
  useEffect(() => {
    let active = true;
    void personalMcpApi<{ enabled: boolean; slack_connectable?: boolean }>("/capabilities")
      .then((capability) => {
        if (!active || !capability.enabled) return null;
        setEnabled(true);
        setSlackConnectable(Boolean(capability.slack_connectable));
        return personalMcpApi<{ connections: PersonalMcpConnection[] }>("");
      })
      .then((data) => {
        if (active && data) setConnections(data.connections);
      })
      .catch(() => {
        if (active) setError(true);
      });
    return () => {
      active = false;
    };
  }, []);
  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setError(false);
    setNotice("");
    try {
      await action();
      await load();
    } catch {
      setError(true);
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
    setError(false);
    void personalMcpApi<{ authorize_url: string }>("/slack/connect", "POST")
      .then(({ authorize_url }) => window.location.assign(authorize_url))
      .catch(() => {
        setBusy(false);
        setError(true);
      });
  }
  function edit(connection: PersonalMcpConnection) {
    setEditing(connection);
    setName(connection.name);
    setEndpoint(connection.endpoint);
    setAuthType(connection.auth_type);
    setValue("");
    setHeader("X-API-Key");
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
  if (!enabled) return error ? <p role="alert">{t("error")}</p> : null;
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
          {error ? <p role="alert">{t("error")}</p> : null}
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
                  void act(async () => {
                    const discovered = await personalMcpApi<{
                      changes: Record<string, string[]>;
                    }>(
                      editing ? `/${editing.id}` : "",
                      editing ? "PUT" : "POST",
                      {
                        name,
                        endpoint,
                        auth_type: authType,
                        ...(value ? { value, header } : {}),
                        ...(editing ? { version: editing.version } : {}),
                      },
                    );
                    setChanges(discovered.changes);
                    setNotice(t("discovered"));
                    resetForm();
                  });
                }}
              >
                <p className="personal-mcp-dialog-hint">
                  {t("connectionFormHint")}
                </p>
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
                      onChange={(event) => setEndpoint(event.target.value)}
                    />
                  </label>
                  <label>
                    {t("auth")}
                    <select
                      name="authType"
                      value={authType}
                      onChange={(event) =>
                        setAuthType(event.target.value as typeof authType)
                      }
                    >
                      <option value="none">{t("none")}</option>
                      <option value="bearer">Bearer Token</option>
                      <option value="api_key">API Key</option>
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
                  {authType !== "none" ? (
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
                          : t("connect")}
                    </button>
                  </div>
                </fieldset>
              </form>
            </Dialog>
          ) : null}
      </TabPanel>
      <TabPanel value="agents" className="personal-mcp-tab-panel">
        <PersonalMcpAgentBindings connections={connections} />
      </TabPanel>
      <TabPanel value="history" className="personal-mcp-tab-panel">
        <PersonalMcpHistory />
      </TabPanel>
      </Tabs>
    </section>
  );
}

function ConnectionEditor({
  connection,
  busy,
  onEdit,
  onRefresh,
  onSave,
}: {
  connection: PersonalMcpConnection;
  busy: boolean;
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
  return (
    <fieldset disabled={busy} className="personal-mcp-connection">
      <legend>{connection.name}</legend>
      <p>{connection.endpoint}</p>
      <p className={`personal-mcp-connection-status is-${connection.status}`}>
        {connection.status === "ready"
          ? t("ready")
          : connection.status === "needs_reauth"
            ? t("needsReauth")
            : connection.status === "unreachable"
              ? t("unreachable")
              : t("disabled")}
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
      <div className="personal-mcp-tool-selection-actions">
        <button
          className="personal-mcp-connection-save"
          type="button"
          onClick={() => onSave("ready", [...selected])}
        >
          {t("saveTools")}
        </button>
      </div>
      <div className="personal-mcp-connection-actions">
        <button type="button" onClick={onRefresh}>
          {t("refreshTools")}
        </button>
        <button
          type="button"
          className={
            connection.status === "ready"
              ? "personal-mcp-connection-disable"
              : "personal-mcp-connection-enable"
          }
          onClick={() =>
            onSave(connection.status === "ready" ? "disabled" : "ready", [
              ...selected,
            ])
          }
        >
          {connection.status === "ready" ? t("disable") : t("enable")}
        </button>
        <button
          className="personal-mcp-connection-edit"
          type="button"
          onClick={onEdit}
        >
          {t("editConnectionAction")}
        </button>
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
}: {
  connections?: PersonalMcpConnection[];
  agentId?: string;
}) {
  const t = useTranslations("personalMcp");
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
  const visibleAgents = !agents
    ? []
    : agentId
      ? agents.filter((agent) => agent.id === agentId)
      : agents;
  return (
    <section
      className="personal-mcp-panel personal-mcp-agent-bindings"
      aria-busy={loadingAgents || undefined}
    >
      <header>
        <div>
          <h3>{t("agentMcpTitle")}</h3>
          <p>{t("agentMcpDescription")}</p>
        </div>
      </header>
      {error ? <p role="alert">{t("error")}</p> : null}
      {!loadingAgents && !visibleAgents.length ? (
        <p>{t("noPrivateAgents")}</p>
      ) : null}
      {visibleAgents.map((agent) => (
        <AgentBindingEditor
          key={`${agent.id}:${agent.revision_id}:${agent.binding_version}`}
          agent={agent}
          connections={usable}
          showAgentName={!agentId}
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
        />
      ))}
    </section>
  );
}

function AgentBindingEditor({
  agent,
  connections,
  showAgentName,
  onSave,
}: {
  agent: PersonalMcpAgent;
  connections: {
    id: string;
    name: string;
    endpoint: string;
    tools: PersonalMcpConnection["tools"];
  }[];
  showAgentName: boolean;
  onSave: (toolIds: string[]) => Promise<void>;
}) {
  const t = useTranslations("personalMcp");
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
      className={`personal-mcp-connection personal-mcp-agent-binding${showAgentName ? "" : " is-embedded"}`}
    >
      {showAgentName ? <legend>{personalMcpAgentName(agent)}</legend> : null}
      <div className="personal-mcp-server-list">
        {connections.map((connection) => (
          <section
            key={connection.id}
            className="personal-mcp-server"
            aria-labelledby={`personal-mcp-server-${connection.id}`}
          >
            <header>
              <div>
                <span>{t("mcpServer")}</span>
                <h4 id={`personal-mcp-server-${connection.id}`}>
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
        {!connections.length ? (
          <p className="personal-mcp-binding-empty">{t("noAvailableTools")}</p>
        ) : null}
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
                  {new Date(call.created_at).toLocaleString()}
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
