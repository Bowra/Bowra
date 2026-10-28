"use client";

import { SaveBar } from "@/components/ui/save-bar";
import { canvasCloseGuardOptions } from "@/components/canvas/object";
import { BEFORE_APP_NAVIGATION_EVENT, type AppNavigationDetail } from "@/lib/use-app-router";
import { useSpaceSelection } from "@/lib/use-space";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import type { CoachEditorSnapshot } from "@/lib/coach-canvas-target";
import type { SkillEditorPort } from "@/components/skill-drawer";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { CapabilityLink } from "@/components/capabilities/capability-link";
import { PersonalMcpAgentBindings } from "@/components/settings/personal-mcp-settings";
import { personalMcpApi, type PersonalMcpAgent } from "@/lib/personal-mcp-api";
import { DatarcMark } from "@/components/settings/data-source-icons";
import { Bot, ChevronDown, ChevronRight } from "@/components/icons";
import { COACH_AGENT_ID } from "@/lib/coach-alignment-continue";
import { handoffAgentToNewChat } from "@/lib/composer-handoff";
import { Dialog } from "@/components/ui/dialog";
import { Tab, TabList, TabPanel, Tabs, tabPanelId } from "@/components/ui/tabs";

import { useCallback, useEffect, useId, useMemo, useRef, useState, type HTMLAttributes, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import { ErrorDiagnosticsInline } from "@/components/error-notice";
import { FeedbackPrefillPanel } from "@/components/agents/feedback-prefill-panel";
import { PromptBudgetHint } from "@/components/agents/prompt-budget-hint";
import { MAX_PROMPT_CHARS_TEAM } from "@/lib/agent-prompt-budget";
import { clearFeedbackPrefill, peekFeedbackPrefill, redraftFeedbackCandidate, stashFeedbackPrefill, type FeedbackPrefillHandoff } from "@/lib/feedback-candidates-api";
import {
  archiveAgent,
  createAgentRollback,
  getAgentEvalManager,
  getAgentForkSettings,
  getAgentAuthoringCatalog,
  getAgentWorkingCopy,
  listAgentReleases,
  publishAgentWorkingCopy,
  restoreAgent,
  runAgentSafetyCheck,
  saveAgentWorkingCopy,
  setManagedAgentResourceStatus,
  updateAgentForkPolicy,
  type AgentAudienceSnapshot,
  type AgentAuthoringCatalog,
  type AgentAuthoringPrimaryCapability,
  type AgentAuthoringTool,
  type AgentExactSkillGrant,
  type AgentForkSettings,
  type AgentReleaseHistoryItem,
  type AgentSafetyCheckReport,
  type AgentWorkingCopyDetail,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { listTeamPlugins, type TeamPlugin } from "@/lib/team-plugins-api";
import { toolSelectionLockedReasonKey, withoutSkillOperationToolRefs, withoutSkillOperationTools, agentResourceCounts } from "@/lib/agent-tool-selection";
import { appPath } from "@/lib/base-path";
import { readReleaseSeen, releaseResultUnseen, writeReleaseSeen } from "@/lib/agent-release-seen";
import { fetchTeamDirectory, fetchTeamGroups, type DirectoryGroup, type DirectoryUser } from "@/lib/content-share-api";
import { localizedToolText } from "@/lib/tool-display";
import { toolLabel } from "@/lib/tool-labels";
import { authoringSkillDescription, authoringSkillMatches, authoringSkillTitle } from "@/lib/skills-meta";
import { AgentBreadcrumb } from "./agent-breadcrumb";
import { agentArtworkStyle } from "./agent-artwork";
import { agentMark } from "./agent-mark";
import { AgentEvalPanel } from "./agent-eval-panel";
import type { TestChangeState } from "./agent-version-validation";
import { PluginUpdateNotice } from "./plugin-update-notice";
import { AgentLocalizationPanel } from "./agent-localization-panel";
import { AgentOptionalSkillsPanel } from "./agent-optional-skills-panel";
import { optionalSkillsNeedPrivateAudience } from "@/lib/agent-runtime-extensions";
import { agentReleaseBlockedText, agentReleaseErrorKey, agentReleaseReasonKey } from "./agent-release-reason";
import { AgentTaxonomyPanel } from "./agent-taxonomy-panel";
import { AGENT_PENDING_ANCHOR, AgentVersionWorkbenchPanel } from "./agent-version-workbench";
import { type PickerPlugin, ResourcePluginPicker, ResourceSummary, ResourceToolPicker, pluginMatches, pluginToolOwners, pluginsFromCatalog, visiblePlugins } from "@/components/ui/resource-picker";
import { randomUuid } from "@/lib/random-id";
import { releaseNoteText } from "@/lib/agent-release-notes";

type AudienceMode = AgentAudienceSnapshot["effective_mode"];
function HostedStage({ value: _value, ...props }: HTMLAttributes<HTMLDivElement> & { value: string }) {
  return <div {...props} />;
}

export type AgentManagerTask = "config" | "evaluate" | "release";
export type AgentConfigSection = "does" | "uses" | "who";

const AGENT_MANAGER_TASKS = new Set<AgentManagerTask>(["config", "evaluate", "release"]);
const AGENT_CONFIG_SECTIONS = new Set<AgentConfigSection>(["does", "uses", "who"]);
// Former Configure sub-pages, by the section that now holds them.
const LEGACY_CONFIG_VIEW_SECTIONS: Record<string, AgentConfigSection> = {
  prompt: "does", capabilities: "does", resources: "uses", access: "who", market: "who",
};
export const AGENT_HISTORY_ANCHOR = "agent-release-history";

export interface AgentManagerLocation {
  task: AgentManagerTask;
  section: AgentConfigSection | null;
  anchor: typeof AGENT_PENDING_ANCHOR | typeof AGENT_HISTORY_ANCHOR | null;
  /** The URL used an old parameter and is rewritten once, with `replace`. */
  legacy: boolean;
}

/** Where a workbench URL points, including links written before the four tabs existed. */
export function agentManagerLocation(search: string): AgentManagerLocation {
  const params = new URLSearchParams(search);
  const raw = params.get("section");
  const configView = params.get("config_view");
  const releaseView = params.get("release_view");
  const legacy = configView !== null || releaseView !== null;
  if (raw === "changes") return { task: "config", section: null, anchor: AGENT_PENDING_ANCHOR, legacy: true };
  if (raw === "history") return { task: "release", section: null, anchor: AGENT_HISTORY_ANCHOR, legacy: true };
  if (raw === "release" && releaseView === "tests") return { task: "evaluate", section: null, anchor: null, legacy: true };
  // The Automation tab was removed (design §10); its old links open Configure.
  if (raw === "automation") return { task: "config", section: null, anchor: null, legacy: true };
  const task = raw && AGENT_MANAGER_TASKS.has(raw as AgentManagerTask) ? raw as AgentManagerTask : "config";
  const requested = params.get("config_section");
  const section = task !== "config" ? null
    : requested && AGENT_CONFIG_SECTIONS.has(requested as AgentConfigSection) ? requested as AgentConfigSection
      : configView ? LEGACY_CONFIG_VIEW_SECTIONS[configView] ?? null : null;
  return { task, section, anchor: null, legacy };
}

function currentManagerLocation(): AgentManagerLocation {
  if (typeof window === "undefined") return { task: "config", section: null, anchor: null, legacy: false };
  return agentManagerLocation(window.location.search);
}

/** The canonical URL for a task: old parameters removed, the section or anchor kept. */
function managerTaskUrl(task: AgentManagerTask, section: AgentConfigSection | null = null, anchor: string | null = null): string {
  const url = new URL(window.location.href);
  url.searchParams.set("section", task);
  url.searchParams.delete("release_view");
  url.searchParams.delete("config_view");
  if (task === "config" && section) url.searchParams.set("config_section", section);
  else url.searchParams.delete("config_section");
  url.hash = anchor ?? "";
  return `${url.pathname}${url.search}${url.hash}`;
}

interface EditorDraft {
  displayName: string;
  description: string;
  sourceLocale: string;
  listingExtras: Record<string, unknown>;
  systemPrompt: string;
  maxIterations: number;
  capabilityProtocolBlock: boolean;
  model: string;
  outputSchema: string;
  primaryCapabilityRef: string | null;
  pluginIds: string[];
  /** Built-in Plugins (`builtin:<name>`); written only when non-empty so untouched Agents keep their version. */
  builtinPluginIds: string[];
  toolConnectionIds: string[];
  toolRefs: string[];
  skillNames: string[];
  skillGrants: AgentExactSkillGrant[];
  extensionFields: { runtime_extensions?: unknown };
  audienceMode: AudienceMode;
  userIds: string[];
  groupIds: string[];
}

interface ToolConnectionGroup {
  id: string;
  name: string;
  tools: AgentAuthoringTool[];
  selectable: boolean;
}

function PersonalToolAudienceNotice({ agentId }: { agentId: string }) {
  const t = useTranslations("agents_marketplace");
  const [hasPersonalTools, setHasPersonalTools] = useState(false);

  useEffect(() => {
    let active = true;
    void personalMcpApi<{ agents: PersonalMcpAgent[] }>("/agents/bindings")
      .then(({ agents }) => {
        if (!active) return;
        const binding = agents.find((agent) => agent.id === agentId);
        if (!binding) return;
        try {
          const toolIds: unknown = JSON.parse(binding.tool_ids_json);
          setHasPersonalTools(Array.isArray(toolIds) && toolIds.some((id) => typeof id === "string"));
        } catch {
          setHasPersonalTools(false);
        }
      })
      .catch(() => undefined);
    return () => { active = false; };
  }, [agentId]);

  if (!hasPersonalTools) return null;
  return <div className="am-release-audience-personal-tools" role="status"><strong>{t("release_audience_personal_tools_title")}</strong><p>{t("release_audience_personal_tools_body")}</p></div>;
}

function toolConnectionId(tool: AgentAuthoringTool): string | null {
  if (tool.source_kind !== "team_mcp") return null;
  if (tool.connection_id) return tool.connection_id;
  const parts = tool.stable_ref.split(".");
  return parts.length >= 3 && parts[0] === "teammcp" ? parts[1] : null;
}

function groupToolConnections(tools: AgentAuthoringTool[]): ToolConnectionGroup[] {
  const grouped = new Map<string, ToolConnectionGroup>();
  for (const tool of tools) {
    const id = toolConnectionId(tool);
    if (!id) continue;
    const current = grouped.get(id);
    if (current) {
      current.tools.push(tool);
      current.selectable ||= tool.selectable;
      continue;
    }
    grouped.set(id, {
      id,
      name: tool.source_display_name || id,
      tools: [tool],
      selectable: tool.selectable,
    });
  }
  return [...grouped.values()]
    .map((group) => ({ ...group, tools: group.tools.sort((left, right) => left.stable_ref.localeCompare(right.stable_ref)) }))
    .sort((left, right) => left.name.localeCompare(right.name));
}

function ToolConnectionCard({
  group,
  selected,
  disabled,
  capabilityLabel,
  detailsLabel,
  typeLabel,
  removeHint,
  onToggle,
}: {
  group: ToolConnectionGroup;
  selected: boolean;
  disabled: boolean;
  capabilityLabel: string;
  detailsLabel: string;
  typeLabel: string;
  removeHint: string;
  onToggle: () => void;
}) {
  const isDatarc = group.tools.some((tool) => /\.datarc_(?:workspaces|schema|query)$/.test(tool.stable_ref));
  return <article className="am-tool-connection-card" data-selected={selected ? "true" : undefined} data-disabled={disabled ? "true" : undefined}>
    <label>
      <input type="checkbox" name="agent-tool-connection" checked={selected} disabled={disabled} onChange={onToggle} aria-label={group.name} />
      <span className="am-tool-connection-icon" aria-hidden="true">{isDatarc ? <DatarcMark size={25} /> : "⌁"}</span>
      <span><strong>{group.name}</strong><small>{capabilityLabel}</small><em>{selected ? removeHint : typeLabel}</em></span>
    </label>
    <details>
      <summary>{detailsLabel}</summary>
      <ul>{group.tools.map((tool) => <li key={tool.stable_ref}><strong>{localizedToolText(tool).name}</strong><span>{localizedToolText(tool).description}</span></li>)}</ul>
    </details>
  </article>;
}

function parseArray(value: unknown): string[] {
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string");
  if (typeof value !== "string") return [];
  try {
    const decoded = JSON.parse(value) as unknown;
    return Array.isArray(decoded) ? decoded.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function parseObjectText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "string") {
    try {
      const decoded = JSON.parse(value) as unknown;
      return decoded && typeof decoded === "object" && !Array.isArray(decoded)
        ? JSON.stringify(decoded, null, 2)
        : "";
    } catch {
      return "";
    }
  }
  return typeof value === "object" && !Array.isArray(value)
    ? JSON.stringify(value, null, 2)
    : "";
}

function exactSkillGrants(definition: Record<string, unknown>): AgentExactSkillGrant[] {
  const exact = definition.skill_grants;
  if (!Array.isArray(exact)) return [];
  return exact.filter((entry): entry is AgentExactSkillGrant => {
    if (!entry || typeof entry !== "object") return false;
    const value = entry as Partial<AgentExactSkillGrant>;
    return Boolean(
      value.skill_ref
      && typeof value.skill_ref.name === "string"
      && typeof value.skill_ref.id === "string"
      && typeof value.revision === "string"
      && Array.isArray(value.permissions),
    );
  });
}

function skillNames(
  definition: Record<string, unknown>,
  grants: AgentExactSkillGrant[],
): string[] {
  if (grants.length) return grants.map((grant) => grant.skill_ref.name);
  return parseArray(definition.skills ?? definition.skills_json);
}

function editorDraft(
  detail: AgentWorkingCopyDetail,
  catalog: AgentAuthoringCatalog,
  availablePlugins: TeamPlugin[],
): EditorDraft {
  const source = detail.working_copy ?? detail.live;
  const definition = source.definition;
  const listing = source.listing;
  const audience = source.audience;
  const grants = exactSkillGrants(definition);
  // Stored Skill operation Tools are ignored by the server; the selected Skills grant them.
  const toolRefs = [...new Set(withoutSkillOperationToolRefs(parseArray(definition.tool_refs ?? definition.tool_refs_json)))].sort();
  const explicitConnectionIds = parseArray(definition.team_tool_connections);
  const derivedConnectionIds = catalog.tools
    .filter((tool) => toolRefs.includes(tool.stable_ref))
    .map(toolConnectionId)
    .filter((value): value is string => Boolean(value));
  const listingExtras = Object.fromEntries(
    Object.entries(listing).filter(([key]) => !["display_name", "description", "summary", "source_locale"].includes(key)),
  );
  return {
    displayName: typeof listing.display_name === "string" ? listing.display_name : detail.aggregate.name,
    description: typeof listing.description === "string"
      ? listing.description
      : typeof listing.summary === "string" ? listing.summary : "",
    sourceLocale: typeof listing.source_locale === "string" ? listing.source_locale : "zh-CN",
    listingExtras,
    systemPrompt: typeof definition.system_prompt === "string" ? definition.system_prompt : "",
    maxIterations: typeof definition.max_iterations === "number" ? definition.max_iterations : 10,
    capabilityProtocolBlock: definition.capability_protocol_block === true,
    model: typeof definition.model === "string" && catalog.model_policy.allowed_roles.includes(definition.model)
      ? definition.model
      : catalog.model_policy.effective_role,
    outputSchema: parseObjectText(definition.output_schema ?? definition.output_schema_json),
    primaryCapabilityRef: typeof definition.primary_capability_ref === "string"
      ? definition.primary_capability_ref
      : null,
    pluginIds: parseArray(definition.capability_ids).filter((id) => availablePlugins.some((item) => item.id === id)),
    builtinPluginIds: [...new Set(parseArray(definition.builtin_plugin_ids))].sort(),
    toolConnectionIds: [...new Set(explicitConnectionIds.length ? explicitConnectionIds : derivedConnectionIds)].sort(),
    toolRefs,
    skillNames: [...new Set(skillNames(definition, grants))].sort(),
    skillGrants: grants.sort((left, right) => left.skill_ref.name.localeCompare(right.skill_ref.name)),
    extensionFields: Object.hasOwn(definition, "runtime_extensions")
      ? { runtime_extensions: structuredClone(definition.runtime_extensions) }
      : {},
    audienceMode: audience.effective_mode,
    userIds: [...audience.user_ids].sort(),
    groupIds: [...audience.group_ids].sort(),
  };
}

function fingerprint(value: EditorDraft): string {
  return JSON.stringify({
    ...value,
    toolConnectionIds: [...value.toolConnectionIds].sort(),
    toolRefs: [...value.toolRefs].sort(),
    skillNames: [...value.skillNames].sort(),
    skillGrants: [...value.skillGrants].sort((left, right) => left.skill_ref.name.localeCompare(right.skill_ref.name)),
    userIds: [...value.userIds].sort(),
    groupIds: [...value.groupIds].sort(),
  });
}

function errorText(_error: unknown, fallback: string): string {
  return fallback;
}

function formatDate(value: string, locale: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function audienceCount(draft: EditorDraft): number {
  if (draft.audienceMode !== "custom") return 0;
  return draft.userIds.length + draft.groupIds.length;
}

function localizedCapabilityText(
  capability: AgentAuthoringPrimaryCapability,
  locale: string,
): { name: string; description: string } {
  return localizedToolText(capability, locale);
}

type HostedAgentEditor = {
  readOnly: boolean;
  controllerRef: { current: SkillEditorPort | null };
  onActivate: () => void;
  onClose: () => void;
  onSaved: (title: string) => void;
};

export function AgentReleaseManager({ agentId, returnHref = "/agents/manage/created/", hosted }: {
  agentId: string; returnHref?: string; hosted?: HostedAgentEditor;
}) {
  const hostedMode = hosted !== undefined;
  const mounted = useRef(true);
  const loadRequest = useRef(0);
  const navigationIntent = useRef(0);
  const approvedInput = useRef<string | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; loadRequest.current += 1; navigationIntent.current += 1; }; }, []);
  const toolT = useTranslations("settings.subagents");
  const t = useTranslations("agents_marketplace");
  const navT = useTranslations("sidebar.centers");
  const pickerT = useTranslations("resource_picker");
  const { confirm, dialog: navigationDialog } = useConfirmDialog();
  const locale = useLocale();
  const skillReasonId = useId();
  const [detail, setDetail] = useState<AgentWorkingCopyDetail | null>(null);
  const [catalog, setCatalog] = useState<AgentAuthoringCatalog | null>(null);
  const [teamPlugins, setTeamPlugins] = useState<TeamPlugin[]>([]);
  const [releases, setReleases] = useState<AgentReleaseHistoryItem[]>([]);
  const [forkSettings, setForkSettings] = useState<AgentForkSettings | null>(null);
  const [draft, setDraft] = useState<EditorDraft | null>(null);
  const [savedFingerprint, setSavedFingerprint] = useState("");
  const [safety, setSafety] = useState<AgentSafetyCheckReport | null>(null);
  const [releaseNote, setReleaseNote] = useState("");
  const [users, setUsers] = useState<DirectoryUser[]>([]);
  const [groups, setGroups] = useState<DirectoryGroup[]>([]);
  const [directoryQuery, setDirectoryQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Raw version-workbench failure: Copy diagnostics only while its corresponding error remains visible.
  const [versionErrorCause, setVersionErrorCause] = useState<{ message: string; cause: unknown } | null>(null);
  const handleVersionError = useCallback((message: string, cause?: unknown) => {
    setError(message);
    setVersionErrorCause(message && cause !== undefined ? { message, cause } : null);
  }, []);
  // This page reports every action's failure into one banner under the hero, but most actions —
  // grants, webhooks, revocation, dependency policy, proposal merges — live far below it. Dogfooding
  // 116 showed a rejected webhook URL rendered 1,174px above the button that caused it, so the
  // Owner saw nothing happen at all. Bring the banner into view whenever a new failure arrives.
  const errorBannerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (error) errorBannerRef.current?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [error]);
  const [statusMessage, setStatusMessage] = useState("");
  const [confirmArchive, setConfirmArchive] = useState(false);
  // agent-feedback-loop FE batch B: one-time Adopt and prefill inbox handoff (read-and-delete, agent validation).
  // Panel gives revised body to draft only; saving/evaluation/publishing retain pipeline/gates/page (tech-design §2.3).
  const [feedbackPrefill, setFeedbackPrefill] = useState<FeedbackPrefillHandoff | null>(null);
  const [feedbackPrefillApplied, setFeedbackPrefillApplied] = useState(false);
  const [askPublish, setAskPublish] = useState(false);
  const [realigning, setRealigning] = useState(false);
  const [versionAvailability, setVersionAvailability] = useState<"checking" | "enabled" | "disabled">("checking");
  const [evalAvailability, setEvalAvailability] = useState<"checking" | "enabled" | "disabled">("checking");
  const [evalPanelVisited, setEvalPanelVisited] = useState(false);
  // Evaluate: the suite editor and the test card above it are separate panels; these keep them in step.
  const [suitesRefreshKey, setSuitesRefreshKey] = useState(0);
  const [addCaseRequest, setAddCaseRequest] = useState(0);
  const [evaluateTestState, setEvaluateTestState] = useState<TestChangeState | null>(null);
  const [activeTask, setActiveTask] = useState<AgentManagerTask>(() => hostedMode ? "config" : currentManagerLocation().task);
  // "Who can use it" starts folded; a link to a section opens it.
  const [closedSections, setClosedSections] = useState<ReadonlySet<AgentConfigSection>>(() => new Set(["who"]));
  // "Advanced" under What it does (Item 4): the Owner's own toggle wins; until then it follows the Agent.
  const [advancedOpen, setAdvancedOpen] = useState<boolean | null>(null);
  const [addResourcesOpen, setAddResourcesOpen] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);
  // A result waiting on Release, and the newest one this viewer has already seen there (S5 tab marker).
  const [releaseResult, setReleaseResult] = useState<string | null>(null);
  const [releaseSeen, setReleaseSeen] = useState<string | null>(null);
  const [coachSlot, setCoachSlot] = useState<HTMLDivElement | null>(null);
  const locationApplied = useRef(false);
  useEffect(() => { setReleaseSeen(readReleaseSeen(agentId)); }, [agentId]);
  useEffect(() => {
    // Opening Release marks the current result as seen; storing its own timestamp avoids client clock skew.
    if (activeTask !== "release" || !releaseResult) return;
    writeReleaseSeen(agentId, releaseResult);
    setReleaseSeen(releaseResult);
  }, [activeTask, agentId, releaseResult]);
  // The hosted editor's own Save and Discard, for Chat's "Save and continue" (C6-e).
  const hostedActions = useRef<{ save: () => Promise<boolean>; discard: () => Promise<boolean> }>({
    save: async () => false, discard: async () => false,
  });
  const [resourceQuery, setResourceQuery] = useState("");
  const [resourceVisibleCount, setResourceVisibleCount] = useState(8);
  const fp = useTranslations("feedback_prefill");
  const { activeId: workspaceId } = useSpaceSelection();
  const archiveConfirmationRef = useRef<HTMLElement | null>(null);
  const resourceActionsRef = useRef<HTMLDivElement | null>(null);
  const localize = useCallback((key: string, fallback: string) => (
    t.has(key as never) ? t(key as never) : fallback
  ), [t]);
  const openRecommendedTask = useCallback((task: "config" | "release") => {
    if (hostedMode) return;
    const url = new URL(window.location.href);
    if (url.searchParams.has("section") || url.searchParams.get("feedback_prefill") === "1") return;
    window.history.replaceState(null, "", managerTaskUrl(task));
    setActiveTask(task);
  }, [hostedMode]);
  const revealAnchor = useCallback((id: string) => {
    window.requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView?.({ block: "start", behavior: "smooth" }));
  }, []);
  const openSection = useCallback((section: AgentConfigSection) => {
    setClosedSections((current) => {
      if (!current.has(section)) return current;
      const next = new Set(current);
      next.delete(section);
      return next;
    });
    revealAnchor(`agent-config-${section}`);
  }, [revealAnchor]);

  const inputFingerprint = draft ? fingerprint(draft) : "";
  const latestInput = useRef({ fingerprint: inputFingerprint, savedFingerprint, hosted });
  latestInput.current = { fingerprint: inputFingerprint, savedFingerprint, hosted };
  const load = useCallback(async (savedInput?: string) => {
    if (!mounted.current) return;
    const request = ++loadRequest.current;
    const original = latestInput.current;
    setLoading(true);
    try {
      const [nextDetail, nextCatalog, history, nextForkSettings, nextTeamPlugins] = await Promise.all([
        getAgentWorkingCopy(agentId),
        getAgentAuthoringCatalog(),
        listAgentReleases(agentId),
        getAgentForkSettings(agentId).catch((cause) => {
          if (cause instanceof ApiError && cause.status === 404) return null;
          throw cause;
        }),
        listTeamPlugins(),
      ]);
      if (!mounted.current || request !== loadRequest.current) return;
      // A background read cannot absorb a different baseline under retained local input.
      if (latestInput.current.fingerprint !== original.fingerprint
        || original.fingerprint !== original.savedFingerprint && original.fingerprint !== savedInput) return;
      const catalogValue = { ...nextCatalog, tools: withoutSkillOperationTools(nextCatalog.tools) };
      const nextDraft = editorDraft(nextDetail, catalogValue, nextTeamPlugins);
      setDetail(nextDetail);
      setCatalog(catalogValue);
      setTeamPlugins(nextTeamPlugins);
      setReleases(history.items);
      setForkSettings(nextForkSettings);
      setDraft(nextDraft);
      setSavedFingerprint(fingerprint(nextDraft));
      setSafety(null);
      setError(null);
    } catch (cause) {
      if (!mounted.current || request !== loadRequest.current) return;
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status) && cause.code !== "agent_release_cutover_required") {
        setDetail(null); setDraft(null); setCatalog(null);
      }
      // "No release history yet" is a deployment state, not a missing agent. The
      // backend used to answer agent_not_found here, telling the owner of a
      // catalog-visible agent that it did not exist.
      if (cause instanceof ApiError && cause.code === "agent_release_cutover_required") {
        setError(t("release_cutover_required"));
      } else {
        setError(errorText(cause, t("release_load_failed")));
      }
    } finally {
      if (mounted.current && request === loadRequest.current) setLoading(false);
    }
  }, [agentId, t]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (hostedMode) return;
    let cancelled = false;
    setEvalAvailability("checking");
    void getAgentEvalManager(agentId).then(() => {
      if (!cancelled) setEvalAvailability("enabled");
    }).catch((cause) => {
      if (cancelled) return;
      setEvalAvailability(cause instanceof ApiError && cause.status === 404 ? "disabled" : "enabled");
    });
    return () => { cancelled = true; };
  }, [agentId, hostedMode]);

  useEffect(() => {
    if (hostedMode || versionAvailability === "checking") return;
    const syncNavigation = (fromHistory: boolean) => {
      const requested = currentManagerLocation();
      const unavailable = requested.task === "evaluate" && evalAvailability === "disabled";
      // An evaluation link falls back to Release, the closest view that is always there.
      const task = !unavailable ? requested.task : requested.task === "evaluate" ? "release" : "config";
      if (requested.legacy || unavailable) {
        window.history.replaceState(null, "", managerTaskUrl(task, task === "config" ? requested.section : null, requested.anchor));
      }
      setActiveTask(task);
      // The URL's section and anchor apply once on arrival and on Back/Forward, not each time availability
      // settles, which would reopen a section the Owner just folded.
      if (fromHistory || !locationApplied.current) {
        locationApplied.current = true;
        if (task === "config" && requested.section) openSection(requested.section);
        if (requested.anchor) revealAnchor(requested.anchor);
      }
    };
    const onPopState = () => syncNavigation(true);
    syncNavigation(false);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [evalAvailability, versionAvailability, hostedMode, openSection, revealAnchor]);

  useEffect(() => {
    if (activeTask === "evaluate" && evalAvailability === "enabled") setEvalPanelVisited(true);
  }, [activeTask, evalAvailability]);

  useEffect(() => {
    if (hostedMode) return;
    // With feedback_prefill=1, inspect without deleting: candidate already accepted, cannot re-adopt;
    // refresh/load failure must not lose draft. Explicit clear on Apply/Discard; no flag means remnant, clear directly.
    const flagged = new URLSearchParams(window.location.search).get("feedback_prefill") === "1";
    if (!flagged) {
      clearFeedbackPrefill();
      return;
    }
    const handoff = peekFeedbackPrefill(agentId);
    if (handoff?.prefill?.kind === "agent_system_prompt") setFeedbackPrefill(handoff);
  }, [agentId, hostedMode]);

  useEffect(() => {
    if (!detail?.actions.edit) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void Promise.all([
        fetchTeamDirectory(directoryQuery.trim() || undefined, 0),
        fetchTeamGroups(),
      ]).then(([directory, groupDirectory]) => {
        if (cancelled) return;
        setUsers(directory.items);
        setGroups(groupDirectory.items.filter((group) => !directoryQuery.trim()
          || group.name.toLocaleLowerCase().includes(directoryQuery.trim().toLocaleLowerCase())));
      }).catch(() => {
        if (!cancelled) setError(t("release_directory_failed"));
      });
    }, 180);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [detail?.actions.edit, directoryQuery]);

  const toolsByRef = useMemo(
    () => new Map(catalog?.tools.map((tool) => [tool.stable_ref, tool]) ?? []),
    [catalog],
  );
  const toolConnections = useMemo(
    () => groupToolConnections(catalog?.tools ?? []),
    [catalog],
  );
  const toolConnectionsById = useMemo(
    () => new Map(toolConnections.map((connection) => [connection.id, connection])),
    [toolConnections],
  );
  const toolConnectionSummary = (connection: ToolConnectionGroup) => {
    const locked = connection.tools.find((tool) => !tool.selectable && tool.locked_reason);
    const base = t("version_config_connection_capabilities", { count: connection.tools.length });
    return locked ? `${base} · ${toolT(toolSelectionLockedReasonKey(locked.locked_reason ?? ""))}` : base;
  };
  const skillsByName = useMemo(
    () => new Map(catalog?.skills.map((skill) => [skill.name, skill]) ?? []),
    [catalog],
  );
  // Agents follow a Skill's latest approved revision, so a revision difference is an update, not a
  // missing dependency; only a Skill that is gone or now names a different Skill is.
  const missingSkills = draft?.skillNames.filter((name) => {
    const catalogGrant = skillsByName.get(name)?.grant;
    if (!catalogGrant) return true;
    const existingGrant = draft.skillGrants.find((grant) => grant.skill_ref.name === name);
    return Boolean(existingGrant && (
      existingGrant.skill_ref.kind !== catalogGrant.skill_ref.kind
      || existingGrant.skill_ref.id !== catalogGrant.skill_ref.id
    ));
  }) ?? [];
  const missingTools = draft?.toolRefs.filter((ref) => !toolsByRef.has(ref)) ?? [];
  const audienceBlockedSkills = draft?.skillNames.filter((name) => {
    const skill = skillsByName.get(name);
    return skill ? !skill.allowed_audiences.includes(draft.audienceMode) : false;
  }) ?? [];
  const dirty = draft !== null && fingerprint(draft) !== savedFingerprint;
  const needsSave = dirty || !detail?.working_copy;
  const hasCapability = Boolean(
    draft?.primaryCapabilityRef
    || draft?.pluginIds.length
    || draft?.builtinPluginIds.length
    || draft?.toolConnectionIds.length
    || draft?.toolRefs.length
    || draft?.skillNames.length,
  );
  const selectedPrimaryCapability = catalog?.primary_capabilities.find(
    (capability) => capability.exact_ref === draft?.primaryCapabilityRef,
  );
  const missingPrimaryCapability = Boolean(
    draft?.primaryCapabilityRef && !selectedPrimaryCapability,
  );
  const customAudienceInvalid = draft?.audienceMode === "custom" && audienceCount(draft) === 0;
  const optionalAudienceInvalid = draft ? optionalSkillsNeedPrivateAudience(draft.extensionFields.runtime_extensions, draft.audienceMode) : false;
  const displayNameInvalid = !draft?.displayName.trim();
  const outputSchemaInvalid = useMemo(() => {
    if (!draft?.outputSchema.trim()) return false;
    try {
      const value = JSON.parse(draft.outputSchema) as unknown;
      return !value || typeof value !== "object" || Array.isArray(value);
    } catch {
      return true;
    }
  }, [draft?.outputSchema]);
  const listFormatter = useMemo(
    () => new Intl.ListFormat(locale, { style: "short", type: "conjunction" }),
    [locale],
  );
  const pickerPlugins = useMemo(() => pluginsFromCatalog(catalog?.plugins, locale), [catalog?.plugins, locale]);
  const selectedPluginIds = useMemo(
    () => new Set([...(draft?.pluginIds ?? []), ...(draft?.builtinPluginIds ?? [])]),
    [draft?.pluginIds, draft?.builtinPluginIds],
  );
  const pluginToolOwner = useMemo(() => pluginToolOwners(pickerPlugins, selectedPluginIds), [pickerPlugins, selectedPluginIds]);
  const resourceSummary = useMemo(() => {
    const tools = new Set([...(draft?.toolRefs ?? []), ...pluginToolOwner.keys()]);
    const selectedPlugins = pickerPlugins.filter((plugin) => selectedPluginIds.has(plugin.id));
    return {
      tools: tools.size,
      skills: (draft?.skillNames.length ?? 0) + selectedPlugins.reduce((total, plugin) => total + plugin.skillCount, 0),
      needsConfirmation: [...tools].filter((ref) => {
        const risk = toolsByRef.get(ref)?.risk;
        return risk === "external_write" || risk === "destructive";
      }).length,
      guidance: selectedPlugins.filter((plugin) => plugin.guidance).map((plugin) => plugin.name),
    };
  }, [draft?.toolRefs, draft?.skillNames, pickerPlugins, pluginToolOwner, selectedPluginIds, toolsByRef]);
  // The Tools this Agent can call (its own and its Plugins'), by the names the Configure picker shows: the
  // only Tools a required action on Evaluate can name.
  const evalPointTools = useMemo(() => {
    const refs = [...new Set([...(draft?.toolRefs ?? []), ...pluginToolOwner.keys()])];
    return refs.map((ref) => {
      const tool = toolsByRef.get(ref);
      return { ref, name: tool ? localizedToolText(tool).name : toolLabel(ref).name };
    }).sort((a, b) => a.name.localeCompare(b.name, locale));
  }, [draft?.toolRefs, locale, pluginToolOwner, toolsByRef]);
  const selectablePrimaryCapabilities = catalog?.primary_capabilities.filter(
    (capability) => capability.selectable || capability.exact_ref === draft?.primaryCapabilityRef,
  ) ?? [];
  const unavailablePrimaryCapabilities = catalog?.primary_capabilities.filter(
    (capability) => !capability.selectable && capability.exact_ref !== draft?.primaryCapabilityRef,
  ) ?? [];
  const selectedTools = catalog?.tools.filter((tool) => tool.source_kind === "native" && draft?.toolRefs.includes(tool.stable_ref)) ?? [];
  const selectedToolConnections = toolConnections.filter((connection) => draft?.toolConnectionIds.includes(connection.id));
  const missingToolConnections = draft?.toolConnectionIds.filter((id) => !toolConnectionsById.has(id)) ?? [];
  const selectedSkills = catalog?.skills.filter((skill) => draft?.skillNames.includes(skill.name) && !missingSkills.includes(skill.name)) ?? [];
  // One count of what the Agent can use, Plugin-provided Tools and Skills included, for every place that states it
  // (the "can do" summary, the added-resources heading and the save card), so they never disagree.
  const pickedPlugins = pickerPlugins.filter((plugin) => selectedPluginIds.has(plugin.id));
  const resourceCounts = {
    plugins: pickedPlugins.length,
    skills: pickerPlugins.length ? resourceSummary.skills : (draft?.skillNames.length ?? 0),
    tools: pickerPlugins.length ? resourceSummary.tools : selectedTools.length,
    connections: draft?.toolConnectionIds.length ?? 0,
  };
  const hasResources = resourceCounts.plugins + resourceCounts.skills + resourceCounts.tools + resourceCounts.connections > 0;
  const normalizedResourceQuery = resourceQuery.trim().toLocaleLowerCase(locale);
  const availableTools = catalog?.tools.filter((tool) => {
    if (tool.source_kind !== "native") return false;
    if (draft?.toolRefs.includes(tool.stable_ref)) return false;
    if (!normalizedResourceQuery) return true;
    const copy = localizedToolText(tool);
    return `${copy.name} ${copy.description} ${tool.stable_ref}`.toLocaleLowerCase(locale).includes(normalizedResourceQuery);
  }) ?? [];
  const availableToolConnections = toolConnections.filter((connection) => {
    if (draft?.toolConnectionIds.includes(connection.id)) return false;
    if (!normalizedResourceQuery) return true;
    const searchable = [connection.name, connection.id, ...connection.tools.flatMap((tool) => {
      const copy = localizedToolText(tool);
      return [copy.name, copy.description, tool.stable_ref];
    })].join(" ");
    return searchable.toLocaleLowerCase(locale).includes(normalizedResourceQuery);
  });
  const availableSkills = catalog?.skills.filter((skill) => {
    if (draft?.skillNames.includes(skill.name) || missingSkills.includes(skill.name)) return false;
    return authoringSkillMatches(skill, resourceQuery, locale);
  }) ?? [];
  const visibleAvailableSkills = availableSkills.slice(0, resourceVisibleCount);
  // One search over the sectioned catalog (Plugins → Tools → Skills); selected Plugins stay in the main panel.
  const pluginCanManage = Boolean(catalog?.plugins?.viewer_can_manage);
  const libraryPlugins = visiblePlugins(pickerPlugins, { canManage: pluginCanManage, selected: new Set() })
    .filter((plugin) => !selectedPluginIds.has(plugin.id) && pluginMatches(plugin, resourceQuery, locale));
  const libraryEmpty = Boolean(normalizedResourceQuery) && !libraryPlugins.length && !availableToolConnections.length && !availableTools.length && !availableSkills.length;

  useEffect(() => {
    if (hostedMode || !dirty && !releaseNote.trim()) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const guard = (event: Event) => {
      event.preventDefault();
      const { proceed } = (event as CustomEvent<AppNavigationDetail>).detail;
      void confirm({ title: navT("discard_title"), description: navT("discard_description"), confirmLabel: navT("discard_action"), cancelLabel: navT("discard_keep") })
        .then((leave) => { if (leave) proceed(); });
    };
    window.addEventListener("beforeunload", warn);
    window.addEventListener(BEFORE_APP_NAVIGATION_EVENT, guard);
    return () => {
      window.removeEventListener("beforeunload", warn);
      window.removeEventListener(BEFORE_APP_NAVIGATION_EVENT, guard);
    };
  }, [dirty, releaseNote, confirm, navT, hostedMode]);

  const tObject = useTranslations("canvas_object");
  const hostedState = useRef({ dirty, busy, inputFingerprint, hosted, confirm, navT, detail, tObject, name: draft?.displayName ?? "" });
  hostedState.current = { dirty, busy, inputFingerprint, hosted, confirm, navT, detail, tObject, name: draft?.displayName ?? "" };
  useEffect(() => {
    if (!hostedMode) return;
    const hasUnsaved = () => Boolean(hostedState.current.busy
      || hostedState.current.dirty && approvedInput.current !== hostedState.current.inputFingerprint);
    const allowLeave = async (afterClose?: () => void) => {
      const intent = ++navigationIntent.current;
      const state = hostedState.current;
      if (!mounted.current || state.busy) return false;
      if (state.dirty && !await state.confirm(canvasCloseGuardOptions(state.tObject, state.name))) return false;
      if (!mounted.current || intent !== navigationIntent.current || hostedState.current.busy
        || state.inputFingerprint !== hostedState.current.inputFingerprint) return false;
      approvedInput.current = state.inputFingerprint;
      afterClose?.();
      return true;
    };
    const port: SkillEditorPort = { requestClose: (afterClose) => { void allowLeave(afterClose ?? (() => hostedState.current.hosted?.onClose())); }, capture: (): CoachEditorSnapshot | null => {
      const state = hostedState.current; const value = state.detail;
      if (!mounted.current || !value) return null;
      const copy = value.working_copy;
      return { target: { kind: "agent", id: value.agent_id, version: value.aggregate_row_version, live_release_id: value.live.release_id,
        working_copy: copy ? { id: copy.id, version: copy.row_version, base_release_id: [null, "", "None"].includes(copy.base_release_id) ? null : copy.base_release_id } : null },
        dirty: state.dirty, pending_write: state.busy !== null };
    }, save: () => hostedActions.current.save(), discard: () => hostedActions.current.discard() };
    if (hosted?.controllerRef) hosted.controllerRef.current = port;
    const unregister = registerSubagentDraftNavigationGuard({ hasUnsaved,
      confirmLeave: async () => { if (!hasUnsaved()) return true; hostedState.current.hosted?.onActivate(); return allowLeave(); },
      cancelLeave: () => { navigationIntent.current += 1; approvedInput.current = null; },
    });
    const warn = (event: BeforeUnloadEvent) => { if (hostedState.current.dirty || hostedState.current.busy) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => { unregister(); window.removeEventListener("beforeunload", warn);
      if (hosted?.controllerRef.current === port) hosted.controllerRef.current = null; };
  }, [hostedMode, hosted?.controllerRef]);

  useEffect(() => {
    if (confirmArchive) archiveConfirmationRef.current?.focus();
  }, [confirmArchive]);

  const togglePlugin = (plugin: PickerPlugin) => {
    if (!draft) return;
    if (plugin.kind === "builtin") {
      update({ builtinPluginIds: draft.builtinPluginIds.includes(plugin.id) ? draft.builtinPluginIds.filter((id) => id !== plugin.id) : [...draft.builtinPluginIds, plugin.id].sort() });
    } else {
      update({ pluginIds: draft.pluginIds.includes(plugin.id) ? draft.pluginIds.filter((id) => id !== plugin.id) : [...draft.pluginIds, plugin.id] });
    }
  };
  const update = (values: Partial<EditorDraft>) => {
    setDraft((current) => current ? { ...current, ...values } : current);
    setSafety(null);
    setError(null);
    setAskPublish(false); // Editing prevents direct publish (evaluation gate requires saved state); hide prompt, ask again after save.
  };

  const toggleToolConnection = (connection: ToolConnectionGroup) => {
    if (!draft) return;
    const selected = draft.toolConnectionIds.includes(connection.id);
    const connectionRefs = new Set(connection.tools.map((tool) => tool.stable_ref));
    const selectableConnectionRefs = connection.tools.filter((tool) => tool.selectable).map((tool) => tool.stable_ref);
    update({
      toolConnectionIds: selected
        ? draft.toolConnectionIds.filter((id) => id !== connection.id)
        : [...draft.toolConnectionIds, connection.id].sort(),
      toolRefs: selected
        ? draft.toolRefs.filter((ref) => !connectionRefs.has(ref))
        : [...new Set([...draft.toolRefs, ...selectableConnectionRefs])].sort(),
    });
  };

  const toggle = (field: "toolRefs" | "userIds" | "groupIds", value: string) => {
    if (!draft) return;
    const current = draft[field];
    update({ [field]: current.includes(value) ? current.filter((item) => item !== value) : [...current, value].sort() });
  };

  const toggleSkill = (name: string) => {
    if (!draft || !catalog) return;
    if (draft.skillNames.includes(name)) {
      update({
        skillNames: draft.skillNames.filter((item) => item !== name),
        skillGrants: draft.skillGrants.filter((grant) => grant.skill_ref.name !== name),
      });
      return;
    }
    const namesToAdd = new Set<string>();
    const visit = (skillName: string): boolean => {
      if (draft.skillNames.includes(skillName) || namesToAdd.has(skillName)) return true;
      const skill = skillsByName.get(skillName);
      if (!skill) return false;
      namesToAdd.add(skillName);
      return (skill.required_skills ?? []).every(visit);
    };
    if (!visit(name)) {
      setError(t("release_skill_dependency_unavailable"));
      return;
    }
    const nextGrants = new Map(draft.skillGrants.map((grant) => [grant.skill_ref.name, grant]));
    for (const skillName of namesToAdd) {
      const grant = skillsByName.get(skillName)?.grant;
      if (!grant) return;
      nextGrants.set(skillName, grant);
    }
    update({
      skillNames: [...new Set([...draft.skillNames, ...namesToAdd])].sort(),
      skillGrants: [...nextGrants.values()].sort((left, right) => (
        left.skill_ref.name.localeCompare(right.skill_ref.name)
      )),
    });
  };

  const buildPayload = () => {
    if (!detail || !draft || !catalog) throw new Error(t("release_load_failed"));
    let outputSchema: Record<string, unknown> | null = null;
    if (draft.outputSchema.trim()) {
      const parsed = JSON.parse(draft.outputSchema) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error(t("release_output_schema_invalid"));
      }
      outputSchema = parsed as Record<string, unknown>;
    }
    // The catalog grant already carries every permission the Skill grants (run included for a Skill
    // with scripts) at its latest approved revision; a stored grant is kept only when the catalog lacks it.
    const grants = draft.skillNames.map((name) => (
      skillsByName.get(name)?.grant
      ?? draft.skillGrants.find((grant) => grant.skill_ref.name === name)
    ));
    if (grants.some((grant) => !grant)) throw new Error(t("release_dependency_stale"));
    const selectedGrants = grants.filter((grant): grant is AgentExactSkillGrant => Boolean(grant));
    // Selecting the Python sandbox Tool requires the catalog's exact invoke grant on its built-in Skill.
    const sandboxGrant = draft.toolRefs.includes("python_sandbox") ? skillsByName.get("python_sandbox")?.grant : undefined;
    const exactGrants = sandboxGrant
      ? [...selectedGrants.filter((grant) => grant.skill_ref.name !== "python_sandbox"), sandboxGrant]
      : selectedGrants;
    const nativeToolRefs = withoutSkillOperationToolRefs(draft.toolRefs.filter((ref) => !ref.startsWith("teammcp.")));
    const resolvedConnectionToolRefs = draft.toolConnectionIds.flatMap(
      (id) => toolConnectionsById.get(id)?.tools
        .filter((tool) => tool.selectable || draft.toolRefs.includes(tool.stable_ref))
        .map((tool) => tool.stable_ref) ?? [],
    );
    const resolvedToolRefs = [...new Set([...nativeToolRefs, ...resolvedConnectionToolRefs])].sort();
    // An Agent without a stored model follows the team's effective role. Keep it unset while the role shown is
    // that default, so saving another field neither pins the role nor counts as a model change.
    const storedModel = (detail.working_copy ?? detail.live).definition.model;
    const model = draft.model && (typeof storedModel === "string" || draft.model !== catalog.model_policy.effective_role) ? draft.model : null;
    return {
      definition: {
        ...draft.extensionFields,
        max_iterations: draft.maxIterations,
        // Written only when on, so saving an untouched Agent keeps its definition (and version) unchanged.
        ...(draft.capabilityProtocolBlock ? { capability_protocol_block: true } : {}),
        model,
        output_schema: outputSchema,
        primary_capability_ref: draft.primaryCapabilityRef,
        capability_ids: draft.pluginIds,
        ...(draft.builtinPluginIds.length ? { builtin_plugin_ids: [...draft.builtinPluginIds].sort() } : {}),
        skill_grants: exactGrants,
        system_prompt: draft.systemPrompt,
        team_tool_connections: [...draft.toolConnectionIds].sort(),
        tool_refs: resolvedToolRefs,
      },
      listing: {
        ...draft.listingExtras,
        description: draft.description,
        display_name: draft.displayName,
        source_locale: draft.sourceLocale,
      },
      audience: {
        schema_version: 1 as const,
        effective_mode: draft.audienceMode,
        user_ids: draft.audienceMode === "custom" ? [...draft.userIds].sort() : [],
        group_ids: draft.audienceMode === "custom" ? [...draft.groupIds].sort() : [],
      },
      expected_row_version: detail.working_copy?.row_version ?? null,
    };
  };

  const save = async (): Promise<boolean> => {
    if (hosted?.readOnly || !detail || !draft || busy || !hasCapability || missingPrimaryCapability || customAudienceInvalid || optionalAudienceInvalid || displayNameInvalid || outputSchemaInvalid) return false;
    setBusy("save");
    setStatusMessage("");
    try {
      await saveAgentWorkingCopy(agentId, buildPayload());
      if (!mounted.current) return false;
      setStatusMessage(t("release_saved", { version: detail.live.release_sequence }));
      if (feedbackPrefillApplied) {
        // One journey: after adopted prefill saves, ask whether to publish now, no separate path.
        // Retain applied flag until publish/decline/discard; edit and save again after saving must ask again.
        setAskPublish(true);
      }
      await load(inputFingerprint);
      if (mounted.current) latestInput.current.hosted?.onSaved(draft.displayName);
      return true;
    } catch (cause) {
      if (!mounted.current) return false;
      const releaseErrorKey = agentReleaseErrorKey(cause instanceof ApiError ? cause.code : undefined);
      const blockedText = cause instanceof ApiError ? agentReleaseBlockedText(cause, (key, values) => t(key as never, values as never)) : null;
      setError(cause instanceof ApiError && cause.code === "approval_executor_unavailable"
        ? toolT(toolSelectionLockedReasonKey(cause.code))
        : blockedText ?? (releaseErrorKey ? t(releaseErrorKey as never) : errorText(cause, t("release_save_failed"))));
      return false;
    } finally {
      setBusy(null);
    }
  };
  const discardHosted = async (): Promise<boolean> => {
    if (!detail || !catalog || !draft) return false;
    if (!await confirm(canvasCloseGuardOptions(tObject, draft.displayName))) return false;
    setDraft(editorDraft(detail, catalog, teamPlugins));
    return true;
  };
  hostedActions.current = { save, discard: discardHosted };

  const checkSafety = async (): Promise<AgentSafetyCheckReport | null> => {
    const working = detail?.working_copy;
    if (!working || busy || dirty) return null;
    setBusy("safety");
    try {
      const report = await runAgentSafetyCheck(agentId, working.id, working.row_version);
      setSafety(report);
      setStatusMessage(t("release_safety_finished"));
      setError(null);
      return report;
    } catch (cause) {
      setError(errorText(cause, t("release_safety_failed")));
      return null;
    } finally {
      setBusy(null);
    }
  };

  // Whether this publish becomes a pending candidate is the server's call --
  // `requires_admin_review` comes back from the safety check.  The old guess
  // (audience === "teamwide") was wrong for `custom` as soon as the Eval gate went
  // in, and the button kept saying "Release new version" for something the server
  // queued for approval.  Before a safety check has run there is no server answer,
  // so fall back to the audience, which is all the old code ever had.
  const willRequireReview = safety?.requires_admin_review ?? draft?.audienceMode === "teamwide";
  // Only shared Agents have reviewers, so only they hear about what reviewers see and about Lead.
  const sharedAgent = draft?.audienceMode !== "private";

  const publish = async (report: AgentSafetyCheckReport | null = safety) => {
    const working = detail?.working_copy;
    if (!working || !report?.actions.publish || busy || dirty || detail.pending_candidate) return;
    setAskPublish(false);
    setFeedbackPrefillApplied(false); // Manual publish also completes journey; never keep prompting later new drafts.
    // The server freezes the candidate together with its Evaluation Plan, which
    // needs an explicit Space (data scope); without one it always answers 422
    // agent_eval_workspace_required.
    if (!workspaceId) {
      setError(t("version_workspace_required"));
      return;
    }
    setBusy("publish");
    try {
      const result = await publishAgentWorkingCopy(
        agentId,
        {
          working_copy_id: working.id,
          // Safety check increments working-copy row_version (116: 1→2); report
          // subject.working_copy_row_version is correct publish version; detail's precheck value always yields 409.
          working_copy_row_version: report.subject?.working_copy_row_version ?? working.row_version,
          release_note: releaseNote.trim() || null,
          workspace_id: workspaceId,
        },
        randomUuid(),
      );
      setReleaseNote("");
      // A prepared candidate is not live yet: it is compared and released from the Release task.
      setStatusMessage(t(result.state === "prepared" ? "release_prepared" : result.state === "pending" ? "release_submitted" : "release_published"));
      await load();
    } catch (cause) {
      // Once Agent Eval is on, every non-private release goes through review, so a
      // team with no admin hits `qualified_reviewer_unavailable` on a path that used
      // to publish directly.  The generic copy tells them to fix safety or versions,
      // which is not the problem.
      const key = agentReleaseErrorKey(cause instanceof ApiError ? cause.code : undefined);
      const blockedText = cause instanceof ApiError ? agentReleaseBlockedText(cause, (name, values) => t(name as never, values as never)) : null;
      setError(blockedText ?? (key ? t(key as never) : errorText(cause, t("release_publish_failed"))));
    } finally {
      setBusy(null);
    }
  };

  // §2.3 ⑤: handoff drafting working-copy version ≠ current → baseline mismatch, panel read-only;
  // Realign with new baseline reruns server coach against current baseline, replacing handoff.
  const feedbackPrefillStale = Boolean(
    feedbackPrefill
    && feedbackPrefill.base.working_copy_row_version != null
    && detail?.working_copy
    && Number(feedbackPrefill.base.working_copy_row_version) !== detail.working_copy.row_version,
  );
  const realignFeedbackPrefill = async () => {
    if (!feedbackPrefill || realigning) return;
    setRealigning(true);
    try {
      const result = await redraftFeedbackCandidate(feedbackPrefill.candidate_id);
      if (!result.prefill || result.prefill.kind !== "agent_system_prompt") {
        setError(fp(result.degrade_reason ? `degrade_${result.degrade_reason}` as never : "realign_failed"));
        return;
      }
      const next: FeedbackPrefillHandoff = { ...feedbackPrefill, prefill: result.prefill, base: result.base ?? {} };
      stashFeedbackPrefill(next);
      setFeedbackPrefill(next);
      setError(null);
    } catch (cause) {
      setError(errorText(cause, fp("realign_failed")));
    } finally {
      setRealigning(false);
    }
  };

  // Publish-now followup retains evaluation/publishing pipeline order (safety → publish), just removes journey break.
  const publishFromPrompt = async () => {
    const report = await checkSafety();
    if (report?.actions.publish) await publish(report);
    setAskPublish(false);
    setFeedbackPrefillApplied(false);
  };

  const changeResourceStatus = async (status: "enabled" | "disabled") => {
    if (!detail || busy) return;
    if (dirty) {
      setError(t("release_save_before_resource_action"));
      return;
    }
    setBusy("status");
    try {
      await setManagedAgentResourceStatus(agentId, status, detail.aggregate_row_version);
      setStatusMessage(t(status === "enabled" ? "release_agent_enabled" : "release_agent_disabled"));
      await load();
    } catch (cause) {
      setError(errorText(cause, t("action_failed")));
    } finally {
      setBusy(null);
    }
  };

  const changeForkPolicy = async (policy: AgentForkSettings["policy"]["policy"]) => {
    if (!forkSettings || busy) return;
    setBusy("fork-policy");
    try {
      const next = await updateAgentForkPolicy(
        agentId,
        policy,
        forkSettings.policy.row_version,
      );
      setForkSettings((current) => current ? {
        ...current,
        policy: {
          policy: next.policy,
          row_version: next.row_version,
          updated_at: next.updated_at,
        },
      } : current);
      setStatusMessage(t("fork_policy_saved"));
      setError(null);
    } catch (cause) {
      setError(errorText(cause, t("fork_policy_failed")));
    } finally {
      setBusy(null);
    }
  };

  const archive = async () => {
    if (!detail || busy) return;
    if (dirty) {
      setConfirmArchive(false);
      setError(t("release_save_before_resource_action"));
      return;
    }
    setBusy("archive");
    try {
      const result = await archiveAgent(agentId, detail.aggregate_row_version);
      setConfirmArchive(false);
      setStatusMessage(t("release_archived", { count: result.paused_schedule_count }));
      await load();
    } catch (cause) {
      setError(errorText(cause, t("release_archive_failed")));
    } finally {
      setBusy(null);
    }
  };

  const restore = async () => {
    if (!detail || busy) return;
    setBusy("restore");
    try {
      await restoreAgent(agentId, detail.aggregate_row_version);
      setStatusMessage(t("release_restored"));
      await load();
    } catch (cause) {
      setError(errorText(cause, t("release_restore_failed")));
    } finally {
      setBusy(null);
    }
  };

  const rollback = async (release: AgentReleaseHistoryItem) => {
    if (!detail || busy || detail.pending_candidate) return;
    setAskPublish(false);
    setFeedbackPrefillApplied(false);
    if (dirty) {
      setError(t("release_save_before_resource_action"));
      return;
    }
    setBusy(`rollback:${release.release_id}`);
    try {
      await createAgentRollback(
        agentId,
        release.release_id,
        detail.aggregate_row_version,
        detail.working_copy?.row_version ?? null,
      );
      setStatusMessage(t("release_rollback_ready", { version: release.release_sequence }));
      await load();
    } catch (cause) {
      setError(errorText(cause, t("release_rollback_failed")));
    } finally {
      setBusy(null);
    }
  };

  if (loading && !detail) {
    return <div className="am-page am-release-page" aria-busy="true"><div className="am-detail-loading" /></div>;
  }
  if (!detail || !draft || !catalog) {
    return <div className="am-page am-release-page"><AgentBreadcrumb href={returnHref} label={t("back_to_manage")} /><div className="am-notice" role="alert"><span>{error ?? t("release_load_failed")}</span><button onClick={() => void load()}>{t("retry")}</button></div></div>;
  }

  const archived = detail.aggregate.lifecycle_state === "archived";
  const selectedAudienceUsers = new Set(draft.userIds);
  const selectedAudienceGroups = new Set(draft.groupIds);
  const releaseLabel = t("version_task_validation");
  const taskItems: Array<{ id: AgentManagerTask; label: string; count?: number; marker?: boolean }> = [
    { id: "config", label: t("version_task_config"), count: pendingCount },
    ...(evalAvailability === "enabled" ? [{ id: "evaluate" as const, label: t("version_task_eval") }] : []),
    { id: "release", label: releaseLabel, marker: activeTask !== "release" && releaseResultUnseen(releaseResult, releaseSeen) },
  ];
  const goToTask = (task: AgentManagerTask, section: AgentConfigSection | null = null, anchor: string | null = null) => {
    if (hostedMode) { setActiveTask(task); return; }
    window.history.pushState(null, "", managerTaskUrl(task, section, anchor));
    setActiveTask(task);
    if (section) openSection(section);
    if (anchor) revealAnchor(anchor);
    else if (!section) window.requestAnimationFrame(() => document.getElementById(tabPanelId("agent-manager", task))?.focus({ preventScroll: true }));
  };
  const toggleSection = (section: AgentConfigSection) => setClosedSections((current) => {
    const next = new Set(current);
    if (next.has(section)) next.delete(section);
    else next.add(section);
    return next;
  });
  const dependencyBlocked = Boolean(missingTools.length || missingSkills.length || missingToolConnections.length);
  const sectionProblem: Record<AgentConfigSection, string | null> = {
    does: displayNameInvalid ? t("release_display_name_required") : missingPrimaryCapability ? t("release_primary_unavailable") : null,
    uses: !hasCapability ? t("release_capability_required")
      : dependencyBlocked ? t("release_dependency_stale")
        : audienceBlockedSkills.length ? t("release_personal_skill_private_only") : null,
    who: customAudienceInvalid ? t("release_custom_required") : outputSchemaInvalid ? t("release_output_schema_invalid") : null,
  };
  // Name only the kinds that are present ("2 Plugins · 4 Tools"), in the interface language, with the rule the
  // creation draft and receipt use (agentResourceCounts): a Plugin counts once, not as the Tools it brings, and the
  // Python Skill that picking Run code adds on its own is not a picked Skill.
  const pickedCounts = agentResourceCounts({
    tool_refs: (draft?.toolRefs ?? []).filter((ref) => !ref.startsWith("teammcp.")),
    skill_grants: (draft?.skillNames ?? []).map((name) => ({ skill_ref: { name } })),
    builtin_plugin_ids: [...selectedPluginIds],
  });
  const resourceCountLine = [
    pickedCounts.plugins ? t("version_config_count_plugins", { count: pickedCounts.plugins }) : null,
    pickedCounts.skills ? t("version_config_count_skills", { count: pickedCounts.skills }) : null,
    resourceCounts.connections ? t("version_config_count_connections", { count: resourceCounts.connections }) : null,
    pickedCounts.tools ? t("version_config_count_tools", { count: pickedCounts.tools }) : null,
  ].filter(Boolean).join(" · ") || t("version_config_count_none");
  const sectionSummary: Record<AgentConfigSection, string> = {
    does: [
      t(draft.systemPrompt.trim() ? "version_config_section_prompt_ready" : "version_config_section_prompt_empty"),
      selectedPrimaryCapability ? t("version_config_section_primary", { name: localizedCapabilityText(selectedPrimaryCapability, locale).name }) : null,
    ].filter(Boolean).join(t("version_list_separator")),
    uses: resourceCountLine,
    who: t(`release_audience_${draft.audienceMode}` as never),
  };
  const configSection = (section: AgentConfigSection, title: string, body: ReactNode, action?: ReactNode) => {
    const open = !closedSections.has(section);
    const problem = sectionProblem[section];
    const bodyId = `agent-config-${section}-body`;
    return (
      <section id={`agent-config-${section}`} className="am-config-disclosure" data-open={open} data-problem={problem ? "true" : undefined}>
        <header>
          <button type="button" aria-expanded={open} aria-controls={bodyId} onClick={() => toggleSection(section)}>
            {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
            <span><strong>{title}</strong><small className={problem ? "am-release-inline-error" : undefined}>{problem ?? sectionSummary[section]}</small></span>
          </button>
          {action}
        </header>
        <div id={bodyId} hidden={!open}>{body}</div>
      </section>
    );
  };

  const TaskStage = hostedMode ? HostedStage : TabPanel;
  // Coach works in Chat: open a new conversation with @Coach and the request staged in the composer, sent only by the user.
  const coachAvailable = !hostedMode && !archived && detail.actions.edit && versionAvailability === "enabled";
  // Unsaved edits ask first; `beforeOpen` runs only once the Owner has chosen to leave.
  const openCoachWith = async (text: string, beforeOpen?: () => Promise<void>) => {
    const name = draft.displayName || detail.aggregate.name;
    if (dirty && !await confirm(canvasCloseGuardOptions(tObject, name))) return;
    await beforeOpen?.();
    handoffAgentToNewChat({ agentId: COACH_AGENT_ID, label: t("system_agent_coach_name"), text });
  };
  const askCoach = coachAvailable
    ? (failed: string[] = []) => {
      const name = draft.displayName || detail.aggregate.name;
      void openCoachWith(failed.length
        ? t("version_ask_coach_fix_prefill", { name, failed: failed.join(t("version_list_separator")) })
        : t("version_ask_coach_prefill", { name }));
    }
    : null;
  const workbenchPanel = (
    <AgentVersionWorkbenchPanel
        // The workbench probes its availability once on mount (an archived Agent answers 404), so
        // remount it when the lifecycle changes; otherwise Restore leaves its tabs hidden until a reload.
        key={detail.aggregate.lifecycle_state}
        agentId={agentId}
        agentName={draft.displayName || detail.aggregate.name}
        view={hostedMode ? "canvas" : activeTask === "config" ? "proposals" : activeTask === "release" ? "validation" : activeTask === "evaluate" ? "evaluate" : "hidden"}
        coachSlot={coachSlot}
        workingCopy={detail.working_copy}
        released={detail.live}
        liveActivatedAt={detail.live.activated_at}
        onAvailability={setVersionAvailability}
        onRecommendedTask={openRecommendedTask}
        onPendingCount={setPendingCount}
        onResultWaiting={setReleaseResult}
        onChanged={load}
        onError={handleVersionError}
        onOpenConfig={() => goToTask("config")}
        onOpenEvaluate={evalAvailability === "enabled" ? () => goToTask("evaluate") : undefined}
        onOpenHistory={hostedMode ? undefined : () => goToTask("release", null, AGENT_HISTORY_ANCHOR)}
        onOpenRelease={hostedMode ? undefined : () => goToTask("release")}
        editorDirty={dirty}
        readOnly={Boolean(hosted?.readOnly)}
        onOpenSection={openSection}
        onAskCoach={askCoach ? (failed) => askCoach(failed) : undefined}
        onImproveWithCoach={coachAvailable ? openCoachWith : undefined}
        suitesRefreshKey={suitesRefreshKey}
        onAddCases={activeTask === "evaluate" && !archived ? () => setAddCaseRequest((value) => value + 1) : undefined}
        onTestState={activeTask === "evaluate" ? setEvaluateTestState : undefined}
    />
  );

  return (
    <div className="am-page am-release-page">
      {navigationDialog}
      <fieldset disabled={Boolean(hosted?.readOnly || hostedMode && busy)} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
      {!hostedMode ? <AgentBreadcrumb href={returnHref} label={t("back_to_manage")} /> : null}
      <p className="sr-only" role="status" aria-live="polite">{statusMessage}</p>
      {!hostedMode ? (
      <header className="am-release-hero">
          <span className="am-management-mark" style={agentArtworkStyle(`${agentId}:${draft.displayName}`)}>{agentMark(draft.displayName)}</span>
          <div>
            <p>{archived ? t("release_manager_eyebrow_archived", { version: detail.live.release_sequence }) : t("release_manager_eyebrow", { version: detail.live.release_sequence, audience: t(`release_audience_${detail.live.audience.effective_mode}` as never) })}</p>
            <h1>{draft.displayName}</h1>
            <div className="am-release-state-row">
              <Badge tone={archived ? "warning" : detail.aggregate.status === "enabled" ? "positive" : "neutral"}>
                {t(archived ? "release_state_archived" : detail.aggregate.status === "enabled" ? "release_state_live" : "release_state_disabled")}
              </Badge>
              {detail.working_copy ? <Badge tone="neutral">{t(draft.audienceMode === "private" ? "release_state_draft_private" : "release_state_draft")}</Badge> : null}
              {detail.pending_candidate ? <Badge tone="warning">{t("release_state_pending")}</Badge> : null}
            </div>
          </div>
          <div ref={resourceActionsRef} className="am-release-resource-actions">
            {detail.actions.restore ? <Button variant="primary" disabled={busy !== null} onClick={() => void restore()}>{t("release_restore_agent")}</Button> : null}
            <RowActionsMenu
              label={t("release_agent_more", { name: draft.displayName })}
              busy={busy === "status" || busy === "archive"}
              actions={[
                ...(detail.actions.disable ? [{ id: "disable", label: t("release_disable_agent"), onSelect: () => void changeResourceStatus("disabled") }] : []),
                ...(detail.actions.enable ? [{ id: "enable", label: t("release_enable_agent"), onSelect: () => void changeResourceStatus("enabled") }] : []),
                ...(detail.actions.archive ? [{ id: "archive", label: t("release_archive_agent"), danger: true, onSelect: () => setConfirmArchive(true) }] : []),
              ]}
            />
          </div>
        </header>
      ) : null}

      {error ? <div ref={errorBannerRef} className="am-notice" role="alert"><span style={{ whiteSpace: "pre-line" }}>{error}</span><button onClick={() => setError(null)}>{t("dismiss")}</button></div> : null}
      {error && versionErrorCause?.message === error ? <ErrorDiagnosticsInline error={versionErrorCause.cause} surface="settings" /> : null}
      <PluginUpdateNotice
        updates={detail.live.plugin_updates}
        skillUpdates={detail.live.skill_updates}
        onReevaluate={evalAvailability === "enabled" && !archived ? () => goToTask("evaluate") : undefined}
      />
      {askPublish && detail.working_copy && detail.actions.publish && !detail.pending_candidate && !dirty ? (
        <section className="am-release-impact" role="group" aria-labelledby="am-ask-publish-title" data-testid="feedback-ask-publish" aria-busy={busy !== null}>
          <div>
            <strong id="am-ask-publish-title">{busy === "safety" || busy === "publish" ? fp("ask_publish_progress") : fp("ask_publish_title")}</strong>
            <p>{busy === "safety" ? t("release_checking") : busy === "publish" ? t("release_publishing") : fp(draft.audienceMode === "private" ? "ask_publish_private" : "ask_publish_shared")}</p>
          </div>
          <div>
            <button disabled={busy !== null} onClick={() => { setAskPublish(false); setFeedbackPrefillApplied(false); }}>{fp("ask_publish_later")}</button>
            <button className="am-action-primary" disabled={busy !== null} onClick={() => void publishFromPrompt()}>{fp(draft.audienceMode === "private" ? "ask_publish_now" : "ask_publish_submit")}</button>
          </div>
        </section>
      ) : null}
      {confirmArchive ? (
        <section ref={archiveConfirmationRef} tabIndex={-1} className="am-release-impact" role="group" aria-labelledby="am-archive-title">
          <div><strong id="am-archive-title">{t("release_archive_title")}</strong><p>{t("release_archive_body")}</p></div>
          <div><button disabled={busy !== null} onClick={() => {
            setConfirmArchive(false);
            // Back to the header "⋯" that opened the confirmation.
            resourceActionsRef.current?.querySelector<HTMLElement>(".ui-row-more")?.focus();
          }}>{t("cancel")}</button><button className="am-danger-confirm" disabled={busy !== null} onClick={() => void archive()}>{t("release_confirm_archive")}</button></div>
        </section>
      ) : null}
      {detail.pending_candidate ? (
        <section className="am-release-pending">
          <div><strong>{t("release_pending_title")}</strong><p>{t("release_pending_body", { version: detail.live.release_sequence })}</p></div>
          <a href={appPath(`/agents/manage/?release_candidate=${encodeURIComponent(detail.pending_candidate.candidate_id)}&agent_id=${encodeURIComponent(agentId)}`)}>{t("release_open_candidate")}</a>
        </section>
      ) : null}

      <Tabs value={activeTask} onValueChange={(task) => goToTask(task)} idBase="agent-manager">
      {!hostedMode ? <div className="am-task-bar">
        <TabList appearance="underline" className="am-release-task-nav" aria-label={t("version_task_navigation")}>
          {taskItems.map((item) => <Tab key={item.id} value={item.id}>{item.label}{item.count ? <span className="am-task-count" aria-label={t("version_task_pending_count", { count: item.count })}>{item.count}</span> : null}{item.marker ? <span className="am-task-marker" role="img" aria-label={t("version_task_result_waiting")} /> : null}</Tab>)}
        </TabList>
        {askCoach ? <Button variant="secondary" size="sm" onClick={() => askCoach()}><Bot size={14} aria-hidden />{t("version_ask_coach_action")}</Button> : null}
      </div> : null}

      {/* The canvas has no tab list, so its stage is a plain region rather than a labelled tab panel. */}
      <TaskStage value={activeTask} className="am-release-task-stage" tabIndex={-1}>
      {/* The canvas puts Coach's changes first and the release check last, around the configuration. */}
      {hostedMode ? <div ref={setCoachSlot} /> : workbenchPanel}
      {activeTask === "config" ? <>
        {!hostedMode && archived ? <header className="am-release-task-heading">
          {/* Archived Agents are history-only: a read-only heading instead of the draft editor. */}
          <span>{t("version_config_eyebrow_archived")}</span><h2>{t("version_config_title_archived")}</h2><p>{t("release_archived_edit_note")}</p>
        </header> : null}
        <div className="am-release-layout">
          <div id="agent-config-view" className="am-release-editor" aria-disabled={archived} tabIndex={-1}>
            {configSection("does", t("version_config_section_does"), <>
              <section className="am-release-section am-config-prompt-section">
                <header><div><h2>{t("release_instructions_title")}</h2><p>{t("release_instructions_body")}</p></div></header>
                {feedbackPrefill && !archived ? (
                  <div className="am-release-field-wide">
                    <FeedbackPrefillPanel
                      handoff={feedbackPrefill}
                      baseline={draft.systemPrompt}
                      stale={feedbackPrefillStale}
                      realigning={realigning}
                      onRealign={() => void realignFeedbackPrefill()}
                      onApply={(edited) => { update({ systemPrompt: edited }); setFeedbackPrefillApplied(true); setFeedbackPrefill(null); clearFeedbackPrefill(); }}
                      onDiscard={() => { setFeedbackPrefill(null); clearFeedbackPrefill(); }}
                    />
                  </div>
                ) : null}
                <label className="am-release-field-wide"><span>{t("release_system_prompt")}</span><textarea aria-label={t("release_system_prompt")} name="agent-system-prompt" autoComplete="off" className="am-release-prompt" maxLength={MAX_PROMPT_CHARS_TEAM} rows={14} disabled={archived} value={draft.systemPrompt} onChange={(event) => update({ systemPrompt: event.target.value })} /><PromptBudgetHint text={draft.systemPrompt} t={t} /></label>
                <div className="am-release-form-grid am-release-compact-fields">
                  <label><span>{t("release_iterations")}</span><input name="agent-max-iterations" autoComplete="off" type="number" min={1} max={50} disabled={archived} value={draft.maxIterations} onChange={(event) => update({ maxIterations: Math.max(1, Math.min(50, Number(event.target.value) || 1)) })} /></label>
                  <label className="am-release-field-wide am-release-toggle-field"><input name="agent-capability-protocol-block" type="checkbox" disabled={archived} checked={draft.capabilityProtocolBlock} onChange={(event) => update({ capabilityProtocolBlock: event.target.checked })} /><span><strong>{t("release_capability_protocol_block")}</strong><small>{t("release_capability_protocol_block_help")}</small></span></label>
                  {catalog.model_policy.locked || catalog.model_policy.allowed_roles.length <= 1 ? (
                    <div className="am-release-policy-field"><span>{t("release_model_role")}</span><strong>{draft.model === "analyst" ? t("release_model_analyst") : draft.model}</strong><small>{t("release_model_policy_locked")}</small></div>
                  ) : (
                    <label><span>{t("release_model_role")}</span><select name="agent-model-role" autoComplete="off" disabled={archived} value={draft.model} onChange={(event) => update({ model: event.target.value })}>{catalog.model_policy.allowed_roles.map((role) => <option key={role} value={role}>{role === "analyst" ? t("release_model_analyst") : role}</option>)}</select></label>
                  )}
                </div>
              </section>
              <section className="am-release-section">
                <header><div><h2>{t(draft.audienceMode === "private" ? "release_listing_title_private" : "release_listing_title")}</h2><p>{t(draft.audienceMode === "private" ? "release_listing_body_private" : "release_listing_body")}</p></div></header>
                <div className="am-release-form-grid">
                  <label><span>{t("release_display_name")}</span><input name="agent-display-name" autoComplete="off" maxLength={40} disabled={archived} value={draft.displayName} onChange={(event) => update({ displayName: event.target.value })} />{displayNameInvalid ? <small className="am-release-inline-error">{t("release_display_name_required")}</small> : null}</label>
                  <label className="am-release-field-wide"><span>{t("release_description")}</span><textarea aria-label={t("release_description")} name="agent-description" autoComplete="off" maxLength={200} rows={3} disabled={archived} value={draft.description} onChange={(event) => update({ description: event.target.value })} /><small>{draft.description.length}/200</small></label>
                </div>
              </section>
              <details
                className="am-release-section am-config-advanced"
                open={advancedOpen ?? (sharedAgent && (draft.primaryCapabilityRef !== null || willRequireReview))}
                onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
              >
                {/* Outcome is rarely needed for a private Agent: kept, but folded (agent-release-beginner-experience.md Item 4). */}
                <summary>
                  <span>{t("release_advanced_title")}</span>
                  <span className="am-config-advanced-summary">
                    {selectedPrimaryCapability
                      ? t("release_advanced_summary_primary", { name: localizedCapabilityText(selectedPrimaryCapability, locale).name })
                      : t("release_advanced_summary_optional")}
                  </span>
                  {missingPrimaryCapability ? <Badge size="sm" tone="warning">{t("release_advanced_needs_attention")}</Badge> : null}
                </summary>
                <div className="am-config-advanced-body">
                {sharedAgent ? <p className="am-config-advanced-note" role="note">{t("release_advanced_reviewer_note")}</p> : null}
                <header><div><h3>{t("release_primary_title")}</h3><p>{t(sharedAgent ? "release_primary_body" : "release_primary_body_private")}</p></div></header>
                {missingPrimaryCapability ? <div className="am-release-stale-list"><strong>{t("release_primary_unavailable")}</strong><label><input type="radio" name="agent-primary-capability" checked disabled /><span translate="no">{draft.primaryCapabilityRef}</span><small>{t("release_primary_clear_to_save")}</small></label></div> : null}
                <fieldset className="am-release-primary-grid">
                  <legend className="sr-only">{t("release_primary_title")}</legend>
                  <label data-selected={draft.primaryCapabilityRef === null}>
                    <input type="radio" name="agent-primary-capability" checked={draft.primaryCapabilityRef === null} disabled={archived} onChange={() => update({ primaryCapabilityRef: null })} />
                    <span><strong>{t("release_primary_none")}</strong><small>{t("release_primary_none_body")}</small><em>{t("release_primary_optional")}</em></span>
                  </label>
                  {selectablePrimaryCapabilities.map((capability) => {
                    const copy = localizedCapabilityText(capability, locale);
                    const selected = draft.primaryCapabilityRef === capability.exact_ref;
                    return (
                      <label key={capability.exact_ref} data-selected={selected} data-disabled={!capability.selectable}>
                        <input type="radio" name="agent-primary-capability" checked={selected} disabled={archived || !capability.selectable} onChange={() => update({ primaryCapabilityRef: capability.exact_ref })} />
                        <span><strong>{copy.name}</strong><small>{copy.description}</small><em>{capability.selectable ? t("release_primary_verified") : t(`release_primary_${capability.verification_state}` as never)}</em></span>
                      </label>
                    );
                  })}
                </fieldset>
                {!catalog.primary_capabilities.some((capability) => capability.selectable) ? (
                  <div className="am-config-empty"><strong>{t("version_config_capability_empty")}</strong><p>{t("release_primary_catalog_pending")}</p><button type="button" className="am-config-inline-link" onClick={() => openSection("uses")}>{t("version_config_capability_configure_resources")}</button></div>
                ) : null}
                {unavailablePrimaryCapabilities.length ? (
                  <details className="am-config-unavailable">
                    <summary>{t("version_config_capability_unavailable", { count: unavailablePrimaryCapabilities.length })}</summary>
                    <p>{t("version_config_capability_unavailable_body")}</p>
                    <div className="am-release-primary-grid">
                      {unavailablePrimaryCapabilities.map((capability) => { const copy = localizedCapabilityText(capability, locale); return <label key={capability.exact_ref} data-disabled="true"><input type="radio" name="unavailable-agent-primary-capability" disabled /><span><strong>{copy.name}</strong><small>{copy.description}</small><em>{t(`release_primary_${capability.verification_state}` as never)}</em></span></label>; })}
                    </div>
                  </details>
                ) : null}
                </div>
              </details>
            
            </>)}
            {configSection("uses", t("version_config_section_uses"), <>
              <section className="am-release-section am-config-resources">
                <header><div><h2>{t("version_config_resources_title")}</h2><p>{t("release_capabilities_body")}</p><p>{t("release_skill_scope_body")}</p></div></header>
                <section className="am-config-capability-panel" aria-labelledby={`${agentId}-capabilities-heading`}>
                  <header><div><h3 id={`${agentId}-capabilities-heading`}>{t("version_config_capability_bundles")}</h3><p>{t("version_config_capability_auto_update")}</p></div></header>
                  <div className="am-config-resource-guide" role="note"><strong>{pickerT("guide_title")}</strong><ul><li>{pickerT("concept_tool")}</li><li>{pickerT("concept_skill")}</li><li>{pickerT("concept_plugin")}</li></ul></div>
                  {pickerPlugins.length ? <ResourcePluginPicker plugins={pickerPlugins} selected={selectedPluginIds} canManage={pluginCanManage} disabled={archived} onToggle={togglePlugin} /> : <div className="am-release-picker-grid">
                    {teamPlugins.map((plugin) => <label key={plugin.id} data-selected={draft.pluginIds.includes(plugin.id)}>
                      <input type="checkbox" checked={draft.pluginIds.includes(plugin.id)} disabled={archived || (plugin.status !== "active" && !draft.pluginIds.includes(plugin.id))} onChange={() => update({ pluginIds: draft.pluginIds.includes(plugin.id) ? draft.pluginIds.filter((id) => id !== plugin.id) : [...draft.pluginIds, plugin.id] })} />
                      <span><strong>{plugin.name}</strong><small>{plugin.description}</small><em>{t("version_config_capability_resources", { count: plugin.tool_refs.length + plugin.skill_grants.length })}</em></span>
                    </label>)}
                  </div>}
                  {pickerPlugins.length ? <ResourceSummary {...resourceSummary} /> : null}
                </section>
                {!hostedMode ? <PersonalMcpAgentBindings agentId={agentId} /> : null}
                {!hasCapability ? <p className="am-release-inline-error">{t("release_capability_required")}</p> : null}
                {missingTools.length || missingSkills.length || missingToolConnections.length ? <p className="am-release-inline-error">{t("release_dependency_stale")}</p> : null}
                {audienceBlockedSkills.length ? <p className="am-release-inline-error">{t("release_personal_skill_private_only")}</p> : null}
                <div className="am-config-selected-resources">
                  <div className="am-release-subsection-heading am-release-responsibility-heading"><h3>{t("version_config_resources_selected")}</h3><p>{t("version_config_resources_selected_body", resourceCounts)}</p></div>
                  {missingTools.length ? <div className="am-release-stale-list"><strong>{t("release_unavailable_tools")}</strong>{missingTools.map((ref) => <label key={ref}><input name="agent-tool" type="checkbox" checked disabled={archived} onChange={() => toggle("toolRefs", ref)} /><span>{ref}</span><small>{t("release_remove_to_publish")}</small></label>)}</div> : null}
                  {missingToolConnections.length ? <div className="am-release-stale-list"><strong>{t("version_config_unavailable_connections")}</strong>{missingToolConnections.map((id) => <label key={id}><input name="agent-tool-connection" type="checkbox" checked disabled={archived} onChange={() => update({ toolConnectionIds: draft.toolConnectionIds.filter((value) => value !== id) })} /><span>{id}</span><small>{t("release_remove_to_publish")}</small></label>)}</div> : null}
                  {missingSkills.length ? <div className="am-release-stale-list"><strong>{t("release_unavailable_skills")}</strong>{missingSkills.map((name) => <label key={name}><input name="agent-skill" type="checkbox" checked disabled={archived} onChange={() => toggleSkill(name)} /><span>{name}</span><small>{t("release_remove_to_publish")}</small></label>)}</div> : null}
                  {selectedToolConnections.length ? <div className="am-tool-connection-grid">{selectedToolConnections.map((connection) => <ToolConnectionCard key={connection.id} group={connection} selected disabled={archived} capabilityLabel={toolConnectionSummary(connection)} detailsLabel={t("version_config_connection_details")} typeLabel={t("release_team_connection")} removeHint={t("version_config_resource_remove_hint")} onToggle={() => toggleToolConnection(connection)} />)}</div> : null}
                  {selectedTools.length || selectedSkills.length ? <div className="am-release-picker-grid am-config-selected-exact-resources">
                    {selectedSkills.map((skill) => {
                      const linkedToolNames = skill.linked_tools.map((ref) => { const hit = toolsByRef.get(ref); return hit ? localizedToolText(hit).name : ref; });
                      const requiredSkillNames = (skill.required_skills ?? []).map((name) => { const required = skillsByName.get(name); return required ? authoringSkillTitle(required) : name; });
                      const requiredByNames = selectedSkills.filter((candidate) => (candidate.required_skills ?? []).includes(skill.name)).map((candidate) => authoringSkillTitle(candidate));
                      return <label key={`selected-skill:${skill.name}`} data-selected="true"><input name="agent-skill" type="checkbox" checked disabled={archived || requiredByNames.length > 0} onChange={() => toggleSkill(skill.name)} /><span><strong>{authoringSkillTitle(skill, locale)}</strong><small>{authoringSkillDescription(skill, locale) || t("release_skill_fallback")}</small>{requiredSkillNames.length ? <small>{t("release_required_skills", { skills: listFormatter.format(requiredSkillNames) })}</small> : null}{requiredByNames.length ? <small>{t("release_required_by_skills", { skills: listFormatter.format(requiredByNames) })}</small> : null}{linkedToolNames.length ? <small>{t("release_linked_tools", { tools: listFormatter.format(linkedToolNames) })}</small> : null}{skill.script_runtime === "disabled" ? <small role="note" data-testid="skill-scripts-disabled">{t("release_skill_scripts_disabled")}</small> : null}<em>Skill · {requiredByNames.length ? t("release_dependency_in_use") : t("version_config_resource_remove_hint")}</em></span></label>;
                    })}
                    {selectedTools.map((tool) => <label key={`selected-tool:${tool.stable_ref}`} data-selected="true"><input name="agent-tool" type="checkbox" checked disabled={archived} onChange={() => toggle("toolRefs", tool.stable_ref)} /><span><strong>{localizedToolText(tool).name}</strong><small>{localizedToolText(tool).description}</small><em>Tool · {t(`release_risk_${tool.risk}` as never)}{!tool.selectable ? ` · ${toolT(toolSelectionLockedReasonKey(tool.locked_reason ?? ""))}` : ""} · {t("version_config_resource_remove_hint")}</em></span></label>)}
                  </div> : pickedPlugins.length ? <p className="am-ci-note">{t("version_config_resources_from_plugins", { names: listFormatter.format(pickedPlugins.map((plugin) => plugin.name)) })}</p>
                    : !hasResources ? <div className="am-config-empty"><strong>{t("version_config_resources_none")}</strong><p>{t("version_config_resources_none_body")}</p></div> : null}
                </div>

              </section>
            
            </>, !archived ? <Button variant="secondary" size="sm" onClick={() => { if (closedSections.has("uses")) toggleSection("uses"); setAddResourcesOpen(true); }}>{t("version_config_resources_add_action")}</Button> : undefined)}
            {configSection("who", t("version_config_section_who"), <>
              <section className="am-release-section">
                <header><div><h2>{t("release_audience_title")}</h2><p>{t("release_audience_body")}</p></div></header>
                <fieldset className="am-release-segments"><legend className="sr-only">{t("release_audience_title")}</legend>{(["private", "custom", "teamwide"] as const).map((mode) => <label data-selected={draft.audienceMode === mode} key={mode}><input type="radio" name="agent-audience-mode" value={mode} checked={draft.audienceMode === mode} disabled={archived} onChange={() => update({ audienceMode: mode })} /><span>{t(`release_audience_${mode}` as never)}</span></label>)}</fieldset>
                <p className="am-release-audience-explanation">{t(`release_audience_${draft.audienceMode}_body` as never)}</p>
                {draft.audienceMode !== "private" ? <PersonalToolAudienceNotice agentId={agentId} /> : null}
                {draft.audienceMode === "custom" ? <div className="am-release-directory"><label><span>{t("release_find_people")}</span><input name="agent-audience-search" autoComplete="off" type="search" disabled={archived} value={directoryQuery} onChange={(event) => setDirectoryQuery(event.target.value)} placeholder={t("release_find_people_placeholder")} /></label>{customAudienceInvalid ? <p className="am-release-inline-error">{t("release_custom_required")}</p> : null}<div>{groups.map((group) => <label key={`group:${group.id}`}><input name="agent-audience-group" type="checkbox" checked={selectedAudienceGroups.has(group.id)} disabled={archived} onChange={() => toggle("groupIds", group.id)} /><span><strong>{group.name}</strong><small>{t("release_group_members", { count: group.member_count })}</small></span></label>)}{users.map((user) => <label key={`user:${user.id}`}><input name="agent-audience-user" type="checkbox" checked={selectedAudienceUsers.has(user.id)} disabled={archived} onChange={() => toggle("userIds", user.id)} /><span><strong>{user.display_name}</strong><small>{user.email_masked}</small></span></label>)}{[...draft.groupIds.filter((id) => !groups.some((group) => group.id === id)), ...draft.userIds.filter((id) => !users.some((user) => user.id === id))].map((id) => <label key={`unresolved:${id}`}><input name="agent-audience-principal" type="checkbox" checked disabled={archived} onChange={() => draft.groupIds.includes(id) ? toggle("groupIds", id) : toggle("userIds", id)} /><span><strong>{id}</strong><small>{t("release_selected_principal")}</small></span></label>)}</div></div> : null}
              </section>
              <section className="am-release-section"><AgentOptionalSkillsPanel key={agentId} agentId={agentId} value={draft.extensionFields.runtime_extensions} audience={draft.audienceMode} disabled={!detail.actions.edit || busy !== null} onChange={(runtime_extensions) => {
                const baseline = detail.working_copy ?? detail.live;
                update({
                  extensionFields: runtime_extensions === null && !Object.hasOwn(baseline.definition, "runtime_extensions")
                    ? {}
                    : { runtime_extensions },
                });
              }} /></section>
              <details className="am-release-advanced"><summary>{t("release_output_contract_title")}</summary><p>{t("release_output_contract_body")}</p><label><span>{t("release_output_contract_json")}</span><textarea name="agent-output-contract" autoComplete="off" spellCheck={false} className="am-release-prompt" rows={8} disabled={archived} value={draft.outputSchema} onChange={(event) => update({ outputSchema: event.target.value })} placeholder={'{\n  "type": "object"\n}'} />{outputSchemaInvalid ? <small className="am-release-inline-error">{t("release_output_schema_invalid")}</small> : null}</label></details>
            
              {!hostedMode ? <div className="am-config-market-stack">
              <p role="note">{t("version_config_market_not_listing")}</p>
              {detail.working_copy ? <AgentLocalizationPanel agentId={agentId} archived={archived} parentDirty={dirty} refreshToken={`${detail.working_copy.id}:${detail.working_copy.row_version}`} onLocalizationChanged={() => setSafety(null)} /> : <div className="am-config-empty"><strong>{t("version_config_market_save_first")}</strong><p>{t("version_config_market_save_first_body")}</p></div>}
              {detail.working_copy ? <AgentTaxonomyPanel agentId={agentId} archived={archived} parentDirty={dirty} refreshToken={`${detail.working_copy.id}:${detail.working_copy.row_version}`} onTaxonomyChanged={() => { setSafety(null); void load(); }} /> : null}
              {forkSettings ? (
                <section className="am-release-history am-fork-manager">
                  <header>
                    <span>{t("fork_manager_eyebrow")}</span>
                    <h2>{t("fork_manager_title")}</h2>
                    <p>{t("fork_manager_body")}</p>
                  </header>
                  <div className="am-fork-manager-explanation">
                    <strong>{t("fork_manager_explanation_title")}</strong>
                    <ul>
                      <li>{t("fork_manager_explanation_copy")}</li>
                      <li>{t("fork_manager_explanation_gaps")}</li>
                      <li>{t("fork_manager_explanation_independent")}</li>
                    </ul>
                  </div>
                  <div className="am-fork-manager-grid">
                    <div className="am-fork-policy-card">
                      <label>
                        <span>{t("fork_policy_label")}</span>
                        <select value={forkSettings.policy.policy} disabled={archived || busy !== null} onChange={(event) => void changeForkPolicy(event.target.value as AgentForkSettings["policy"]["policy"])}>
                          <option value="not_allowed">{t("fork_policy_not_allowed")}</option>
                          <option value="recipients">{t("fork_policy_recipients")}</option>
                          <option value="team_members">{t("fork_policy_team_members")}</option>
                        </select>
                      </label>
                      <p>{t(`fork_policy_${forkSettings.policy.policy}_body` as never)}</p>
                      <small>{t("fork_policy_future_only")}</small>
                    </div>
                    {forkSettings.lineage ? (
                      <div className="am-fork-lineage-card">
                        <span>{t("fork_lineage_label")}</span>
                        <h3>{forkSettings.lineage.source_agent_id ? <a href={appPath(`/agents/detail/?id=${encodeURIComponent(forkSettings.lineage.source_agent_id)}`)}>{forkSettings.lineage.source_display_name}</a> : forkSettings.lineage.source_display_name}</h3>
                        <p>{forkSettings.lineage.source_accessible ? t("fork_lineage_connected") : t("fork_lineage_tombstone")}</p>
                        <dl>
                          <div><dt>{t("fork_source_version")}</dt><dd>{forkSettings.lineage.source_revision_ref.slice(-8)}</dd></div>
                          <div><dt>{t("fork_upstream_status")}</dt><dd>{t(forkSettings.lineage.upstream.update_available ? "fork_update_available" : "fork_up_to_date")}</dd></div>
                          <div><dt>{t("fork_auto_update")}</dt><dd>{t("fork_never_auto_updates")}</dd></div>
                        </dl>
                        {forkSettings.lineage.upstream.comparison ? <p className="am-fork-comparison">{t("fork_change_summary", { tools: Number(forkSettings.lineage.upstream.comparison.upstream_changes.tools_added ?? 0) + Number(forkSettings.lineage.upstream.comparison.upstream_changes.tools_removed ?? 0), skills: Number(forkSettings.lineage.upstream.comparison.upstream_changes.skills_added ?? 0) + Number(forkSettings.lineage.upstream.comparison.upstream_changes.skills_removed ?? 0) })}{forkSettings.lineage.upstream.comparison.local_changed_since_fork ? ` · ${t("fork_local_changed")}` : ""}</p> : null}
                        {forkSettings.lineage.dependency_gaps.length ? <small>{t("fork_manager_gaps", { count: forkSettings.lineage.dependency_gaps.length })}</small> : null}
                      </div>
                    ) : (
                      <div className="am-fork-lineage-card">
                        <span>{t("fork_lineage_label")}</span>
                        <h3>{t("fork_original_agent")}</h3>
                        <p>{t("fork_original_agent_body")}</p>
                      </div>
                    )}
                  </div>
                </section>
              ) : null}
            </div> : null}
            </>)}
            {addResourcesOpen ? (
              <Dialog variant="drawer" title={t("version_config_resources_add")} onClose={() => setAddResourcesOpen(false)} closeLabel={t("cancel")}>
                <div className="am-config-resource-library">
                  <div className="am-release-subsection-heading"><h3>{t("version_config_resources_add")}</h3><p>{t("version_config_resources_add_body")}</p></div>
                  <label className="am-config-resource-search"><span>{t("version_config_search_all")}</span><input type="search" name="agent-resource-search" autoComplete="off" value={resourceQuery} onChange={(event) => { setResourceQuery(event.target.value); setResourceVisibleCount(8); }} placeholder={t("version_config_search_all_placeholder")} /></label>
                  {libraryEmpty ? <div className="am-config-empty" role="status"><strong>{pickerT("no_match_title", { query: resourceQuery.trim() })}</strong><p>{pickerT("no_match_body")}</p></div> : <>
                  {libraryPlugins.length ? <>
                    <div className="am-config-library-heading"><span>{t("version_config_available_plugins", { count: libraryPlugins.length })}</span><CapabilityLink href="/capabilities/?type=capabilities">{navT("capabilities_catalog")}</CapabilityLink></div>
                    <ResourcePluginPicker plugins={libraryPlugins} selected={selectedPluginIds} canManage={pluginCanManage} disabled={archived} onToggle={togglePlugin} />
                  </> : null}
                  {availableToolConnections.length ? <><div className="am-config-library-heading"><span>{t("version_config_available_connections", { count: availableToolConnections.length })}</span><CapabilityLink href="/capabilities/?type=tools">{navT("tools_catalog")}</CapabilityLink></div>
                    <div className="am-tool-connection-grid">{availableToolConnections.map((connection) => <ToolConnectionCard key={connection.id} group={connection} selected={false} disabled={archived || !connection.selectable} capabilityLabel={toolConnectionSummary(connection)} detailsLabel={t("version_config_connection_details")} typeLabel={t("release_team_connection")} removeHint={t("version_config_resource_remove_hint")} onToggle={() => toggleToolConnection(connection)} />)}</div></> : null}
                    {availableTools.length ? <><div className="am-config-library-heading am-config-native-tool-heading"><span>{t("version_config_available_tools", { count: availableTools.length })}</span></div>
                    <ResourceToolPicker
                      tools={availableTools.map((tool) => ({
                        ref: tool.stable_ref,
                        name: localizedToolText(tool).name,
                        description: localizedToolText(tool).description,
                        selectable: tool.selectable,
                        riskLabel: `${t(`release_risk_${tool.risk}` as never)} · ${t("release_bowhead_tool")}`,
                        lockedLabel: tool.selectable ? null : toolT(toolSelectionLockedReasonKey(tool.locked_reason ?? "")),
                      }))}
                      plugins={pickerPlugins}
                      selected={new Set(draft.toolRefs)}
                      selectedPluginIds={selectedPluginIds}
                      disabled={archived}
                      onToggle={(ref) => toggle("toolRefs", ref)}
                    /></> : null}
                    {availableSkills.length ? <><div className="am-config-library-heading"><span>{t("version_config_available_skills", { count: availableSkills.length })}</span><CapabilityLink href="/skills/">{t("version_config_manage_skills")}</CapabilityLink></div>
                    <div className="am-release-picker-grid">{visibleAvailableSkills.map((skill) => {
                      const audienceAllowed = skill.allowed_audiences.includes(draft.audienceMode);
                      const audienceReasonId = `${skillReasonId}-${encodeURIComponent(skill.name)}`;
                      const linkedToolNames = skill.linked_tools.map((ref) => { const hit = toolsByRef.get(ref); return hit ? localizedToolText(hit).name : ref; });
                      const requiredSkillNames = (skill.required_skills ?? []).map((name) => { const required = skillsByName.get(name); return required ? authoringSkillTitle(required) : name; });
                      const dependenciesAvailable = (skill.required_skills ?? []).every((name) => skillsByName.has(name));
                      return <label key={skill.name} data-disabled={!audienceAllowed || !dependenciesAvailable}><input name="agent-skill" type="checkbox" checked={false} aria-describedby={!audienceAllowed ? audienceReasonId : undefined} disabled={archived || !audienceAllowed || !dependenciesAvailable} onChange={() => toggleSkill(skill.name)} /><span><strong>{authoringSkillTitle(skill, locale)}</strong><small>{authoringSkillDescription(skill, locale) || t("release_skill_fallback")}</small>{requiredSkillNames.length ? <small>{t("release_required_skills", { skills: listFormatter.format(requiredSkillNames) })}</small> : null}{linkedToolNames.length ? <small>{t("release_linked_tools", { tools: listFormatter.format(linkedToolNames) })}</small> : null}{skill.script_runtime === "disabled" ? <small role="note">{t("release_skill_scripts_disabled")}</small> : null}<em>{skill.source_kind === "team" ? t("release_team_skill") : skill.source_kind === "user" ? t("release_personal_skill") : t("release_bowhead_skill")}{!audienceAllowed ? ` · ${t("release_private_only")}` : !dependenciesAvailable ? ` · ${t("release_skill_dependency_unavailable_short")}` : ""}</em>{!audienceAllowed ? <small id={audienceReasonId}>{t("release_skill_audience_reason", { audience: t(`release_audience_${draft.audienceMode}`) })}</small> : null}</span></label>;
                    })}</div>{availableSkills.length > resourceVisibleCount ? <button className="am-config-show-more" type="button" onClick={() => setResourceVisibleCount((count) => count + 8)}>{t("version_config_show_more", { count: availableSkills.length - resourceVisibleCount })}</button> : null}</> : null}
                  </>}
                </div>
              </Dialog>
            ) : null}
          </div>

          {hostedMode ? (
            <SaveBar
              dirty={dirty}
              saving={busy === "save"}
              failed={Boolean(error) && dirty}
              guard={false}
              saveDisabled={Boolean(hosted?.readOnly) || archived || !needsSave || !hasCapability || missingPrimaryCapability || customAudienceInvalid || optionalAudienceInvalid || displayNameInvalid || outputSchemaInvalid || Boolean(missingTools.length || missingSkills.length || missingToolConnections.length || audienceBlockedSkills.length)}
              onSave={() => void save()}
              onDiscard={() => {
                void (async () => {
                  if (!await confirm(canvasCloseGuardOptions(tObject, draft.displayName))) return;
                  if (detail && catalog) setDraft(editorDraft(detail, catalog, teamPlugins));
                })();
              }}
            />
          ) : null}
          {/* Archived Agents are read-only: no "Next draft / Save configuration" card (the heading carries the note). */}
          {!hostedMode && !archived ? <aside className="am-release-sidebar">
            <section className="am-release-publish-card">
              <span>{t("version_config_save_eyebrow")}</span><h2>{t("version_config_save_title")}</h2>
              <dl className="am-config-summary"><div><dt>{t("version_config_summary_prompt")}</dt><dd>{t(draft.systemPrompt.trim() ? "version_config_summary_ready" : "version_config_summary_empty")}</dd></div>{selectedPrimaryCapability || missingPrimaryCapability ? <div><dt>{t("version_config_summary_outcome")}</dt><dd>{selectedPrimaryCapability ? localizedCapabilityText(selectedPrimaryCapability, locale).name : t("release_advanced_needs_attention")}</dd></div> : null}<div><dt>{t("version_config_summary_uses")}</dt><dd>{resourceCountLine}</dd></div><div><dt>{t("version_config_summary_audience")}</dt><dd>{t(`release_audience_${draft.audienceMode}` as never)}</dd></div></dl>
              {archived ? <p>{t("release_archived_edit_note")}</p> : <><p>{t(dirty ? "version_config_unsaved" : detail.working_copy ? "version_config_saved" : "version_config_no_draft", { version: detail.live.release_sequence })}</p>{!hasCapability ? <button type="button" className="am-config-blocker-link" onClick={() => openSection("uses")}>{t("release_capability_required")}</button> : null}<button className="am-release-wide-button" disabled={busy !== null || !needsSave || !hasCapability || missingPrimaryCapability || customAudienceInvalid || optionalAudienceInvalid || displayNameInvalid || outputSchemaInvalid || Boolean(missingTools.length || missingSkills.length || missingToolConnections.length || audienceBlockedSkills.length)} onClick={() => void save()}>{busy === "save" ? t("release_saving") : t("release_save_draft")}</button></>}
            </section>
          </aside> : null}
        </div>
      </> : null}

      {hostedMode ? workbenchPanel : null}
      {activeTask === "release" ? (
        <>
          {archived ? <p className="am-ci-note am-release-archived-note">{t("release_archived_edit_note")}</p> : null}
          {!archived && versionAvailability === "disabled" ? (
            <section className="am-ci" aria-labelledby="agent-release-legacy-title">
              <div className="am-ci-card">
                <div className="am-ci-gt">
                  <h3 id="agent-release-legacy-title">{t(draft.audienceMode === "private" ? "version_workbench_title_private" : "release_publish_title")}</h3>
                </div>
                <ol className="am-release-steps">
                  <li data-done={!dirty && Boolean(detail.working_copy)}>{t("release_step_save")}</li>
                  <li data-done={Boolean(safety)}>{t("release_step_safety")}</li>
                  <li data-done={Boolean(detail.pending_candidate) || !detail.working_copy}>{t(draft.audienceMode === "private" ? "release_step_apply_private" : "release_step_publish")}</li>
                </ol>
                <div className="am-ci-fld">
                  <label htmlFor="agent-release-note-legacy">{t("release_note")}</label>
                  <textarea id="agent-release-note-legacy" className="settings-input" name="agent-release-note" autoComplete="off" rows={3} maxLength={2000} value={releaseNote} onChange={(event) => setReleaseNote(event.target.value)} placeholder={t("release_note_placeholder")} />
                </div>
                {needsSave ? <p className="am-ci-note">{t(dirty ? "version_release_save_first" : "version_release_no_draft")}</p> : null}
                {safety ? (
                  <div className="am-ci-ist" data-tone={safety.status === "pass" ? "success" : safety.status === "blocked" ? "error" : "info"}>
                    <span className="am-ci-ist-text" role="status"><strong>{t("release_safety_status", { status: localize(`release_safety_${safety.status}`, safety.status) })}</strong> {safety.summary.reason_codes.length ? [...new Set(safety.summary.reason_codes.map((code) => t(agentReleaseReasonKey(code) as never)))].join(t("version_list_separator")) : t("release_safety_no_findings")}</span>
                  </div>
                ) : null}
                <p className="am-ci-note">{t(draft.audienceMode === "private" ? "release_publish_private_note" : willRequireReview ? "release_publish_team_note" : "release_publish_custom_note")}</p>
                <div className="am-ci-acts">
                  <Button variant={safety?.actions.publish ? "secondary" : "primary"} disabled={busy !== null || dirty || !detail.working_copy} loading={busy === "safety"} onClick={() => void checkSafety()}>{t("release_run_safety")}</Button>
                  {safety?.actions.publish ? <Button variant="primary" disabled={busy !== null || dirty || !detail.actions.publish} loading={busy === "publish"} onClick={() => void publish()}>{t(draft.audienceMode === "private" ? "release_apply_private_action" : willRequireReview ? "release_submit_review" : "release_publish_action")}</Button> : null}
                </div>
              </div>
            </section>
          ) : null}
        </>
      ) : null}


      {evalAvailability === "enabled" && (evalPanelVisited || activeTask === "evaluate") ? (
        <div
          className="am-release-test-assets"
          hidden={activeTask !== "evaluate"}
          aria-hidden={activeTask !== "evaluate" || undefined}
        >
          <AgentEvalPanel
            agentId={agentId}
            agentName={draft.displayName || detail.aggregate.name}
            archived={archived}
            refreshToken={detail.pending_candidate?.candidate_id ?? detail.live.release_id}
            onAskCoach={coachAvailable ? (text) => void openCoachWith(text) : undefined}
            onSuiteSaved={() => setSuitesRefreshKey((value) => value + 1)}
            addCaseRequest={addCaseRequest}
            hideSafetyOnly={evaluateTestState === "no_cases"}
            tools={evalPointTools}
            privateAgent={draft.audienceMode === "private"}
            liveVersion={detail.live.release_sequence}
          />
        </div>
      ) : null}

      {activeTask === "release" && !hostedMode ? <section id={AGENT_HISTORY_ANCHOR} className="am-release-history">
        <header><span>{t("release_history_eyebrow")}</span><h2>{t("release_history_title")}</h2><p>{t("release_history_body")}</p></header>
        <ol>
          {releases.map((release) => {
            const live = release.release_id === detail.live.release_id;
            return <li key={release.release_id} data-live={live}><div className="am-release-version"><strong>v{release.release_sequence}</strong><span>{live ? t("release_current") : localize(`release_change_${release.change_kind}`, release.change_kind)}</span></div><div><h3>{release.display_name || draft.displayName}</h3><p>{releaseNoteText(release.release_note, t)}</p>{release.release_test && release.release_test.state !== "passed" ? <p className="am-release-test" data-state={release.release_test.state}>{localize(`release_history_test_${release.release_test.state}`, t("release_history_test_other"))}</p> : null}<small>{formatDate(release.activated_at, locale)} · {localize(`release_audience_${release.audience.mode}`, release.audience.mode)} · {t("release_history_risk_label", { level: localize(`risk_effect_${release.risk.level ?? "unknown"}`, release.risk.level ?? "unknown") })}</small></div>{!live && !archived ? <button disabled={busy !== null || Boolean(detail.pending_candidate)} onClick={() => void rollback(release)}>{t("release_restore_as_draft")}</button> : null}</li>;
          })}
        </ol>
      </section> : null}
      </TaskStage>
      </Tabs>
      </fieldset>
    </div>
  );
}
