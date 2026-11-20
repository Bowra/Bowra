"use client";

import { useCallback, useEffect, useId, useState, type FormEvent, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import { fetchSkills } from "@/lib/api";
import { createResourceGrant, type AccessGroup } from "@/lib/admin-access-api";
import { fetchAdminMembers } from "@/lib/admin-members-api";
import { fetchTeamSpacesPage } from "@/lib/admin-spaces-api";
import type { MeUser } from "@/lib/auth-api";
import { fetchAllScopeKbFiles } from "@/lib/kb-api";
import { getToolsCatalog } from "@/lib/team-subagents-api";
import { listSources } from "@/lib/team-tool-sources-api";
import { listTeamSkills } from "@/lib/workflow-solidify-api";

export const RESOURCE_KINDS = ["kb_scope", "kb_file", "skill", "team_skill", "team_tool_source", "team_tool_op", "builtin_capability", "tool"] as const;
const PRINCIPAL_TYPES = ["user", "group", "workspace", "team"] as const;
const PERMISSIONS = ["view", "read", "use", "invoke", "edit", "write", "manage", "share", "admin"] as const;
const BUILTIN_CAPABILITIES = ["kb_search", "skill_invoke", "web_search", "web_fetch", "export", "python_sandbox", "subagent"] as const;
const INPUT_CLASS = "ui-input legacy-access-input";
type ResourceKind = (typeof RESOURCE_KINDS)[number];
type PrincipalType = (typeof PRINCIPAL_TYPES)[number];
type Permission = (typeof PERMISSIONS)[number];
type Option = { id: string; label: string };
type LoadOptions = (query: string) => Promise<Option[]>;
type GrantTranslations = ReturnType<typeof useTranslations<"admin.access">>;

export function AccessField({ label, children }: { label: string; children: ReactNode }) {
  return <label className="access-field"><span>{label}</span>{children}</label>;
}

export function permissionsForResource(kind: string): readonly Permission[] {
  return kind === "skill" || kind === "team_skill" ? PERMISSIONS : PERMISSIONS.filter((permission) => permission !== "invoke");
}

function matches(option: Option, query: string) {
  return [option.label, option.id].some((value) => value.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
}

async function spaceOptions(query: string) {
  const result = await fetchTeamSpacesPage({ status: "all", q: query, pageSize: 50 });
  return result.items.map((space) => ({ id: space.workspace_id, label: space.name }));
}

async function resourceOptions(kind: ResourceKind, query: string, me: MeUser, t: GrantTranslations, locale: string): Promise<Option[]> {
  switch (kind) {
    case "kb_scope": {
      const scopes = (await spaceOptions(query)).map((space) => ({
        ...space, id: `workspace:${space.id}`,
      }));
      const team = { id: `team:${me.team_id}`, label: t("grant.team_knowledge") };
      return matches(team, query) ? [team, ...scopes] : scopes;
    }
    case "kb_file":
      return (await fetchAllScopeKbFiles({ q: query, limit: 50 })).files.map((file) => ({
        id: file.file_id,
        label: file.uploaded_by_name ? `${file.filename} · ${file.uploaded_by_name}` : file.filename,
      }));
    case "skill":
      return (await fetchSkills()).builtin.map((skill) => ({
        id: skill.name,
        label: (locale.startsWith("zh") ? skill.title_zh : skill.title) || skill.title || skill.name,
      })).filter((option) => matches(option, query));
    case "team_skill":
      return (await listTeamSkills()).items.map((skill) => ({
        id: skill.id, label: skill.display_name || skill.name,
      })).filter((option) => matches(option, query));
    case "team_tool_source":
      return (await listSources()).map((source) => ({ id: source.id, label: source.name }))
        .filter((option) => matches(option, query));
    case "team_tool_op":
      return (await getToolsCatalog()).team_tools.map((tool) => ({
        id: tool.tool_name,
        label: [tool.operation_id, tool.method, tool.path || tool.tool_name].filter(Boolean).join(" · "),
      })).filter((option) => matches(option, query));
    case "builtin_capability":
      return BUILTIN_CAPABILITIES.map((id) => ({ id, label: t(`grant.builtin.${id}`) }))
        .filter((option) => matches(option, query));
    case "tool":
      return (await getToolsCatalog()).native_tools.map((id) => ({ id, label: id }))
        .filter((option) => matches(option, query));
  }
}

async function principalOptions(kind: PrincipalType, query: string, me: MeUser, groups: AccessGroup[], t: GrantTranslations): Promise<Option[]> {
  switch (kind) {
    case "user":
      return (await fetchAdminMembers({ page: 1, size: 50, q: query })).items.map((member) => ({
        id: member.user_id, label: member.display_name ? `${member.display_name} · ${member.email}` : member.email,
      }));
    case "group":
      return groups.map((group) => ({ id: group.id, label: group.name })).filter((option) => matches(option, query));
    case "workspace":
      return spaceOptions(query);
    case "team":
      return [{ id: me.team_id, label: me.team_name || t("grant.current_team") }];
  }
}

function GrantTarget({ label, searchLabel, value, onChange, load }: {
  label: string;
  searchLabel: string;
  value: Option | null;
  onChange: (value: Option | null) => void;
  load: LoadOptions;
}) {
  const t = useTranslations("admin.access");
  const statusId = useId();
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<Option[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);
  const [exactId, setExactId] = useState(false);

  useEffect(() => {
    if (exactId) return;
    let active = true;
    setOptions([]);
    setState("loading");
    const timer = setTimeout(() => {
      void load(query.trim()).then((rows) => {
        if (!active) return;
        setOptions(rows);
        setState("ready");
      }).catch(() => {
        if (active) setState("failed");
      });
    }, 150);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [attempt, exactId, load, query]);

  const choices = value && !options.some((option) => option.id === value.id) ? [value, ...options] : options;
  const labelCounts = new Map<string, number>();
  for (const option of choices) labelCounts.set(option.label, (labelCounts.get(option.label) ?? 0) + 1);
  const choiceLabel = (option: Option) => (labelCounts.get(option.label) ?? 0) > 1 ? `${option.label} · ${option.id}` : option.label;
  return (
    <div className="resource-grant-target">
      {exactId ? (
        <AccessField label={`${label} · ${t("grant.exact_identifier")}`}>
          <input className={INPUT_CLASS} value={value?.id ?? ""}
            onChange={(event) => onChange({ id: event.target.value, label: event.target.value })} />
        </AccessField>
      ) : (
        <>
          <AccessField label={searchLabel}>
            <input type="search" className={INPUT_CLASS} value={query} aria-describedby={statusId}
              onChange={(event) => {
                setOptions([]);
                setState("loading");
                setQuery(event.target.value);
              }} />
          </AccessField>
          <AccessField label={label}>
            <select className={INPUT_CLASS} aria-describedby={statusId} value={value?.id ?? ""}
              onChange={(event) => {
                const option = choices.find((item) => item.id === event.target.value);
                onChange(option ? { ...option, label: choiceLabel(option) } : null);
              }}>
              <option value="">{t("grant.choose")}</option>
              {choices.map((option) => <option key={option.id} value={option.id}>{choiceLabel(option)}</option>)}
            </select>
          </AccessField>
          <div id={statusId} role="status">
            {state === "loading" ? t("loading") : state === "failed" ? t("grant.lookup_failed")
              : choices.length === 0 ? t("grant.no_results") : null}
          </div>
          {state === "failed" ? (
            <button type="button" className="ui-btn legacy-access-btn" onClick={() => setAttempt((next) => next + 1)}>
              {t("refresh")}
            </button>
          ) : null}
        </>
      )}
      <button type="button" className="ui-btn legacy-access-btn"
        onClick={() => { onChange(null); setExactId((current) => !current); }}>
        {t(exactId ? "grant.use_name" : "grant.use_identifier")}
      </button>
    </div>
  );
}

export function ResourceGrantForm({ me, groups, onSaved, onStart, onError }: {
  me: MeUser;
  groups: AccessGroup[];
  onSaved: () => void;
  onStart: () => void;
  onError: (error: unknown) => void;
}) {
  const t = useTranslations("admin.access");
  const locale = useLocale();
  const [resourceKind, setResourceKind] = useState<ResourceKind>("kb_scope");
  const [principalType, setPrincipalType] = useState<PrincipalType>("group");
  const [resource, setResource] = useState<Option | null>(null);
  const [principal, setPrincipal] = useState<Option | null>(null);
  const [permission, setPermission] = useState<Permission>("read");
  const [effect, setEffect] = useState("allow");
  const [expires, setExpires] = useState("");
  const [busy, setBusy] = useState(false);
  const loadResource = useCallback(
    (query: string) => resourceOptions(resourceKind, query, me, t, locale),
    [resourceKind, me, t, locale],
  );
  const loadPrincipal = useCallback(
    (query: string) => principalOptions(principalType, query, me, groups, t),
    [principalType, me, groups, t],
  );
  const target = principalType === "team"
    ? { id: me.team_id, label: me.team_name || t("grant.current_team") }
    : principal;
  const ready = Boolean(resource?.id.trim() && target?.id.trim());

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready || !resource || !target || busy) return;
    onStart();
    setBusy(true);
    try {
      await createResourceGrant({
        resource_kind: resourceKind, resource_id: resource.id.trim(),
        principal_type: principalType, principal_id: target.id.trim(),
        permission, effect, expires_at: expires ? new Date(expires).toISOString() : null,
      });
      setResource(null);
      setPrincipal(null);
      setExpires("");
      onSaved();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="resource-grant-form" onSubmit={save}>
      <fieldset disabled={busy}>
        <div className="resource-grant-fields">
          <div className="resource-grant-target">
            <AccessField label={t("grant.resource_kind")}>
              <select className={INPUT_CLASS} value={resourceKind} onChange={(event) => {
                const kind = event.target.value as ResourceKind;
                setResourceKind(kind);
                setResource(null);
                if (!permissionsForResource(kind).includes(permission)) setPermission("use");
              }}>
                {RESOURCE_KINDS.map((kind) => <option key={kind} value={kind}>{t(`grant.resource.${kind}`)}</option>)}
              </select>
            </AccessField>
            <GrantTarget key={resourceKind} label={t("grant.resource_label")} searchLabel={t("grant.search_resource")}
              value={resource} onChange={setResource} load={loadResource} />
          </div>
          <div className="resource-grant-target">
            <AccessField label={t("grant.principal_type")}>
              <select className={INPUT_CLASS} value={principalType} onChange={(event) => {
                setPrincipalType(event.target.value as PrincipalType);
                setPrincipal(null);
              }}>
                {PRINCIPAL_TYPES.map((kind) => <option key={kind} value={kind}>{t(`grant.principal.${kind}`)}</option>)}
              </select>
            </AccessField>
            {principalType === "team" ? <p>{target?.label}</p> : (
              <GrantTarget key={principalType} label={t("grant.principal_label")} searchLabel={t("grant.search_principal")}
                value={principal} onChange={setPrincipal} load={loadPrincipal} />
            )}
          </div>
          <AccessField label={t("grant.permission_label")}>
            <select className={INPUT_CLASS} value={permission} onChange={(event) => setPermission(event.target.value as Permission)}>
              {permissionsForResource(resourceKind).map((item) => <option key={item} value={item}>{t(`grant.permission.${item}`)}</option>)}
            </select>
          </AccessField>
          <AccessField label={t("grant.effect_label")}>
            <select className={INPUT_CLASS} value={effect} onChange={(event) => setEffect(event.target.value)}>
              <option value="allow">{t("effect_allow")}</option>
              <option value="deny">{t("effect_deny")}</option>
            </select>
          </AccessField>
          <AccessField label={t("grant.expires_label")}>
            <input type="datetime-local" className={INPUT_CLASS} value={expires}
              onChange={(event) => setExpires(event.target.value)} />
          </AccessField>
        </div>
        <p className="resource-grant-summary" role="status">
          {ready && resource && target ? t("grant.summary", {
            effect: t(effect === "allow" ? "effect_allow" : "effect_deny"),
            principal: target.label.trim(), resource: resource.label.trim(),
            permission: t(`grant.permission.${permission}`),
            expiry: expires ? new Date(expires).toLocaleString(locale) : t("grant.no_expiry"),
          }) : t("grant.summary_empty")}
        </p>
        <button type="submit" className="ui-btn ui-btn--primary legacy-access-btn" disabled={!ready}>{t("save")}</button>
      </fieldset>
    </form>
  );
}
