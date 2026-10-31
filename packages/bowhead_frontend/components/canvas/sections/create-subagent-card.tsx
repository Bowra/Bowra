"use client";

import { captureSubagentDraftContext } from "@/lib/subagent-draft-context";

/**
 * CCA P3 (S5) — CreateSubagentCard.
 *
 * Verbatim translation of the v10 Claude Design handoff
 * (bowhead-v10/Bowhead Create Subagent Card.html + create-subagent-card.jsx): same chrome
 * as the shipped Create Tool card (stepper header, soft borders, terracotta accent, --c-*
 * tokens). A member assembles a SUBSET of the team's existing tools (teammcp.* from P2) +
 * native analyst tools into a routable, team-scoped subagent. Wired to the real
 * /api/v1/team-subagents endpoints (lib/team-subagents-api.ts); the governing invariant
 * (caller's effective perms ∩ declared bundle — zero escalation) is enforced server-side.
 *
 * Faithful to the design except where the mock's demo data has no real source: the skills
 * preset-chips and the "先试运行一次" dry-run link are omitted (no skills catalog / dry-run
 * endpoint in this slice); the output_schema copy does not claim keyword-bans the FE can't
 * perform (the server is authoritative and its 422 is surfaced).
 */

import { useLocale, useTranslations } from "next-intl";
import type { LucideIcon } from "lucide-react";
import {
  Bot as LucideBot,
  Brain as LucideBrain,
  ChartColumn as LucideChartColumn,
  Check as LucideCheck,
  ChevronDown as LucideChevronDown,
  ChevronLeft as LucideChevronLeft,
  ChevronRight as LucideChevronRight,
  Code as LucideCode,
  Folder as LucideFolder,
  Globe as LucideGlobe,
  Hash as LucideHash,
  Info as LucideInfo,
  Layers as LucideLayers,
  MessageSquare as LucideMessageSquare,
  Minus as LucideMinus,
  Plus as LucidePlus,
  Route as LucideRoute,
  Search as LucideSearch,
  SlidersVertical as LucideSlidersVertical,
  Sparkles as LucideSparkles,
  Table as LucideTable,
  TriangleAlert as LucideTriangleAlert,
  User as LucideUser,
  X as LucideX,
} from "lucide-react";
import { renderRefreshSvg } from "@/components/icon-refresh";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { listTeamPlugins, type TeamPlugin } from "@/lib/team-plugins-api";
import { agentResourceCounts, toolSelectionLockedReasonKey, withoutSkillOperationToolRefs, withoutSkillOperationTools, type AgentResourceCounts } from "@/lib/agent-tool-selection";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { useAppRouter } from "@/lib/use-app-router";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { handoffAgentToNewChat } from "@/lib/composer-handoff";
import { confirmCanvasNavigation } from "@/lib/canvas-navigation-guard";
import { useCopilotFields } from "@/lib/use-copilot-fields";
import type { ConfigDraftResult } from "@/lib/config-drafts-api";
import { randomUuid } from "@/lib/random-id";
import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { Button } from "@/components/ui/button";
import { Field, fieldDescribedBy, SuggestionBadge } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { Badge } from "@/components/ui/badge";
import { SegmentedControl, Segment } from "@/components/ui/segmented-control";
import { CanvasSection, FieldRow, SectionHeading } from "@/components/ui/section-heading";
import { AlertTriangle, Bot as BotIcon, Plus as PlusIcon, Search as SearchIcon, X as XIcon } from "@/components/icons";
import {
  CanvasObjectFooter,
  CanvasObjectHeader,
  CanvasObjectSurface,
  CanvasObjectToolbar,
  ChangeRow,
  ObjectStatusBadge,
  StateBlock,
  useCanvasCloseGuard,
  type CanvasObjectStatus,
  type CanvasSaveState,
} from "@/components/canvas/object";
import { formatChangeValue, formatListChange, type ChangeValue } from "@/lib/canvas-change-value";
import { AgentCreationReceipt } from "./agent-creation-receipt";
import { AgentDraftSuggestions, suggestionRowId, type AgentDraftSuggestionsModel } from "./agent-draft-suggestions";
import canvasStyles from "./agent-draft-canvas.module.css";
import { createAgentErrorKey, CreateAgentNotice } from "./create-subagent-errors";
import { blockedItemLines, dependencyIssueItems, type AgentBlockedItem } from "@/lib/agent-blocked-items";
import {
  createManualSubagentDraft,
  createSubagentFromManualDraft,
  getManualSubagentDraft,
  isSubagentDraftReadUnavailable,
  saveManualSubagentDraft,
  SubagentDraftApiError,
  validateManualSubagentDraft,
  type ManualSubagentDraftDefinition,
  type ManualSubagentDraftDetail,
} from "@/lib/subagent-drafts-api";
import {
  getAgentAuthoringCatalog,
  getAgentWorkingCopy,
  saveAgentWorkingCopy,
  type AgentAuthoringCatalog,
  type AgentAuthoringPluginSection,
  type AgentWorkingCopyDetail,
} from "@/lib/agents-api";
import { ResourcePluginPicker, ResourceToolPicker, pluginsFromCatalog } from "@/components/ui/resource-picker";

import {
  createSubagent,
  getSubagentManager,
  getSkillsCatalog,
  getToolsCatalog,
  patchSubagent,
  type TeamSubagentOwnerManagerDetail,
  type TeamSubagentSkillEntry,
  type TeamToolPickerEntry,
  type SkillsCatalog,
  type ToolsCatalog,
} from "@/lib/team-subagents-api";

// ── props ────────────────────────────────────────────────────────────────────

export interface CreateSubagentCardProps {
  /** Canvas owns object navigation and closing; the original form keeps its writer. */
  presentation?: "card" | "canvas";
  /** Continuous creation fields share the same retained writer as suggestion adoption. */
  continuous?: boolean;
  controllerRef?: React.RefObject<AgentDraftEditorPort | null>;
  onActivate?: () => void;
  onAccessDenied?: () => void;
  /** Render flush inside the right-side creation pane. */
  embedded?: boolean;
  /** ConfigDrawer owns the close control when this editor is nested in it. */
  hideClose?: boolean;
  /** one-line purpose hint from the cca.subagent_creator_open event (prefills description). */
  purposeHint?: string | null;
  /** name the user explicitly stated in chat (from the same event); prefills the slug. */
  nameHint?: string | null;
  /** display name the user explicitly stated in chat; prefills the display-name field. */
  displayNameHint?: string | null;
  /** edit mode (cca P3 settings): an existing subagent to prefill + PATCH instead of create. */
  editing?: TeamSubagentOwnerManagerDetail | null;
  /** Chat edit draft; only its changed fields overlay the freshly fetched manager detail. */
  initialDraft?: ConfigDraftResult | null;
  draftSubscribe?: (apply: (draft: ConfigDraftResult) => { filled: number }) => () => void;
  revisioned?: { detail: AgentWorkingCopyDetail; catalog: AgentAuthoringCatalog } | null;
  unifiedDraftEnabled?: boolean;
  manualDraft?: ManualSubagentDraftDetail | null;
  refreshVersion?: number | null;
  chatDraftEditEnabled?: boolean;
  onDraftChange?: (binding: { draftId: string; version: number; dirty: boolean } | null) => void;
  onManualDraftSaved?: (draftId: string) => void;
  /** close the card (clears the local open state in the mount). */
  onClose: () => void;
  /** called after a successful create — the mount may drop a local non-turn confirmation line. */
  onCreated?: (name: string, toolCount: number) => void;
  /** Read-only recovery metadata; it does not repeat the creation callback. */
  onReceiptLabel?: (label: string) => void;
  /** called after a successful edit — the settings list refreshes. */
  onSaved?: (name: string, toolCount: number, displayName?: string) => void;
  /** Canvas: AI suggestions section (decision D8), owned by the suggestion reader. */
  aiSuggestions?: AgentDraftSuggestionsModel | null;
  /** Canvas: a suggestion is being applied outside this form. */
  externalBusy?: boolean;
  /** Canvas: save failure and creation progress for the object tab marker. */
  onCanvasState?: (state: AgentDraftCanvasState) => void;
}

export interface AgentDraftCanvasState {
  saveFailed: boolean;
  creating: boolean;
}

export interface AgentDraftEditorPort {
  read: () => { draft: ManualSubagentDraftDetail | null; dirty: boolean };
  /** Save pending local edits now (autosave flush); resolves once nothing is unsaved. */
  flush: () => Promise<void>;
  mutate: (operation: (draft: ManualSubagentDraftDetail) => Promise<ManualSubagentDraftDetail>) => Promise<ManualSubagentDraftDetail>;
  requestClose: () => void;
}

type Step = "describe" | "tools" | "configure" | "success";
type CopilotField = "display_name" | "description" | "system_prompt" | "tool_refs" | "skills" | "output_schema";
type FieldSnapshot = { field: CopilotField; value: unknown };
type ManualConflictField = "name" | "display_name" | "description" | "system_prompt"
  | "tool_refs" | "skill_grants" | "capability_ids" | "model" | "max_iterations" | "output_schema_text" | "text_only_confirmed";
const MANUAL_CONFLICT_FIELDS: { key: ManualConflictField; label: string }[] = [
  { key: "name", label: "slug_label" },
  { key: "display_name", label: "display_label" },
  { key: "description", label: "desc_label" },
  { key: "system_prompt", label: "persona_label" },
  { key: "tool_refs", label: "conflict_tools" },
  { key: "skill_grants", label: "suggestion_field_skill_grants" },
  { key: "capability_ids", label: "suggestion_field_capability_ids" },
  { key: "model", label: "model_label" },
  { key: "output_schema_text", label: "schema_label" },
  { key: "text_only_confirmed", label: "draft_text_only_confirm" },
];

function manualConflictValues(
  definition: Partial<ManualSubagentDraftDefinition>,
  textOnlyConfirmed: boolean,
): Record<ManualConflictField, string> {
  return {
    name: definition.name ?? "",
    display_name: definition.display_name ?? "",
    description: definition.description ?? "",
    system_prompt: definition.system_prompt ?? "",
    tool_refs: JSON.stringify([...(definition.tool_refs ?? [])].sort()),
    skill_grants: JSON.stringify(definition.skill_grants ?? []),
    // Built-in Plugin selections share the Plugin conflict field, so a change made elsewhere is never overwritten silently.
    capability_ids: JSON.stringify([...[...(definition.capability_ids ?? [])].sort(), ...[...(definition.builtin_plugin_ids ?? [])].sort()]),
    model: definition.model ?? "analyst",
    max_iterations: String(definition.max_iterations ?? 12),
    output_schema_text: definition.output_schema_text ?? (definition.output_schema ? JSON.stringify(definition.output_schema, null, 2) : ""),
    text_only_confirmed: String(textOnlyConfirmed),
  };
}

/** translation fn from useTranslations("create_subagent"), threaded into the module-level
 * primitives below that render copy (they live outside the component, so they can't call the
 * hook themselves). */
type T = (key: string, values?: Record<string, string | number>) => string;

const MAX_PROMPT = 15000;
const MAX_DESC = 200;
const MAX_DISPLAY = 40; // server caps display_name at 40 (team_subagents._validate_common).
const MIN_ITER = 1;
const MAX_ITER = 50;
const NAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;
const MODELS = ["analyst"] as const;

// ── icons (merged from icons.jsx + the card's local SP set) ────────────────────

/** P2 batch 2: Lucide icon per name (see `components/icon-refresh.tsx`). */
const ICONS: Readonly<Record<string, LucideIcon>> = {
  check: LucideCheck,
  x: LucideX,
  chevronRight: LucideChevronRight,
  chevronLeft: LucideChevronLeft,
  chevronDown: LucideChevronDown,
  plus: LucidePlus,
  search: LucideSearch,
  alertTriangle: LucideTriangleAlert,
  folder: LucideFolder,
  sparkles: LucideSparkles,
  table: LucideTable,
  code: LucideCode,
  barChart: LucideChartColumn,
  brain: LucideBrain,
  messageSquare: LucideMessageSquare,
  info: LucideInfo,
  bot: LucideBot,
  globe: LucideGlobe,
  user: LucideUser,
  hash: LucideHash,
  route: LucideRoute,
  sliders: LucideSlidersVertical,
  minus: LucideMinus,
  layers: LucideLayers,
};

function Icon({ name, s = 18, style }: { name: string; s?: number; style?: React.CSSProperties }) {
  const Next = ICONS[name];
  return Next ? renderRefreshSvg(Next, s, s, { "aria-hidden": true, style }) : null;
}

// ── shared visual primitives (verbatim from the design) ────────────────────────

function methodStyle(m: string): { bg: string; bd: string; fg: string } {
  const mm = (m || "").toUpperCase();
  if (mm === "GET") return { bg: "var(--c-pos-soft)", bd: "var(--c-pos-line)", fg: "var(--c-pos-ink)" };
  if (mm === "DELETE") return { bg: "var(--c-err-soft)", bd: "var(--c-err-line)", fg: "var(--c-err-ink)" };
  return { bg: "var(--c-accent-soft)", bd: "var(--c-accent-line)", fg: "var(--c-accent)" };
}

function MethodBadge({ m }: { m: string }) {
  const c = methodStyle(m);
  return (
    <span className="bh-mono" style={{
      fontSize: "0.625rem", fontWeight: 700, letterSpacing: ".04em", color: c.fg, background: c.bg,
      border: `1px solid ${c.bd}`, borderRadius: 5, padding: "2px 6px", lineHeight: 1.2,
      flex: "0 0 auto", minWidth: 42, textAlign: "center",
    }}>{(m || "").toUpperCase()}</span>
  );
}

function RiskTag({ label, warning }: { label: string; warning: boolean }) {
  return (
    <span style={{
      fontSize: "0.625rem", fontWeight: 600, color: warning ? "var(--c-warn)" : "var(--c-ink-3)",
      background: warning ? "var(--c-warn-soft)" : "var(--c-surface-2)",
      border: `1px solid ${warning ? "var(--c-warn-line)" : "var(--c-line)"}`, borderRadius: 5, padding: "1px 6px",
      display: "inline-flex", alignItems: "center", gap: 3,
    }}>
      {warning ? <Icon name="alertTriangle" s={10} /> : null}{label}
    </span>
  );
}

function Stepper({ active, t }: { active: number; t: T }) {
  const steps: [string, string][] = [
    ["①", t("step_describe")], ["②", t("step_tools")], ["③", t("step_configure")], ["✓", t("step_done")],
  ];
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 7, padding: "11px 16px",
      borderTop: "1px solid var(--c-line-2)", borderBottom: "1px solid var(--c-line-2)",
      background: "var(--c-bg)",
    }}>
      {steps.map((st, i) => {
        const done = i < active, cur = i === active;
        const ring = done || cur ? "var(--c-accent)" : "var(--c-line)";
        const fill = done ? "var(--c-accent)" : cur ? "var(--c-accent-tint)" : "transparent";
        const fg = done ? "var(--c-accent-ink)" : cur ? "var(--c-accent)" : "var(--c-ink-4)";
        return (
          <span key={i} style={{ display: "contents" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <span style={{
                width: 20, height: 20, borderRadius: "50%", border: `1.5px solid ${ring}`,
                background: fill, color: fg, display: "grid", placeItems: "center",
                fontSize: "0.6875rem", fontWeight: 700,
              }}>{done ? <Icon name="check" s={12} /> : <CharIcon ch={st[0]} />}</span>
              <span style={{
                fontSize: "0.75rem", fontWeight: cur ? 600 : 500,
                color: cur ? "var(--c-ink)" : done ? "var(--c-ink-3)" : "var(--c-ink-4)",
              }}>{st[1]}</span>
            </div>
            {i < 3 ? (
              <span style={{
                flex: 1, height: 1, minWidth: 14,
                background: i < active ? "var(--c-accent)" : "var(--c-line)",
                opacity: i < active ? 0.5 : 1,
              }} />
            ) : null}
          </span>
        );
      })}
    </div>
  );
}

function Callout({ tone, icon, children }: {
  tone: "warn" | "info"; icon: string; children: React.ReactNode;
}) {
  const map = {
    warn: { bg: "var(--c-warn-soft)", bd: "var(--c-warn-line)", fg: "var(--c-warn-ink)", ic: "var(--c-warn)" },
    info: { bg: "var(--c-surface-2)", bd: "var(--c-line)", fg: "var(--c-ink-3)", ic: "var(--c-ink-4)" },
  }[tone];
  return (
    <div style={{
      display: "flex", gap: 10, alignItems: "flex-start", padding: "10px 12px",
      background: map.bg, border: `1px solid ${map.bd}`, borderRadius: "var(--r-2)",
    }}>
      <span style={{ flex: "0 0 auto", color: map.ic, marginTop: 1 }}>
        <Icon name={icon} s={16} />
      </span>
      <div style={{ flex: 1, fontSize: "0.75rem", lineHeight: 1.55, color: map.fg }}>{children}</div>
    </div>
  );
}

function Check({ on, onToggle, disabled = false }: { on: boolean; onToggle: () => void; disabled?: boolean }) {
  return (
    <button type="button" role="checkbox" aria-checked={on} disabled={disabled}
      onClick={(e) => { e.preventDefault(); onToggle(); }}
      style={{
        width: 19, height: 19, flex: "0 0 19px", borderRadius: 6, cursor: disabled ? "not-allowed" : "pointer",
        border: `1.5px solid ${on ? "var(--c-accent)" : "var(--c-line)"}`,
        background: on ? "var(--c-accent)" : "var(--c-surface)", color: "var(--c-accent-ink)",
        display: "grid", placeItems: "center", padding: 0, transition: "all .14s var(--ease-v2)",
      }}>
      {on ? <Icon name="check" s={12} /> : null}
    </button>
  );
}

function btnPrimary(disabled?: boolean): React.CSSProperties {
  return {
    display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7, fontSize: "0.84375rem",
    fontWeight: 600, padding: "10px 18px", borderRadius: "var(--r-1)", border: "none",
    cursor: disabled ? "default" : "pointer",
    background: disabled ? "var(--c-surface-3)" : "var(--c-accent)",
    color: disabled ? "var(--c-ink-4)" : "var(--c-accent-ink)", transition: "background .15s",
  };
}

function btnGhost(): React.CSSProperties {
  return {
    display: "inline-flex", alignItems: "center", gap: 6, fontSize: "0.84375rem", fontWeight: 600,
    padding: "10px 16px", borderRadius: "var(--r-1)", border: "1px solid var(--c-line)",
    cursor: "pointer", background: "var(--c-surface)", color: "var(--c-ink-2)",
  };
}

function linkBtn(): React.CSSProperties {
  return {
    fontSize: "0.78125rem", fontWeight: 500, color: "var(--c-ink-3)", background: "transparent",
    border: "none", cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 5,
    padding: "8px 2px", textDecorationLine: "underline", textDecorationColor: "var(--c-line)",
    textUnderlineOffset: 3,
  };
}

function fieldInput(): React.CSSProperties {
  return {
    width: "100%", border: "1.5px solid var(--c-line)", borderRadius: "var(--r-1)", padding: "10px 12px",
    fontSize: "0.84375rem", color: "var(--c-ink)", background: "var(--c-surface)", outline: "none",
    fontFamily: "var(--f-sans)",
  };
}

/** `htmlFor` ties the visible label to its control so the control's accessible name is the
 * field label, not its placeholder example. */
function FieldLabel({ children, hint, htmlFor }: { children: React.ReactNode; hint?: string; htmlFor?: string }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6 }}>
      <label htmlFor={htmlFor} style={{ fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)" }}>{children}</label>
      {hint ? <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)" }}>{hint}</span> : null}
    </div>
  );
}

function GroupHeader({ icon, title, sub }: { icon: string; title: string; sub?: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 4px 6px" }}>
      <span style={{ color: "var(--c-ink-4)" }}><Icon name={icon} s={14} /></span>
      <span style={{ fontSize: "0.71875rem", fontWeight: 700, color: "var(--c-ink-2)", letterSpacing: ".02em" }}>{title}</span>
      {sub ? <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)" }}>{sub}</span> : null}
    </div>
  );
}

// Native tool name → icon + human-readable name + one-sentence description.
//
// ⚠ Descriptions **formerly** guessed from prefixes (`kb_*` always "查询知识库表与记录", `canvas_*` always
// "在画布上渲染图表与报表"); unknown tools used **tool identifiers themselves** as descriptions. Real-device dogfood on 116,
// 2026-09-07: 7 of 28 items showed identifiers such as `pbi_execute_dax` / `web_fetch`,
// with others reduced to family descriptions, leaving customers unable to tell what tools do. Original comment "the catalog returns bare
// names" was accurate then — this drawer's catalog endpoint returned only **bare tool-name arrays**.
//
// Names/descriptions now come from generated `tool_aliases.yaml` table (100% active-tool coverage, Chinese/English pairs),
// shared with step cards and chips. Map icons by category (generated sigil is emoji; here we need
// outline icon names). New tools require only YAML changes, none here.
import { toolDisplay, type ToolCategory } from "@/lib/tool-display";
import { authoringSkillDescription, authoringSkillTitle } from "@/lib/skills-meta";
import { CharIcon } from "@/components/char-icon";

const CATEGORY_ICON: Record<ToolCategory, string> = {
  bi: "table",
  kb: "folder",
  web: "search",
  compute: "code",
  canvas: "barChart",
  skill: "sparkles",
  orchestrate: "bot",
  media: "layers",
  document: "folder",
  unknown: "code",
};

function nativeMeta(name: string): { icon: string; title: string; desc: string } {
  const d = toolDisplay(name);
  return {
    icon: CATEGORY_ICON[d.category] ?? "code",
    // Unknown tools: `toolDisplay` falls back to original name + empty description — **do not pretend to recognize them**;
    // description falls back to identifier, at least making it grep-able.
    title: d.label,
    desc: d.blurb || name,
  };
}

/** Team-tool list labels.
 *
 * Team-tool wire name is `teammcp.<source_id>.<operation>`; the `ots_819dfef35887` portion is
 * internal ID, pure noise to users. Step cards already strip prefix (`subagent.py:_tool_header_zh`
 * teammcp branch, prompted by 116 E2E on 2026-07-21), but checkbox list did not catch up —
 * real-device dogfood on 116, 2026-09-07 displayed full `teammcp.ots_819dfef35887.addPet` to customers.
 *
 * ⚠ **Field name `operation_id` does not mean only the operation portion** (my first version made this mistake;
 * 116 dogfood recheck on 2026-09-07 still showed full string). It contains `TeamToolEntry.dispatch_name`,
 * and `openapi_tool_source.py:405` defines **OpenAPI source `dispatch_name == advertised_name`**
 * — full `teammcp.<src>.<op>`; only remote MCP sources use the peer's original tool name.
 * Therefore strip prefix from **the retrieved string**, rather than routing by field name.
 */
function teamToolLabel(t: TeamToolPickerEntry): string {
  const raw = t.operation_id || t.tool_name || "";
  // Remote MCP original names lack this prefix; retain verbatim (peer-provided names, not our internal IDs).
  if (!raw.startsWith("teammcp.")) return raw;
  return raw.slice(raw.lastIndexOf(".") + 1) || raw;
}

// Presentation only: the server classifies risk and controls selection.
function teamToolRiskLabelKey(risk: TeamToolPickerEntry["risk"]) {
  switch (risk) {
    case "read": return "draft_risk_read";
    case "compute": return "draft_risk_compute";
    case "artifact_write": return "draft_risk_artifact_write";
    case "external_write": return "draft_risk_external_write";
    case "destructive": return "draft_risk_destructive";
    default: return "draft_risk_unknown";
  }
}

function TeamToolRow({ t, on, onToggle }: { t: TeamToolPickerEntry; on: boolean; onToggle: () => void }) {
  const toolT = useTranslations("settings.subagents");
  const locked = t.selectable === false || Boolean(t.locked_reason);
  const disabled = locked && !on;
  return (
    <label style={{
      display: "flex", gap: 11, alignItems: "flex-start", padding: "10px 12px",
      borderRadius: "var(--r-2)", cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.62 : 1,
      background: on ? "var(--c-accent-tint)" : "transparent",
      border: `1px solid ${on ? "var(--c-accent-line)" : "transparent"}`, transition: "background .14s",
    }}>
      <div style={{ marginTop: 1 }}><Check on={on} onToggle={onToggle} disabled={disabled} /></div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ fontSize: "0.8125rem", fontWeight: 600, color: "var(--c-ink)" }}>
            {teamToolLabel(t)}
          </span>
          {t.method ? <MethodBadge m={t.method} /> : null}
          <RiskTag label={toolT(teamToolRiskLabelKey(t.risk))} warning={t.risk !== "read" && t.risk !== "compute" && t.risk !== "artifact_write"} />
        </div>
        <div style={{ fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 3 }}>
          {t.description || t.tool_name}
          {locked ? ` · ${toolT(toolSelectionLockedReasonKey(t.locked_reason ?? ""))}` : null}
        </div>
      </div>
    </label>
  );
}

function SkillToggleRow({ entry, badge, on, onToggle, disabled = false, issue }: {
  entry: TeamSubagentSkillEntry; badge: string; on: boolean; onToggle: () => void;
  disabled?: boolean; issue?: string;
}) {
  const locale = useLocale();
  const title = authoringSkillTitle({ ...entry, display_name: entry.display_name || entry.name }, locale);
  const description = authoringSkillDescription({ ...entry, display_name: title }, locale);
  return (
    <label style={{
      display: "flex", gap: 11, alignItems: "flex-start", padding: "10px 12px",
      borderRadius: "var(--r-2)", cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.62 : 1,
      background: on ? "var(--c-accent-tint)" : "transparent",
      border: `1px solid ${on ? "var(--c-accent-line)" : "transparent"}`, transition: "background .14s",
    }}>
      <div style={{ marginTop: 1 }}><Check on={on} onToggle={onToggle} disabled={disabled} /></div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ fontSize: "0.8125rem", fontWeight: 600, color: "var(--c-ink)" }}>{title}</span>
          {title !== entry.name ? <code className="bh-mono" style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)" }}>{entry.name}</code> : null}
          <span style={{
            fontSize: "0.59375rem", fontWeight: 600, color: "var(--c-info-ink)", background: "var(--c-info-soft)",
            border: "1px solid var(--c-info-line)", borderRadius: 5, padding: "1px 6px",
          }}>{badge}</span>
        </div>
        {description ? (
          <div style={{ fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 3 }}>{description}</div>
        ) : null}
        {issue ? <div style={{ fontSize: "0.71875rem", color: "var(--c-warn)", marginTop: 3 }}>{issue}</div> : null}
      </div>
    </label>
  );
}

/** D9 · identifier from the display name: ASCII letters and digits only; otherwise a stable fallback. */
export function identifierFromName(name: string, seed: string): string {
  const ascii = name.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").replace(/^[^a-z]+/, "").slice(0, 32).replace(/-+$/g, "");
  if (NAME_RE.test(ascii)) return ascii;
  const suffix = seed.toLowerCase().replace(/[^a-z0-9]/g, "").slice(-6) || "draft";
  return `agent-${suffix}`;
}

function RiskBadge({ risk }: { risk: TeamToolPickerEntry["risk"] }) {
  const toolT = useTranslations("settings.subagents");
  const warning = risk !== "read" && risk !== "compute" && risk !== "artifact_write";
  return <Badge tone={warning ? "warning" : "neutral"} size="sm">{toolT(teamToolRiskLabelKey(risk))}</Badge>;
}

// ── component ──────────────────────────────────────────────────────────────────

export function CreateSubagentCard({
  presentation = "card",
  purposeHint, nameHint, displayNameHint, embedded = false, hideClose = false, editing, initialDraft, draftSubscribe, revisioned,
  unifiedDraftEnabled = false, manualDraft: initialManualDraft, refreshVersion = null,
  chatDraftEditEnabled = false, onDraftChange, onManualDraftSaved,
  continuous = false, controllerRef, onActivate, onAccessDenied,
  onClose, onCreated, onReceiptLabel, onSaved, aiSuggestions = null, externalBusy = false, onCanvasState,
}: CreateSubagentCardProps) {
  const t = useTranslations("create_subagent");
  const tMarket = useTranslations("agents_marketplace");
  const locale = useLocale();
  const router = useAppRouter();
  const canDirectUse = useOptionalAuthSession()?.me?.capabilities?.composer?.agent_start_use === true;
  const fieldId = useId();
  const slugId = `${fieldId}-slug`;
  const displayId = `${fieldId}-display`;
  const descId = `${fieldId}-desc`;
  const personaId = `${fieldId}-persona`;
  const modelId = `${fieldId}-model`;
  const toolsRequiredId = `${fieldId}-tools-required`;
  const isEditing = !!editing;
  const [step, setStep] = useState<Step>(initialManualDraft?.result ? "success" : "describe");
  const [busy, setBusy] = useState(false);
  const finalizingRef = useRef(false);
  // Show only what happened and what to do; validation/error codes, status codes, and request IDs go in "复制诊断".
  const [error, setError] = useState<ShownError | null>(null);
  const dependencyLinesRef = useRef<(details: unknown) => string[]>(() => []);
  const showError = useCallback(
    (cause: unknown) => {
      const key = createAgentErrorKey(cause);
      // Editing an existing agent has no draft: conflict means agent changed elsewhere, not "左侧对话更新了草稿".
      const shownKey = isEditing && key === "draft_chat_conflict" ? "issue_edit_conflict" : key;
      // A dependency failure names each Plugin, Tool or Skill and what to do about it.
      const named = cause instanceof SubagentDraftApiError ? dependencyLinesRef.current(cause.details) : [];
      setError(shownError(cause instanceof CreateAgentNotice ? cause.notice : named.length ? named.join("\n") : t(shownKey), cause));
    },
    [isEditing, t],
  );
  const [savedAgent, setSavedAgent] = useState<{ id: string; label: string; visibility: string } | null>(initialManualDraft?.result ? {
    id: initialManualDraft.result.definition_id, label: initialManualDraft.definition.display_name || initialManualDraft.definition.name || "", visibility: "creator_only",
  } : null);
  const [recoveredAgent, setRecoveredAgent] = useState<TeamSubagentOwnerManagerDetail | null>(null);
  const [receiptNeedsRead, setReceiptNeedsRead] = useState(Boolean(initialManualDraft?.result));
  const [receiptReadFailed, setReceiptReadFailed] = useState(false);
  const [receiptReadEpoch, setReceiptReadEpoch] = useState(0);
  const requestContext = useRef(Object.freeze({ ...(initialManualDraft?.requestContext ?? captureSubagentDraftContext(initialManualDraft?.workspace_id)) }));
  const [manualDraft, setManualDraft] = useState<ManualSubagentDraftDetail | null>(initialManualDraft ? { ...initialManualDraft, requestContext: requestContext.current } : null);
  const [manualPurposeOverride, setManualPurposeOverride] = useState<string | null>(initialManualDraft?.purpose ?? null);
  const [manualConflict, setManualConflict] = useState<{
    latest: ManualSubagentDraftDetail;
    choices: Partial<Record<ManualConflictField, "mine" | "latest">>;
  } | null>(null);
  const manualCreateKey = useRef(randomUuid());
  const [manualSaveStatus, setManualSaveStatus] = useState<"idle" | "saving" | "saved">("idle");
  const [manualSaveFailed, setManualSaveFailed] = useState(false);
  const [unknownSave, setUnknownSave] = useState<{ baseline: ManualSubagentDraftDetail; snapshot: string; creating?: boolean } | null>(null);
  const unknownSaveRef = useRef(unknownSave);
  unknownSaveRef.current = unknownSave;
  const [leavePromptOpen, setLeavePromptOpen] = useState(false);
  const leavePromptOpenRef = useRef(false);
  const leaveDecisionRef = useRef<((allowed: boolean) => void) | null>(null);
  const leavePromiseRef = useRef<Promise<boolean> | null>(null);
  const closeAfterLeaveRef = useRef(false);
  const externalMutationRef = useRef(false);
  const [externalMutation, setExternalMutation] = useState(false);
  const activateRef = useRef(onActivate);
  activateRef.current = onActivate;
  const manualSavesInFlightRef = useRef(0);
  const manualSavePromiseRef = useRef<Promise<ManualSubagentDraftDetail> | null>(null);
  const manualDraftRef = useRef(manualDraft);
  manualDraftRef.current = manualDraft;
  const savedManualSnapshotRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  useEffect(() => {
    if (step !== "success" || !receiptNeedsRead || !savedAgent?.id) return;
    let active = true;
    setReceiptReadFailed(false);
    void getSubagentManager(savedAgent.id, requestContext.current).then((agent) => {
      if (!active || !mountedRef.current) return;
      if (agent.id !== savedAgent.id) throw new Error("Creation receipt target mismatch");
      const label = agent.display_name || agent.name;
      setRecoveredAgent(agent);
      setSavedAgent({ id: agent.id, label, visibility: agent.visibility });
      setReceiptNeedsRead(false);
    }).catch(() => {
      // Creation is acknowledged, but current configuration is unknown or no longer authorized.
      if (active && mountedRef.current) setReceiptReadFailed(true);
    });
    return () => { active = false; };
  }, [step, receiptNeedsRead, savedAgent?.id, receiptReadEpoch]);
  // The receipt counts what the created Agent was released with, read the same way live and after a reload.
  const [receiptResources, setReceiptResources] = useState<AgentResourceCounts | null>(null);
  useEffect(() => {
    if (step !== "success" || isEditing || !savedAgent?.id) return;
    let active = true;
    void getAgentWorkingCopy(savedAgent.id).then((detail) => {
      if (active && mountedRef.current && detail.agent_id === savedAgent.id) setReceiptResources(agentResourceCounts(detail.live.definition));
    }).catch(() => {
      // Keep this session's counts; a reopened receipt then shows no resource line rather than a guess.
    });
    return () => { active = false; };
  }, [step, isEditing, savedAgent?.id, receiptReadEpoch]);
  useEffect(() => {
    if (step === "success" && savedAgent) onReceiptLabel?.(savedAgent.label || t("canvas_created_agent"));
  }, [step, savedAgent, onReceiptLabel, t]);
  const readManualDraft = useCallback((id: string) => getManualSubagentDraft(id, requestContext.current).catch((cause: unknown) => {
    if (mountedRef.current && isSubagentDraftReadUnavailable(cause)) onAccessDenied?.();
    throw cause;
  }), [onAccessDenied]);
  const [textOnlyConfirmed, setTextOnlyConfirmed] = useState(
    () => Boolean(initialManualDraft?.warnings.some((warning) => warning.code === "subagent_text_only_confirmation")),
  );
  const [boundSkills, setBoundSkills] = useState<NonNullable<ManualSubagentDraftDefinition["skill_grants"]>>(() => initialManualDraft?.definition.skill_grants ?? []);
  // Editing a revisioned Agent starts from its working copy (or live) Plugin selections.
  const revisionedPluginIds = (key: "capability_ids" | "builtin_plugin_ids"): string[] => {
    const value = (revisioned?.detail.working_copy ?? revisioned?.detail.live)?.definition[key];
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string") : [];
  };
  const [boundPlugins, setBoundPlugins] = useState<string[]>(() => initialManualDraft?.definition.capability_ids ?? revisionedPluginIds("capability_ids"));
  // Plugins the draft generator picked from the request: marked "Recommended" in the creation draft only.
  const [recommendedPlugins] = useState<ReadonlySet<string>>(() => new Set(initialManualDraft?.definition.builtin_plugin_ids ?? []));
  const [builtinPlugins, setBuiltinPlugins] = useState<string[]>(
    () => initialManualDraft?.definition.builtin_plugin_ids ?? revisionedPluginIds("builtin_plugin_ids"),
  );
  const [pluginSection, setPluginSection] = useState<AgentAuthoringPluginSection | undefined>(undefined);
  useEffect(() => {
    if (!unifiedDraftEnabled || isEditing) return;
    let active = true;
    void Promise.resolve().then(() => getAgentAuthoringCatalog()).then((value) => { if (active) setPluginSection(value?.plugins); }).catch(() => { if (active) setPluginSection(undefined); });
    return () => { active = false; };
  }, [unifiedDraftEnabled, isEditing]);
  const [pluginCatalog, setPluginCatalog] = useState<TeamPlugin[]>([]);
  useEffect(() => {
    if (!unifiedDraftEnabled || isEditing || boundPlugins.length === 0) return;
    let active = true;
    void listTeamPlugins(requestContext.current).then((items) => { if (active) setPluginCatalog(items); }).catch(() => { if (active) setPluginCatalog([]); });
    return () => { active = false; };
  }, [unifiedDraftEnabled, isEditing, boundPlugins]);
  const hasBoundResources = boundSkills.length > 0 || boundPlugins.length > 0 || builtinPlugins.length > 0;

  // describe (edit mode prefills from the stored subagent)
  const initialPurpose = purposeHint?.trim() ?? "";
  const [slug, setSlug] = useState(() => editing?.name ?? initialManualDraft?.definition.name ?? (nameHint ? nameHint.trim().replace(/\s+/g, "-").toLowerCase() : ""));
  const [displayName, setDisplayName] = useState(() => editing?.display_name ?? initialManualDraft?.definition.display_name ?? (displayNameHint ?? "").slice(0, MAX_DISPLAY));
  const [desc, setDesc] = useState(() => editing?.description ?? initialManualDraft?.definition.description ?? (initialManualDraft ? "" : initialPurpose.replace(/\s+/g, " ").slice(0, MAX_DESC)));
  const [persona, setPersona] = useState(() => editing?.system_prompt ?? initialManualDraft?.definition.system_prompt ?? (initialManualDraft ? "" : initialPurpose.slice(0, MAX_PROMPT)));
  // Required-field errors wait for the user: a field reports once it has been left
  // (blur) or once the user tries to continue. Opening a prefilled card must not greet
  // them with red text on fields they have not touched yet.
  const [personaTouched, setPersonaTouched] = useState(false);
  const [triedDescribeNext, setTriedDescribeNext] = useState(false);

  // tools
  const [catalog, setCatalog] = useState<ToolsCatalog | null>(null);
  const [catalogErr, setCatalogErr] = useState<ShownError | null>(null);
  const [rawSel, setSel] = useState<Set<string>>(() => new Set(editing?.tool_refs ?? initialManualDraft?.definition.tool_refs ?? []));
  // Stored Skill operation Tools are granted by the selected Skills; they are never picker rows.
  const sel = useMemo(() => new Set(withoutSkillOperationToolRefs([...rawSel])), [rawSel]);
  const [filter, setFilter] = useState("");

  // configure
  const [model, setModel] = useState<string>(() => editing?.model ?? initialManualDraft?.definition.model ?? "analyst");
  const [iters, setIters] = useState(() => editing?.max_iterations ?? initialManualDraft?.definition.max_iterations ?? 12);
  const [schemaOpen, setSchemaOpen] = useState(() => !!(editing?.output_schema ?? initialManualDraft?.definition.output_schema ?? initialManualDraft?.definition.output_schema_text));
  const [schemaText, setSchemaText] = useState(() => {
    const savedText = initialManualDraft?.definition.output_schema_text;
    if (typeof savedText === "string") return savedText;
    const schema = editing?.output_schema ?? initialManualDraft?.definition.output_schema;
    return schema ? JSON.stringify(schema, null, 2) : "";
  });

  // skills (auto-read SOPs the subagent skill_reads at startup)
  const [skillSel, setSkillSel] = useState<Set<string>>(() => new Set(editing?.skills ?? []));
  const [skillsOpen, setSkillsOpen] = useState(() => !!(editing?.skills && editing.skills.length));
  const [skillsCatalog, setSkillsCatalog] = useState<SkillsCatalog | null>(null);
  const [skillsCatErr, setSkillsCatErr] = useState<ShownError | null>(null);
  const [suggestedFields, setSuggestedFields] = useState<string[]>([]);
  // Canvas presentation (Canvas Objects Spec part 2, screens 5–7).
  const canvasMode = presentation === "canvas" && !isEditing;
  const canvasIdBase = useId().replace(/[^A-Za-z0-9_-]/g, "_");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [toolPickerOpen, setToolPickerOpen] = useState(false);
  const [triedCreate, setTriedCreate] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(() => {
    const at = initialManualDraft?.updated_at ? Date.parse(initialManualDraft.updated_at) : Number.NaN;
    return Number.isFinite(at) ? at : null;
  });
  // D9: the identifier follows the name until the user edits it (or one was already saved).
  const [slugEdited, setSlugEdited] = useState(() => Boolean(editing?.name || initialManualDraft?.definition.name || nameHint));
  const { confirmDiscard, dialog: guardDialog } = useCanvasCloseGuard();

  const copilot = useCopilotFields<CopilotField, FieldSnapshot>({
    enabled: !!editing && !!draftSubscribe,
    fields: ["display_name", "description", "system_prompt", "tool_refs", "skills", "output_schema"],
    snapshotOf: (field) => ({
      field,
      value: ({
        display_name: displayName, description: desc, system_prompt: persona,
        tool_refs: [...sel], skills: [...skillSel], output_schema: schemaText,
      })[field],
    }),
    restoreSnapshot: ({ field, value }) => {
      if (field === "display_name") setDisplayName(value as string);
      if (field === "description") setDesc(value as string);
      if (field === "system_prompt") setPersona(value as string);
      if (field === "tool_refs") setSel(new Set(value as string[]));
      if (field === "skills") setSkillSel(new Set(value as string[]));
      if (field === "output_schema") setSchemaText(value as string);
    },
  });

  const applyAiDraft = (draft: ConfigDraftResult): { filled: number } => {
    const fields = draft.fields;
    setSuggestedFields((previous) => [...new Set([...previous, ...Object.keys(fields)])]);
    const session = copilot.beginApply(draft);
    const admit = session.admit;
    if (typeof fields.display_name === "string" && admit("display_name", "display_name")) setDisplayName(fields.display_name.slice(0, MAX_DISPLAY));
    if (typeof fields.description === "string" && admit("description", "description")) setDesc(fields.description.slice(0, MAX_DESC));
    if (typeof fields.system_prompt === "string" && admit("system_prompt", "system_prompt")) setPersona(fields.system_prompt.slice(0, MAX_PROMPT));
    if (Array.isArray(fields.tool_refs) && fields.tool_refs.every((item) => typeof item === "string") && admit("tool_refs", "tool_refs")) setSel(new Set(fields.tool_refs as string[]));
    if (Array.isArray(fields.skills) && fields.skills.every((item) => typeof item === "string") && admit("skills", "skills")) { setSkillSel(new Set(fields.skills as string[])); setSkillsOpen(true); }
    if ((fields.output_schema === null || (typeof fields.output_schema === "object" && !Array.isArray(fields.output_schema))) && admit("output_schema", "output_schema")) {
      setSchemaText(fields.output_schema ? JSON.stringify(fields.output_schema, null, 2) : "");
      setSchemaOpen(true);
    }
    return session.commit(["display_name", "description", "system_prompt", "tool_refs", "skills", "output_schema"]);
  };
  const applyDraftRef = useRef(applyAiDraft);
  applyDraftRef.current = applyAiDraft;
  useEffect(() => {
    if (!initialDraft || !editing) return;
    applyDraftRef.current(initialDraft);
    const keys = Object.keys(initialDraft.fields);
    const counts = [
      keys.filter((key) => ["display_name", "description", "system_prompt"].includes(key)).length,
      keys.filter((key) => key === "tool_refs").length,
      keys.filter((key) => ["skills", "output_schema"].includes(key)).length,
    ];
    setStep((["describe", "tools", "configure"] as const)[counts.indexOf(Math.max(...counts))]);
  }, [initialDraft, editing]);
  useEffect(() => draftSubscribe?.((draft) => applyDraftRef.current(draft)), [draftSubscribe]);

  useEffect(() => {
    if (revisioned) {
      setCatalog({
        text_only_allowed: true,
        native_tools: withoutSkillOperationTools(revisioned.catalog.tools).filter((tool) => tool.source_kind === "native").map((tool) => tool.stable_ref),
        team_tools: revisioned.catalog.tools.filter((tool) => tool.source_kind === "team_mcp").map((tool) => ({
          tool_name: tool.stable_ref, operation_id: "", description: tool.description,
          is_write: tool.risk === "external_write" || tool.risk === "destructive",
          risk: tool.risk, selectable: tool.selectable, locked_reason: tool.locked_reason,
          method: "", path: "",
        })),
      });
      return;
    }
    let live = true;
    getToolsCatalog(unifiedDraftEnabled ? requestContext.current : undefined)
      .then((c) => { if (live) setCatalog(c); })
      .catch((e) => { if (live) setCatalogErr(shownError(t("catalog_load_failed_plain"), e)); });
    return () => { live = false; };
  }, [revisioned]);

  useEffect(() => {
    if (revisioned) {
      setSkillsCatalog({
        builtin: revisioned.catalog.skills.filter((skill) => skill.source_kind === "builtin"),
        team_skills: revisioned.catalog.skills.filter((skill) => skill.source_kind === "team"),
      });
      return;
    }
    let live = true;
    getSkillsCatalog(unifiedDraftEnabled ? requestContext.current : undefined)
      .then((c) => { if (live) setSkillsCatalog(c); })
      .catch((e) => { if (live) setSkillsCatErr(shownError(t("skills_load_failed_plain"), e)); });
    return () => { live = false; };
  }, [revisioned]);

  const toggle = useCallback((id: string) => {
    copilot.touchField("tool_refs");
    setSel((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
    setTextOnlyConfirmed(false);
  }, [copilot]);

  const toggleSkill = useCallback((name: string) => {
    copilot.touchField("skills");
    setSkillSel((s) => { const n = new Set(s); if (n.has(name)) n.delete(name); else n.add(name); return n; });
  }, [copilot]);

  const teamTools = catalog?.team_tools ?? [];
  const nativeTools = catalog?.native_tools ?? [];
  const filt = useCallback((s: string) => filter.trim() === "" || s.toLowerCase().includes(filter.trim().toLowerCase()), [filter]);
  const teamF = teamTools.filter((tool) => filt(tool.tool_name + (tool.operation_id || "") + (tool.description || "") + (tool.method || "")));
  // Search recognizes human-readable names too: customers seeking "运行代码" should not need to enter python_sandbox.
  const natF = nativeTools.filter((n) => {
    const m = nativeMeta(n);
    return filt(n + m.title + m.desc);
  });
  const selectedTeamTools = teamTools.filter((tool) => sel.has(tool.tool_name));
  // A removable, selected checkbox is not evidence that the tool can execute.
  const approvalLockedSelected = selectedTeamTools.some((tool) => tool.locked_reason === "approval_executor_unavailable");
  // Selecting a Tool that changes another system is the Owner's grant (beginner experience §7.4): no second
  // confirmation, only a consequence line that names those Tools.
  const writeTools = selectedTeamTools.filter((tool) =>
    (tool.risk === "external_write" || tool.risk === "destructive") && tool.selectable !== false && !tool.locked_reason,
  );
  const writeSelected = writeTools.length > 0;
  const writeConsequence = t("write_consequence", {
    names: writeTools.map((tool) => teamToolLabel(tool)).join(t("list_separator")),
  });
  // edit mode: a selected tool_ref that's no longer in the live catalog (team tool disabled/removed)
  // renders no picker row → can't be deselected → PATCH would 422 on _validate_tool_refs, blocking
  // ALL edits. Surface stale refs as removable rows + block advancing until they're cleared.
  const staleSel = catalog
    ? [...sel].filter((n) => !teamTools.some((tool) => tool.tool_name === n) && !nativeTools.includes(n))
    : [];

  const teamSkills = skillsCatalog?.team_skills ?? [];
  const builtinSkills = skillsCatalog?.builtin ?? [];
  const personalSkills = revisioned?.catalog.skills.filter((skill) => skill.source_kind === "user") ?? [];
  const skillStatus = (name: string) => {
    const skill = revisioned?.catalog.skills.find((entry) => entry.name === name);
    if (!skill) return { issue: undefined, disabled: false };
    const audience = revisioned?.detail.working_copy?.audience.effective_mode ?? revisioned?.detail.live.audience.effective_mode;
    const audienceBlocked = !audience || !skill.allowed_audiences.includes(audience);
    const missing = (skill.required_skills ?? []).filter((required) => !skillSel.has(required));
    return {
      issue: audienceBlocked ? t("skill_audience_blocked") : missing.length ? t("skill_requires", { names: missing.join("、") }) : undefined,
      disabled: audienceBlocked && !skillSel.has(name),
    };
  };
  const knownSkillNames = new Set([...teamSkills, ...builtinSkills, ...personalSkills].map((s) => s.name));
  const skillsLoading = skillsCatalog === null && !skillsCatErr;
  // selected names not in the loaded catalog. While the catalog is still loading this is ALL
  // selected names — render them (as plain rows) so prefilled skills are visible/removable
  // immediately rather than hidden behind "加载中…". Once loaded, genuinely-absent names remain
  // here (with a "不在目录" warning) so an edit never silently drops them.
  const extraSkills = [...skillSel].filter((n) => !knownSkillNames.has(n));
  const dependencyLabel = (item: AgentBlockedItem): string | undefined => {
    if (item.kind === "skill") {
      return [...teamSkills, ...builtinSkills, ...personalSkills].find((skill) => skill.name === item.name)?.display_name || undefined;
    }
    if (item.kind !== "tool") return undefined;
    return nativeTools.includes(item.identity) || !item.identity.startsWith("teammcp.") ? nativeMeta(item.identity).title : undefined;
  };
  dependencyLinesRef.current = (details: unknown) => blockedItemLines(
    dependencyIssueItems(details), (key, values) => tMarket(key as never, values as never), dependencyLabel,
  );

  const nameValid = NAME_RE.test(slug.trim());
  const schemaValid = useMemo(() => {
    const txt = schemaText.trim();
    if (!txt) return true;
    try {
      const v = JSON.parse(txt);
      return typeof v === "object" && v !== null && !Array.isArray(v);
    } catch {
      return false;
    }
  }, [schemaText]);
  // Both texts become part of the subagent definition: the routing description tells
  // Bowhead when to call it, while the work instructions tell it how to work. The API
  // requires both, so keep an incomplete definition out of the later submit step.
  const canNextDescribe = nameValid && !!desc.trim() && !!persona.trim();
  // staleSel.length===0 must also gate the configure-step submit (not just the tools-step Next button):
  // a fast describe→tools→configure before the catalog loads could otherwise submit a removed team tool
  // and 422 on _validate_tool_refs. In create mode sel only holds catalog entries → staleSel is always [].
  const emptyToolsAllowed = catalog?.text_only_allowed === true || !!(editing && editing.tool_refs.length === 0);
  const toolSelectionValid = (sel.size > 0 || hasBoundResources || emptyToolsAllowed) && staleSel.length === 0;
  // Explain the disabled Next only once the catalog has loaded (a load failure has its own
  // callout); the rule itself comes from the server's text_only_allowed capability.
  const toolRequiredBlocking = catalog !== null && sel.size === 0 && !hasBoundResources && !emptyToolsAllowed;
  const canSubmit =
    nameValid && !!desc.trim() && !!persona.trim() && toolSelectionValid &&
    (!unifiedDraftEnabled || isEditing || catalog !== null && Boolean(manualDraft?.actions.edit)) &&
    iters >= MIN_ITER && iters <= MAX_ITER && schemaValid &&
    (!unifiedDraftEnabled || isEditing || skillSel.size === 0) &&
    (!unifiedDraftEnabled || isEditing || sel.size > 0 || hasBoundResources || textOnlyConfirmed) &&
    (!initialDraft || (catalog !== null && skillsCatalog !== null && extraSkills.length === 0 && !approvalLockedSelected)) &&
    (!revisioned || (
      [...sel].every((ref) => {
        const tool = revisioned.catalog.tools.find((entry) => entry.stable_ref === ref);
        return Boolean(tool && (tool.selectable || editing?.tool_refs.includes(ref)));
      }) &&
      [...skillSel].every((name) => {
        const skill = revisioned.catalog.skills.find((entry) => entry.name === name);
        return Boolean(skill && skill.allowed_audiences.includes(revisioned.detail.working_copy?.audience.effective_mode ?? revisioned.detail.live.audience.effective_mode)
          && (skill.required_skills ?? []).every((required) => skillSel.has(required)));
      })
    ));

  const manualDefinition: ManualSubagentDraftDefinition = {
    name: slug.trim(), display_name: displayName.trim(), description: desc.trim(),
    system_prompt: persona.trim(), tool_refs: [...sel].sort(), skill_grants: boundSkills, capability_ids: boundPlugins, max_iterations: iters,
    ...(builtinPlugins.length ? { builtin_plugin_ids: builtinPlugins } : {}),
    model, output_schema: schemaValid && schemaText.trim()
      ? JSON.parse(schemaText) as Record<string, unknown> : null,
    output_schema_text: schemaText, visibility: "creator_only",
  };
  const manualPurpose = manualPurposeOverride ?? (desc.trim() || initialPurpose);
  const manualSnapshot = JSON.stringify({ purpose: manualPurpose, definition: manualDefinition, textOnlyConfirmed });
  const manualSnapshotRef = useRef("");
  manualSnapshotRef.current = manualSnapshot;
  const manualInputRef = useRef({ purpose: manualPurpose, definition: manualDefinition, textOnlyConfirmed: textOnlyConfirmed && sel.size === 0, snapshot: manualSnapshot });
  manualInputRef.current = { purpose: manualPurpose, definition: manualDefinition, textOnlyConfirmed: textOnlyConfirmed && sel.size === 0, snapshot: manualSnapshot };
  const failedAutoSaveSnapshotRef = useRef<string | null>(null);
  const hasManualInput = Boolean(
    slug.trim() || displayName.trim() || desc.trim() || persona.trim()
    || sel.size || schemaText.trim() || textOnlyConfirmed,
  );
  const manualDirty = unifiedDraftEnabled && !isEditing && step !== "success" && (
    manualDraft === null ? hasManualInput : manualDraft.purpose !== manualPurpose
    || (manualDraft.definition.name ?? "") !== manualDefinition.name
    || (manualDraft.definition.display_name ?? "") !== manualDefinition.display_name
    || (manualDraft.definition.description ?? "") !== manualDefinition.description
    || (manualDraft.definition.system_prompt ?? "") !== manualDefinition.system_prompt
    || (manualDraft.definition.tool_refs ?? []).slice().sort().join("\0") !== manualDefinition.tool_refs?.join("\0")
    || JSON.stringify(manualDraft.definition.skill_grants ?? []) !== JSON.stringify(boundSkills)
    || JSON.stringify(manualDraft.definition.capability_ids ?? []) !== JSON.stringify(boundPlugins)
    || JSON.stringify(manualDraft.definition.builtin_plugin_ids ?? []) !== JSON.stringify(builtinPlugins)
    || (manualDraft.definition.max_iterations ?? 12) !== manualDefinition.max_iterations
    || (manualDraft.definition.model ?? "analyst") !== manualDefinition.model
    || (manualDraft.definition.output_schema_text ?? "") !== schemaText
    || Boolean(manualDraft.warnings.some((warning) => warning.code === "subagent_text_only_confirmation")) !== (textOnlyConfirmed && sel.size === 0)
  );
  const localConflictValues = manualConflictValues(manualDefinition, textOnlyConfirmed);
  const baselineConflictValues = manualDraft
    ? manualConflictValues(manualDraft.definition, manualDraft.warnings.some((warning) => warning.code === "subagent_text_only_confirmation"))
    : null;
  const latestConflictValues = manualConflict
    ? manualConflictValues(manualConflict.latest.definition, manualConflict.latest.warnings.some((warning) => warning.code === "subagent_text_only_confirmation"))
    : null;
  const conflictFields = latestConflictValues
    ? MANUAL_CONFLICT_FIELDS.filter(({ key }) => localConflictValues[key] !== latestConflictValues[key])
    : [];
  const manualDirtyRef = useRef(manualDirty);
  manualDirtyRef.current = manualDirty;
  const manualDraftVersionRef = useRef(manualDraft?.version ?? 0);
  manualDraftVersionRef.current = manualDraft?.version ?? 0;
  const installManualDraft = (latest: ManualSubagentDraftDetail) => {
    if (!mountedRef.current || latest.draft_id !== manualDraftRef.current?.draft_id
      || latest.version < manualDraftVersionRef.current) return;
    manualDraftRef.current = latest;
    manualDraftVersionRef.current = latest.version;
    savedManualSnapshotRef.current = null;
    setManualDraft(latest);
    setSlug(latest.definition.name ?? "");
    setDisplayName(latest.definition.display_name ?? "");
    setDesc(latest.definition.description ?? "");
    setPersona(latest.definition.system_prompt ?? "");
    setSel(new Set(latest.definition.tool_refs ?? []));
    setBoundSkills(latest.definition.skill_grants ?? []);
    setBoundPlugins(latest.definition.capability_ids ?? []);
    setBuiltinPlugins(latest.definition.builtin_plugin_ids ?? []);
    setModel(latest.definition.model ?? "analyst");
    setIters(latest.definition.max_iterations ?? 12);
    setSchemaText(latest.definition.output_schema_text ?? (latest.definition.output_schema ? JSON.stringify(latest.definition.output_schema, null, 2) : ""));
    setManualPurposeOverride(latest.purpose);
    setTextOnlyConfirmed(latest.warnings.some((warning) => warning.code === "subagent_text_only_confirmation"));
    manualDirtyRef.current = false;
  };
  useEffect(() => {
    if (!unifiedDraftEnabled || isEditing) return;
    if (step === "success") { onDraftChange?.(null); return; }
    onDraftChange?.(manualDraft?.actions.edit
      ? { draftId: manualDraft.draft_id, version: manualDraft.version, dirty: finalizingRef.current || manualDirty || manualSaveStatus === "saving" || externalMutation || unknownSave !== null }
      : null);
  }, [unifiedDraftEnabled, isEditing, step, manualDraft?.draft_id, manualDraft?.version, manualDraft?.actions.edit, manualDirty, manualSaveStatus, externalMutation, unknownSave, busy, onDraftChange]);
  useEffect(() => {
    if (!manualDraft || refreshVersion === null || refreshVersion <= manualDraft.version) return;
    if (unknownSaveRef.current || finalizingRef.current) return;
    if (manualDirty) {
      setError(shownError(t("draft_chat_conflict")));
      return;
    }
    let cancelled = false;
    void readManualDraft(manualDraft.draft_id).then((latest) => {
      if (cancelled || manualDirtyRef.current || unknownSaveRef.current || finalizingRef.current || latest.version <= manualDraftVersionRef.current) return;
      if (manualSavesInFlightRef.current || externalMutationRef.current) return;
      installManualDraft(latest);
      setError(null);
    }).catch((cause: unknown) => {
      if (!cancelled) showError(cause);
    });
    return () => { cancelled = true; };
  }, [manualDraft?.draft_id, manualDraft?.version, refreshVersion, manualDirty, unknownSave, busy, readManualDraft, t]);

  const persistManual = async (): Promise<ManualSubagentDraftDetail> => {
    if (unknownSaveRef.current) throw new CreateAgentNotice(t("draft_unknown_save"));
    if (manualDraftRef.current && !manualDraftRef.current.actions.edit) throw new CreateAgentNotice(t("draft_readonly"));
    if (externalMutationRef.current) throw new CreateAgentNotice(t("suggestion_save_first"));
    // Serialize all entry points, including explicit save during autosave.
    while (manualSavePromiseRef.current) await manualSavePromiseRef.current;
    const input = manualInputRef.current;
    if (manualDraftRef.current && savedManualSnapshotRef.current === input.snapshot) return manualDraftRef.current;
    if (!mountedRef.current) throw new Error("Draft editor is no longer active");
    const savingSnapshot = input.snapshot;
    manualSavesInFlightRef.current = 1;
    setManualSaveStatus("saving");
    const save = (async () => {
      try {
        const existing = manualDraftRef.current ?? await createManualSubagentDraft(initialPurpose, manualCreateKey.current, locale, "manual", requestContext.current);
        const saved = await saveManualSubagentDraft(existing, input.purpose, input.definition, input.textOnlyConfirmed);
        if (!mountedRef.current) return saved;
        if (saved.version < manualDraftVersionRef.current) throw new CreateAgentNotice(t("draft_chat_conflict"));
        manualDraftRef.current = saved;
        manualDraftVersionRef.current = saved.version;
        savedManualSnapshotRef.current = savingSnapshot;
        setManualDraft(saved);
        failedAutoSaveSnapshotRef.current = null;
        setManualSaveFailed(false);
        setSavedAt(Date.now());
        setManualSaveStatus(manualSnapshotRef.current === savingSnapshot ? "saved" : "idle");
        if (manualSnapshotRef.current === savingSnapshot) manualDirtyRef.current = false;
        onManualDraftSaved?.(saved.draft_id);
        return saved;
      } catch (cause) {
        if (!mountedRef.current) throw cause;
        failedAutoSaveSnapshotRef.current = savingSnapshot;
        if (cause instanceof SubagentDraftApiError && cause.status === 409 && manualDraftRef.current) {
          try {
            const latest = await readManualDraft(manualDraftRef.current.draft_id);
            if (!mountedRef.current) throw cause;
            if (latest.actions.edit) {
              setManualConflict({ latest, choices: {} });
              setManualSaveFailed(false);
            } else {
              setManualSaveFailed(true);
            }
          } catch {
            if (mountedRef.current) setManualSaveFailed(true);
          }
        } else {
          setManualSaveFailed(true);
          if (manualDraftRef.current && (!(cause instanceof SubagentDraftApiError) || cause.status >= 500)) {
            const unknown = { baseline: manualDraftRef.current, snapshot: savingSnapshot };
            unknownSaveRef.current = unknown;
            setUnknownSave(unknown);
          }
        }
        if (mountedRef.current) setManualSaveStatus("idle");
        throw cause;
      }
    })();
    manualSavePromiseRef.current = save;
    try { return await save; }
    finally {
      if (manualSavePromiseRef.current === save) {
        manualSavePromiseRef.current = null;
        manualSavesInFlightRef.current = 0;
      }
    }
  };

  const externalPortRef = useRef<AgentDraftEditorPort | null>(null);
  externalPortRef.current = {
    requestClose: () => closeManual(),
    flush: async () => {
      if (!mountedRef.current || unknownSaveRef.current) return;
      if (manualDirtyRef.current || manualSavesInFlightRef.current > 0) await persistManual();
    },
    read: () => ({ draft: manualDraftRef.current, dirty: finalizingRef.current || manualDirtyRef.current || manualSavesInFlightRef.current > 0 || externalMutationRef.current || unknownSaveRef.current !== null || !mountedRef.current }),
    mutate: async (operation) => {
      if (finalizingRef.current || externalMutationRef.current || unknownSaveRef.current || manualDirtyRef.current || manualConflict || !mountedRef.current) throw new CreateAgentNotice(t("suggestion_save_first"));
      externalMutationRef.current = true;
      setExternalMutation(true);
      let operationDispatched = false;
      try {
        if (manualSavePromiseRef.current) await manualSavePromiseRef.current;
        const baseline = manualDraftRef.current;
        const snapshot = manualSnapshotRef.current;
        if (!baseline || !baseline.actions.edit || manualDirtyRef.current || !mountedRef.current) throw new CreateAgentNotice(t("suggestion_save_first"));
        const latest = await readManualDraft(baseline.draft_id);
        if (!mountedRef.current || manualDirtyRef.current || manualSnapshotRef.current !== snapshot) throw new CreateAgentNotice(t("suggestion_save_first"));
        if (latest.version !== baseline.version || !latest.actions.edit) {
          installManualDraft(latest);
          throw new CreateAgentNotice(t("suggestion_version_changed"));
        }
        operationDispatched = true;
        const updated = await operation(latest);
        if (!mountedRef.current) return updated;
        if (manualSnapshotRef.current === snapshot && !manualDirtyRef.current) installManualDraft(updated);
        else setManualConflict({ latest: updated, choices: {} });
        onManualDraftSaved?.(updated.draft_id);
        return updated;
      } catch (cause) {
        if (operationDispatched && mountedRef.current && manualDraftRef.current && (!(cause instanceof SubagentDraftApiError) || cause.status >= 500) && !(cause instanceof CreateAgentNotice)) {
          const unknown = { baseline: manualDraftRef.current, snapshot: manualSnapshotRef.current };
          unknownSaveRef.current = unknown;
          setUnknownSave(unknown);
        }
        throw cause;
      } finally {
        externalMutationRef.current = false;
        if (mountedRef.current) setExternalMutation(false);
      }
    },
  };
  useEffect(() => {
    if (!controllerRef) return;
    const port: AgentDraftEditorPort = {
      read: () => externalPortRef.current!.read(),
      flush: () => externalPortRef.current!.flush(),
      mutate: (operation) => externalPortRef.current!.mutate(operation),
      requestClose: () => externalPortRef.current!.requestClose(),
    };
    controllerRef.current = port;
    return () => { if (controllerRef.current === port) controllerRef.current = null; };
  }, [controllerRef]);

  const resolveManualConflict = () => {
    if (!manualConflict || conflictFields.some(({ key }) => !manualConflict.choices[key])) return;
    const { latest, choices } = manualConflict;
    const server = latest.definition;
    if (choices.name === "latest") setSlug(server.name ?? "");
    if (choices.display_name === "latest") setDisplayName(server.display_name ?? "");
    if (choices.description === "latest") setDesc(server.description ?? "");
    if (choices.system_prompt === "latest") setPersona(server.system_prompt ?? "");
    if (choices.tool_refs === "latest") setSel(new Set(server.tool_refs ?? []));
    if (choices.skill_grants === "latest") setBoundSkills(server.skill_grants ?? []);
    if (choices.capability_ids === "latest") {
      setBoundPlugins(server.capability_ids ?? []);
      setBuiltinPlugins(server.builtin_plugin_ids ?? []);
    }
    if (choices.model === "latest") setModel(server.model ?? "analyst");
    if (choices.max_iterations === "latest") setIters(server.max_iterations ?? 12);
    if (choices.output_schema_text === "latest") setSchemaText(latestConflictValues?.output_schema_text ?? "");
    if (choices.text_only_confirmed === "latest") {
      setTextOnlyConfirmed(latest.warnings.some((warning) => warning.code === "subagent_text_only_confirmation"));
    }
    setManualPurposeOverride(latest.purpose);
    setManualDraft(latest);
    savedManualSnapshotRef.current = null;
    setManualConflict(null);
    setError(null);
    setManualSaveFailed(false);
    failedAutoSaveSnapshotRef.current = null;
  };

  useEffect(() => {
    if (!unifiedDraftEnabled || isEditing || step === "success" || !manualDirty || manualConflict || leavePromptOpen
      || manualSaveStatus === "saving" || failedAutoSaveSnapshotRef.current === manualSnapshot) return;
    const timer = window.setTimeout(() => {
      if (leavePromptOpenRef.current || manualSavesInFlightRef.current > 0 || externalMutationRef.current || unknownSaveRef.current) return;
      void persistManual().catch((cause: unknown) => showError(cause));
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [unifiedDraftEnabled, isEditing, step, manualDirty, manualSaveStatus, manualSnapshot, manualConflict, leavePromptOpen]);

  const canvasLeaveRef = useRef<() => Promise<boolean>>(async () => true);
  canvasLeaveRef.current = async () => {
    activateRef.current?.();
    if (!manualConflict) {
      try {
        const snapshot = manualSnapshotRef.current;
        await persistManual();
        if (!mountedRef.current) return false;
        if (manualSnapshotRef.current === snapshot && !manualDirtyRef.current) return true;
      } catch (cause) {
        if (mountedRef.current) showError(cause);
      }
    }
    if (!mountedRef.current) return false;
    const discard = await confirmDiscard(displayName.trim() || slug.trim() || t("entry_untitled"));
    if (discard && manualDraftRef.current && mountedRef.current) {
      installManualDraft(manualDraftRef.current);
      setManualConflict(null);
      setManualSaveFailed(false);
    }
    return discard;
  };
  const requestLeave = useCallback((): Promise<boolean> => {
    if (finalizingRef.current) return Promise.resolve(false);
    if (unknownSaveRef.current) { activateRef.current?.(); return Promise.resolve(false); }
    if (externalMutationRef.current) return Promise.resolve(false);
    if (!manualDirtyRef.current && manualSavesInFlightRef.current === 0) {
      return Promise.resolve(true);
    }
    if (presentation === "canvas") return canvasLeaveRef.current();
    // A second destination replaces the first while the prompt is open. Never
    // resume two navigations after the user confirms one leave decision.
    leaveDecisionRef.current?.(false);
    leaveDecisionRef.current = null;
    leavePromptOpenRef.current = true;
    setLeavePromptOpen(true);
    activateRef.current?.();
    leavePromiseRef.current = new Promise<boolean>((resolve) => { leaveDecisionRef.current = resolve; });
    return leavePromiseRef.current;
  }, [presentation]);

  const finishLeave = (allowed: boolean) => {
    if (allowed) {
      if (closeAfterLeaveRef.current) onClose();
    }
    leavePromptOpenRef.current = false;
    setLeavePromptOpen(false);
    leaveDecisionRef.current?.(allowed);
    leaveDecisionRef.current = null;
    leavePromiseRef.current = null;
    closeAfterLeaveRef.current = false;
  };

  useEffect(() => {
    if (!unifiedDraftEnabled || isEditing || step === "success") return;
    const unregister = registerSubagentDraftNavigationGuard({
      confirmLeave: requestLeave,
      hasUnsaved: () => finalizingRef.current || manualDirtyRef.current || manualSavesInFlightRef.current > 0 || externalMutationRef.current || unknownSaveRef.current !== null,
      cancelLeave: () => {
        leaveDecisionRef.current?.(false);
        leaveDecisionRef.current = null;
        leavePromptOpenRef.current = false;
        closeAfterLeaveRef.current = false;
        if (mountedRef.current) setLeavePromptOpen(false);
      },
    });
    return () => {
      unregister();
      leaveDecisionRef.current?.(false);
      leaveDecisionRef.current = null;
      leavePromiseRef.current = null;
    };
  }, [unifiedDraftEnabled, isEditing, step, requestLeave]);

  const closeManual = () => {
    if (presentation === "canvas" && !finalizingRef.current && !externalMutationRef.current && !unknownSaveRef.current) {
      // The tab × waits for an in-flight autosave and closes when it lands; only a failed save asks (C14).
      if (!manualDirtyRef.current && manualSavesInFlightRef.current === 0) { onClose(); return; }
      void requestLeave().then((allowed) => { if (allowed && mountedRef.current) onClose(); });
      return;
    }
    if (finalizingRef.current || manualSavesInFlightRef.current > 0 || externalMutationRef.current || unknownSaveRef.current) { if (unknownSaveRef.current) activateRef.current?.(); return; }
    if (!manualDirty) { onClose(); return; }
    closeAfterLeaveRef.current = true;
    void requestLeave();
  };

  const checkSavedDraft = async () => {
    const original = unknownSaveRef.current;
    if (!original || busy) return;
    setBusy(true);
    try {
      const latest = await readManualDraft(original.baseline.draft_id);
      if (!mountedRef.current || unknownSaveRef.current !== original) return;
      if (original.creating) {
        if (!latest.result) return;
        unknownSaveRef.current = null;
        setUnknownSave(null);
        setManualDraft(latest);
        setSavedAgent({ id: latest.result.definition_id, label: latest.definition.display_name || latest.definition.name || "", visibility: "creator_only" });
        setReceiptNeedsRead(true);
        setStep("success");
        onCreated?.(original.baseline.definition.name || latest.result.definition_id, original.baseline.definition.tool_refs?.length ?? 0);
        return;
      }
      if (latest.version <= original.baseline.version) return;
      unknownSaveRef.current = null;
      setUnknownSave(null);
      const candidate = manualInputRef.current;
      const equal = latest.purpose === candidate.purpose && JSON.stringify(manualConflictValues(latest.definition,
        latest.warnings.some((warning) => warning.code === "subagent_text_only_confirmation")))
        === JSON.stringify(manualConflictValues(candidate.definition, candidate.textOnlyConfirmed));
      if (equal) { installManualDraft(latest); setManualSaveFailed(false); setError(null); }
      else { setManualConflict({ latest, choices: {} }); }
    } catch (cause) { if (mountedRef.current) showError(cause); }
    finally { if (mountedRef.current) setBusy(false); }
  };

  const saveAndLeave = async () => {
    if (!manualDirtyRef.current && manualSavesInFlightRef.current === 0) {
      finishLeave(true);
      return;
    }
    const closingSnapshot = manualSnapshotRef.current;
    setBusy(true);
    setError(null);
    try {
      await persistManual();
      if (mountedRef.current && manualSnapshotRef.current === closingSnapshot) finishLeave(true);
    }
    catch (cause) { if (mountedRef.current) showError(cause); }
    finally { if (mountedRef.current) setBusy(false); }
  };

  useEffect(() => {
    if (!manualDirty && manualSaveStatus !== "saving" && !externalMutation && !unknownSave && !finalizingRef.current) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [manualDirty, manualSaveStatus, externalMutation, unknownSave, busy]);

  useEffect(() => {
    if (!canvasMode) return;
    onCanvasState?.({ saveFailed: manualSaveFailed, creating: busy && finalizingRef.current });
  }, [canvasMode, manualSaveFailed, busy, onCanvasState]);
  useEffect(() => {
    if (!canvasMode || slugEdited || step === "success") return;
    const next = identifierFromName(displayName, manualDraft?.draft_id ?? canvasIdBase);
    if (next !== slug) setSlug(next);
  }, [canvasMode, slugEdited, displayName, manualDraft?.draft_id, canvasIdBase, slug, step]);
  useEffect(() => {
    // D9: the Advanced section opens only when the identifier needs attention.
    if (canvasMode && !nameValid && (slugEdited || triedCreate)) setAdvancedOpen(true);
  }, [canvasMode, nameValid, slugEdited, triedCreate]);

  const doSubmit = async () => {
    if (finalizingRef.current) return;
    finalizingRef.current = true;
    setBusy(true);
    setError(null);
    let creatingDraft: ManualSubagentDraftDetail | null = null;
    try {
      const output_schema = schemaText.trim()
        ? (JSON.parse(schemaText) as Record<string, unknown>) : null;
      if (editing) {
        if (revisioned) {
          const source = revisioned.detail.working_copy ?? revisioned.detail.live;
          const outputSchema = output_schema;
          const toolChanged = [...sel].sort().join("\0") !== [...editing.tool_refs].sort().join("\0");
          const skillChanged = [...skillSel].sort().join("\0") !== [...editing.skills].sort().join("\0");
          const teamConnectionIds = [...new Set([...sel].flatMap((ref) => {
            const tool = revisioned.catalog.tools.find((entry) => entry.stable_ref === ref);
            if (!tool || tool.source_kind !== "team_mcp") return [];
            return [tool.connection_id || ref.split(".")[1]];
          }).filter((id): id is string => Boolean(id)))].sort();
          const grants = [...skillSel].map((name) => revisioned.catalog.skills.find((skill) => skill.name === name)?.grant);
          if ((skillChanged || !Array.isArray(source.definition.skill_grants)) && grants.some((grant) => !grant)) throw new CreateAgentNotice(t("copilot_blocked"));
          const nextDefinition = { ...source.definition };
          // Plugin selections are written only when they changed; an empty built-in list is omitted.
          const sameIds = (left: string[], right: string[]) => [...left].sort().join("\0") === [...right].sort().join("\0");
          if (!sameIds(boundPlugins, revisionedPluginIds("capability_ids"))) nextDefinition.capability_ids = [...boundPlugins];
          if (!sameIds(builtinPlugins, revisionedPluginIds("builtin_plugin_ids"))) {
            if (builtinPlugins.length) nextDefinition.builtin_plugin_ids = [...builtinPlugins].sort();
            else delete nextDefinition.builtin_plugin_ids;
          }
          delete nextDefinition.tool_refs_json;
          delete nextDefinition.skills;
          delete nextDefinition.skills_json;
          delete nextDefinition.output_schema_json;
          await saveAgentWorkingCopy(editing.id, {
            definition: {
              ...nextDefinition,
              system_prompt: persona.trim(), output_schema: outputSchema,
              model: typeof source.definition.model === "string" && revisioned.catalog.model_policy.allowed_roles.includes(source.definition.model)
                ? source.definition.model : revisioned.catalog.model_policy.effective_role,
              ...(toolChanged || !Array.isArray(source.definition.tool_refs)
                ? { tool_refs: [...sel].sort(), team_tool_connections: toolChanged ? teamConnectionIds : source.definition.team_tool_connections ?? teamConnectionIds }
                : {}),
              ...(skillChanged || !Array.isArray(source.definition.skill_grants) ? { skill_grants: grants } : {}),
            },
            listing: { ...source.listing, display_name: displayName.trim(), description: desc.trim() },
            audience: source.audience,
            expected_row_version: revisioned.detail.working_copy?.row_version ?? null,
          });
          setSavedAgent({ id: editing.id, label: displayName.trim() || editing.name, visibility: editing.visibility });
          onSaved?.(editing.name, sel.size, displayName.trim());
          setStep("success");
          return;
        }
        // edit mode: PATCH. name is immutable + visibility is not patchable (the server has no field
        // for it); status is managed from the settings list toggle, so it's omitted (= kept).
        const sa = await patchSubagent(
          editing.id,
          {
            display_name: displayName.trim(),
            description: desc.trim(),
            system_prompt: persona.trim(),
            tool_refs: [...sel],
            skills: [...skillSel],
            output_schema,
          },
          editing.etag,
        );
        setSavedAgent({ id: sa.id, label: sa.display_name || sa.name, visibility: sa.visibility });
        onSaved?.(sa.name, sa.tool_count, sa.display_name);
      } else {
        if (unifiedDraftEnabled) {
          if (sel.size === 0 && !hasBoundResources && !textOnlyConfirmed) throw new CreateAgentNotice(t("draft_text_only_confirm"));
          const saved = await persistManual();
          if (!mountedRef.current) return;
          const reviewed = await validateManualSubagentDraft(saved);
          if (!mountedRef.current) return;
          setManualDraft(reviewed);
          if (!reviewed.actions.create) {
            // Do not show validation codes: explain each issue and remedy; use server explanations when provided.
            const reasons = [...new Set(reviewed.validation.issues.flatMap((issue) => {
              const named = dependencyLinesRef.current(issue.details);
              return named.length ? named : [issue.message || t(createAgentErrorKey({ code: issue.code, field: issue.field }))];
            }))];
            throw new CreateAgentNotice(reasons.join(" ") || t("draft_review_failed"));
          }
          // The external-change warning is acknowledged by the selection itself: once granted, nothing asks again.
          creatingDraft = reviewed;
          const created = await createSubagentFromManualDraft(
            reviewed, reviewed.warnings.map((warning) => warning.code),
          );
          if (!mountedRef.current) return;
          setSavedAgent({ id: created.definition_id, label: reviewed.definition.display_name || reviewed.definition.name || created.definition_id, visibility: "creator_only" });
          onCreated?.(reviewed.definition.name || created.definition_id, reviewed.definition.tool_refs?.length ?? 0);
        } else {
          const sa = await createSubagent({
          name: slug.trim(),
          display_name: displayName.trim(),
          description: desc.trim(),
          system_prompt: persona.trim(),
          tool_refs: [...sel],
          skills: [...skillSel],
          model,
          output_schema,
          visibility: "creator_only",
          });
          setSavedAgent({ id: sa.id, label: sa.display_name || sa.name, visibility: sa.visibility });
          onCreated?.(sa.name, sa.tool_count);
        }
      }
      setStep("success");
    } catch (e) {
      if (!mountedRef.current) return;
      if (creatingDraft && (!(e instanceof SubagentDraftApiError) || e.status >= 500)) {
        const unknown = { baseline: creatingDraft, snapshot: manualSnapshotRef.current, creating: true };
        unknownSaveRef.current = unknown;
        setUnknownSave(unknown);
      }
      showError(e);
    } finally {
      finalizingRef.current = false;
      if (mountedRef.current) setBusy(false);
    }
  };

  const stepIdx = step === "describe" ? 0 : step === "tools" ? 1 : step === "success" ? 3 : 2;
  const suggestedLabels = suggestedFields.map((field) => ({
    display_name: t("display_label"), description: t("desc_label"), system_prompt: t("persona_label"),
    tool_refs: t("step_tools"), skills: t("skills_label"),
    output_schema: t("schema_label"),
  })[field as CopilotField]).filter(Boolean).join(" · ");
  const flush = embedded || presentation === "canvas";
  const wrap: React.CSSProperties = {
    border: flush ? "none" : "1px solid var(--c-line)",
    borderRadius: flush ? 0 : "var(--r-4)",
    background: presentation === "canvas" ? "transparent" : "var(--c-surface)",
    overflow: "hidden", boxShadow: flush ? "none" : "var(--shadow-float)",
  };

  const header = (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 12, padding: "16px 18px 14px" }}>
      <span style={{
        width: 36, height: 36, flex: "0 0 36px", borderRadius: 11, background: "var(--c-accent-soft)",
        color: "var(--c-accent)", display: "grid", placeItems: "center",
      }}><Icon name="bot" s={19} /></span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 9, flexWrap: "wrap" }}>
          <h3 style={{ margin: 0, fontSize: "1.03125rem", fontWeight: 600, color: "var(--c-ink)", letterSpacing: "-.01em" }}>
            {isEditing ? t("header_title_edit") : t("header_title_create")}
          </h3>
          <span className="bh-mono" style={{
            fontSize: "0.625rem", color: "var(--c-ink-4)", background: "var(--c-surface-2)",
            border: "1px solid var(--c-line)", borderRadius: 999, padding: "2px 8px",
          }}>{isEditing ? "edit_subagent" : t("header_badge_create")}</span>
        </div>
        <p style={{ margin: "4px 0 0", fontSize: "0.8125rem", color: "var(--c-ink-3)" }}>
          {isEditing ? t("header_sub_edit") : t("header_sub_create")}
        </p>
        {step !== "success" ? (
          <p role="status" style={{ margin: "8px 0 0", fontSize: "0.75rem", color: "var(--c-ink-2)", lineHeight: 1.5 }}>
            {unifiedDraftEnabled && !isEditing
              ? t(manualSaveStatus === "saving" ? "draft_saving"
                : manualDirty ? leavePromptOpen ? "draft_unsaved_paused" : "draft_unsaved"
                  : manualDraft && chatDraftEditEnabled ? "draft_chat_connected"
                  : manualDraft ? "draft_saved" : "draft_new")
              : isEditing && initialDraft && draftSubscribe
              ? t("chat_connected")
              : t(isEditing ? "chat_unbound_edit" : "chat_unbound_create")}
          </p>
        ) : null}
        {initialDraft && editing ? (
          <div style={{ marginTop: 8, fontSize: "0.75rem", color: "var(--c-ink-2)" }}>
            <strong>{t("copilot_target", { name: editing.display_name || editing.name })}</strong>
            <div>{t("copilot_summary", { count: suggestedFields.length })}</div>
            {suggestedLabels ? <div>{suggestedLabels}</div> : null}
          </div>
        ) : null}
      </div>
      {unifiedDraftEnabled && !isEditing && step !== "success" ? (
        <button type="button" disabled={busy || manualSaveStatus === "saving" || manualConflict !== null || unknownSave !== null || manualDraft?.actions.edit === false}
          onClick={() => void persistManual().catch((cause: unknown) => showError(cause))}
          style={{ ...btnGhost(), whiteSpace: "nowrap" }}>{t("draft_save")}</button>
      ) : null}
      {!hideClose ? <button type="button" disabled={busy || manualSaveStatus === "saving"}
        aria-label={t("close_aria")} onClick={closeManual} style={{
        width: 28, height: 28, flex: "0 0 28px", border: "none", background: "transparent",
        color: "var(--c-ink-4)", cursor: "pointer", borderRadius: 8, display: "grid", placeItems: "center",
      }}><Icon name="x" s={17} /></button> : null}
    </div>
  );

  // ===== STEP ① DESCRIBE =====
  function renderDescribe() {
    return (
      <div style={{ padding: "16px 18px 18px", display: "flex", flexDirection: "column", gap: 16 }}>
        <div>
          <FieldLabel htmlFor={slugId} hint={isEditing ? t("hint_immutable") : t("slug_hint")}>{t("slug_label")}</FieldLabel>
          <div style={{
            display: "flex", alignItems: "center", gap: 8, border: "1.5px solid var(--c-line)",
            borderRadius: "var(--r-1)", background: isEditing ? "var(--c-surface-2)" : "var(--c-surface)", padding: "0 12px",
          }}>
            <Icon name="hash" s={14} style={{ color: "var(--c-ink-4)", flex: "0 0 auto" }} />
            <input id={slugId} className="bh-mono" value={slug} spellCheck={false} placeholder="sales-analysis"
              disabled={isEditing} readOnly={isEditing}
              onChange={(e) => { if (!isEditing) setSlug(e.target.value.replace(/\s+/g, "-").toLowerCase()); }}
              style={{ flex: 1, border: "none", outline: "none", background: "transparent",
                padding: "10px 0", fontSize: "0.8125rem", color: isEditing ? "var(--c-ink-3)" : "var(--c-ink)",
                cursor: isEditing ? "not-allowed" : "text" }} />
          </div>
          {(!!slug || triedDescribeNext) && !nameValid ? (
            <div style={{ fontSize: "0.6875rem", color: "var(--c-err)", marginTop: 5 }}>
              {t("slug_invalid")}
            </div>
          ) : null}
        </div>
        <div>
          <FieldLabel htmlFor={displayId} hint={t("display_hint")}>{t("display_label")}</FieldLabel>
          <input id={displayId} value={displayName} maxLength={MAX_DISPLAY} onChange={(e) => { copilot.touchField("display_name"); setDisplayName(e.target.value); }}
            placeholder={t("display_placeholder")} style={fieldInput()} />
        </div>
        <div>
          <FieldLabel htmlFor={descId} hint={t("desc_hint")}>{t("desc_label")}</FieldLabel>
          <input id={descId} value={desc} maxLength={MAX_DESC} onChange={(e) => { copilot.touchField("description"); setDesc(e.target.value); }}
            placeholder={t("desc_placeholder")} style={fieldInput()} />
          {triedDescribeNext && !desc.trim() ? (
            <div style={{ fontSize: "0.6875rem", color: "var(--c-err)", marginTop: 5 }}>
              {t("desc_required")}
            </div>
          ) : null}
          <div style={{ display: "flex", alignItems: "center", gap: 7, marginTop: 6, fontSize: "0.71875rem", color: "var(--c-ink-4)" }}>
            <Icon name="route" s={13} style={{ color: "var(--c-info)" }} />
            {t("desc_router_hint")}
          </div>
        </div>
        <div>
          <FieldLabel htmlFor={personaId} hint={t("persona_hint")}>{t("persona_label")}</FieldLabel>
          <textarea id={personaId} className="bh-scroll" value={persona} rows={5} maxLength={MAX_PROMPT}
            onChange={(e) => { copilot.touchField("system_prompt"); setPersona(e.target.value); }}
            onBlur={() => setPersonaTouched(true)}
            placeholder={t("persona_placeholder")}
            style={{ width: "100%", resize: "vertical", border: "1.5px solid var(--c-line)",
              borderRadius: "var(--r-1)", padding: "11px 12px", fontSize: "0.8125rem", lineHeight: 1.65,
              color: "var(--c-ink)", background: "var(--c-surface)", outline: "none", fontFamily: "var(--f-sans)" }} />
          {!persona.trim() && (personaTouched || triedDescribeNext) ? (
            <div style={{ fontSize: "0.6875rem", color: "var(--c-err)", marginTop: 5 }}>
              {t("persona_required")}
            </div>
          ) : null}
        </div>
        {!continuous ? <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 2 }}>
          {/* aria-disabled (not disabled) keeps the button focusable/clickable so an attempt
              to continue can reveal which required field is missing. */}
          <button type="button" aria-disabled={!canNextDescribe} style={btnPrimary(!canNextDescribe)}
            onClick={() => {
              if (!canNextDescribe) { setTriedDescribeNext(true); return; }
              setStep("tools");
            }}>
            {t("next_tools")}<Icon name="chevronRight" s={16} />
          </button>
        </div> : null}
      </div>
    );
  }

  // Compact Plugin picker (brief §4.10, E2), shared by the step flow and the Canvas layout.
  // Revisioned edits (owner card and configuration drawer) read the section from the catalog they already loaded.
  const editPluginSection = isEditing ? revisioned?.catalog.plugins : undefined;
  // The draft heading and the creation receipt name each kind once: "1 Plugin · 3 Tools". A Plugin is never
  // counted as the Tools it brings, so the count matches the chips the user picked.
  const localResourceCounts = agentResourceCounts({
    tool_refs: [...sel], skill_grants: boundSkills, builtin_plugin_ids: builtinPlugins, capability_ids: boundPlugins,
  });
  const resourceSummary = (counts: AgentResourceCounts): string => {
    const parts = [
      counts.plugins ? t("chip_plugins", { n: counts.plugins }) : null,
      counts.skills ? t("chip_skills", { n: counts.skills }) : null,
      counts.tools ? t("chip_tools", { n: counts.tools }) : null,
    ].filter((part): part is string => Boolean(part));
    return parts.length ? parts.join(" · ") : t("resources_none");
  };
  const builtinPluginPicker = (unifiedDraftEnabled && !isEditing && pluginSection) || editPluginSection ? (
    <ResourcePluginPicker
      density="compact"
      plugins={pluginsFromCatalog(editPluginSection ?? pluginSection, locale)}
      selected={new Set([...builtinPlugins, ...boundPlugins])}
      canManage={Boolean((editPluginSection ?? pluginSection)?.viewer_can_manage)}
      recommended={isEditing ? undefined : recommendedPlugins}
      onToggle={(plugin) => plugin.kind === "builtin"
        ? setBuiltinPlugins((items) => items.includes(plugin.id) ? items.filter((id) => id !== plugin.id) : [...items, plugin.id].sort())
        : setBoundPlugins((items) => items.includes(plugin.id) ? items.filter((id) => id !== plugin.id) : [...items, plugin.id])}
    />
  ) : null;
  // Built-in Tools grouped like the Tools page (brief §4.10); Tools a selected Plugin brings are locked as included.
  const nativeToolPicker = natF.length ? (
    <ResourceToolPicker
      tools={natF.map((name) => ({ ref: name, name: nativeMeta(name).title, description: nativeMeta(name).desc, selectable: true }))}
      plugins={pluginsFromCatalog(editPluginSection ?? pluginSection, locale)}
      selected={sel}
      selectedPluginIds={new Set([...builtinPlugins, ...boundPlugins])}
      onToggle={toggle}
      showRef
    />
  ) : null;

  // ===== STEP ② TOOLS =====
  function renderTools() {
    return (
      <div className="bh-scroll" style={{ padding: "16px 18px 16px", display: "flex", flexDirection: "column", gap: 13 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 8, border: "1.5px solid var(--c-line)",
            borderRadius: "var(--r-1)", padding: "0 11px", background: "var(--c-surface)" }}>
            <Icon name="search" s={14} style={{ color: "var(--c-ink-4)" }} />
            <input type="search" aria-label={t("search_tools")} value={filter}
              onChange={(e) => setFilter(e.target.value)} placeholder={t("search_tools")}
              style={{ flex: 1, border: "none", background: "transparent",
                padding: "9px 0", fontSize: "0.8125rem", color: "var(--c-ink)" }} />
          </div>
          <span className="bh-mono" style={{ fontSize: "0.6875rem", fontWeight: 600, color: "var(--c-accent)",
            background: "var(--c-accent-tint)", border: "1px solid var(--c-accent-line)", borderRadius: 999,
            padding: "5px 11px", flex: "0 0 auto" }}>{t("selected_count", { n: sel.size })}</span>
        </div>

        {emptyToolsAllowed ? (
          <p style={{ margin: 0, fontSize: "0.75rem", color: "var(--c-ink-3)" }}>{t("text_only_hint")}</p>
        ) : toolRequiredBlocking ? (
          // Same slot and style as text_only_hint: the two are mutually exclusive, and this one
          // is the only reason the Next button below is disabled.
          <p id={toolsRequiredId} style={{ margin: 0, fontSize: "0.75rem", color: "var(--c-ink-3)" }}>{t("tool_required_hint")}</p>
        ) : null}
        {builtinPluginPicker}
        {unifiedDraftEnabled && !isEditing && sel.size === 0 && !hasBoundResources && emptyToolsAllowed ? (
          <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "0.75rem", color: "var(--c-ink-2)" }}>
            <input type="checkbox" checked={textOnlyConfirmed} onChange={(event) => setTextOnlyConfirmed(event.target.checked)} />
            {t("draft_text_only_confirm")}
          </label>
        ) : null}
        {unifiedDraftEnabled && !isEditing && hasBoundResources ? (
          <div style={{ display: "grid", gap: 8 }}>
            <p style={{ margin: 0, fontSize: "0.75rem" }}>{t("bound_resources_hint")}</p>
            {boundSkills.map((grant) => <div key={`${grant.skill_ref.kind}:${grant.skill_ref.id}`}>
              <span>{t("suggestion_field_skill_grants")}: {grant.skill_ref.name} ({t(`bound_skill_source_${grant.skill_ref.kind}`)}, {grant.permissions.map((permission) => t(`bound_skill_${permission}`)).join(", ")})</span>{" "}
              <button type="button" onClick={() => setBoundSkills((items) => items.filter((item) => item !== grant))}>{t("bound_resource_remove")}</button>
            </div>)}
            {boundPlugins.map((id) => <div key={id}>
              <span>{t("suggestion_field_capability_ids")}: {pluginCatalog.find((item) => item.id === id)?.name ?? t("bound_resource_unavailable")}</span>{" "}
              <button type="button" onClick={() => setBoundPlugins((items) => items.filter((item) => item !== id))}>{t("bound_resource_remove")}</button>
            </div>)}
          </div>
        ) : null}
        {approvalLockedSelected ? (
          <Callout tone="warn" icon="alertTriangle">{t("retained_approval_warning")}</Callout>
        ) : null}
        {writeSelected ? (
          <Callout tone="warn" icon="alertTriangle">{writeConsequence}</Callout>
        ) : null}
        {catalogErr ? (
          <>
            <Callout tone="warn" icon="alertTriangle">{catalogErr.message}</Callout>
            <ErrorDiagnosticsInline error={catalogErr.error} surface="settings" />
          </>
        ) : null}

        <div style={{ border: "1px solid var(--c-line)", borderRadius: "var(--r-3)", background: "var(--c-surface)", padding: 6 }}>
          <GroupHeader icon="folder" title={t("group_team_tools")} sub={t("tool_count", { n: teamF.length })} />
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {teamF.length ? teamF.map((tool) => (
              <TeamToolRow key={tool.tool_name} t={tool} on={sel.has(tool.tool_name)} onToggle={() => toggle(tool.tool_name)} />
            )) : (
              <div style={{ fontSize: "0.75rem", color: "var(--c-ink-4)", padding: "8px 12px" }}>
                {catalog ? t("no_match") : t("loading")}
              </div>
            )}
          </div>
        </div>

        <div style={{ border: "1px solid var(--c-line)", borderRadius: "var(--r-3)", background: "var(--c-surface)", padding: 6 }}>
          <GroupHeader icon="sparkles" title={t("group_native_tools")} sub={t("tool_count", { n: natF.length })} />
          <div style={{ padding: "4px 6px" }}>
            {nativeToolPicker ?? (
              <div style={{ fontSize: "0.75rem", color: "var(--c-ink-4)", padding: "8px 12px" }}>
                {catalog ? t("no_match") : t("loading")}
              </div>
            )}
          </div>
        </div>

        {staleSel.length ? (
          <div style={{ border: "1px solid var(--c-warn-line)", borderRadius: "var(--r-3)", background: "var(--c-warn-soft)", padding: 6 }}>
            <GroupHeader icon="alertTriangle" title={t("group_stale_tools")} sub={t("stale_tools_sub")} />
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {staleSel.map((n) => (
                <label key={n} style={{ display: "flex", gap: 11, alignItems: "center", padding: "10px 12px", borderRadius: "var(--r-2)", cursor: "pointer" }}>
                  <div><Check on onToggle={() => toggle(n)} /></div>
                  <code className="bh-mono" style={{ fontSize: "0.78125rem", color: "var(--c-ink-3)", textDecoration: "line-through" }}>{n}</code>
                  <span style={{ fontSize: "0.625rem", fontWeight: 600, color: "var(--c-warn)" }}>{t("stale_badge")}</span>
                </label>
              ))}
            </div>
          </div>
        ) : null}

        {!continuous ? <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 3, paddingTop: 14, borderTop: "1px solid var(--c-line-2)" }}>
          <button type="button" style={btnGhost()} onClick={() => setStep("describe")}>
            <Icon name="chevronLeft" s={15} />{t("back")}
          </button>
          <div style={{ flex: 1 }} />
          <button type="button" disabled={!toolSelectionValid} style={btnPrimary(!toolSelectionValid)}
            aria-describedby={toolRequiredBlocking ? toolsRequiredId : undefined} onClick={() => setStep("configure")}>
            {t("next_configure")}<Icon name="chevronRight" s={16} />
          </button>
        </div> : null}
      </div>
    );
  }

  // ===== STEP ③ CONFIGURE =====
  function renderConfigure() {
    return (
      <div className="bh-scroll" style={{ padding: "16px 18px 16px", display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", gap: 14 }}>
          <div style={{ flex: 1 }}>
            <FieldLabel htmlFor={modelId}>{t("model_label")}</FieldLabel>
            <div style={{ position: "relative" }}>
              <select id={modelId} value={model} disabled style={{ ...fieldInput(), appearance: "none",
                WebkitAppearance: "none", cursor: "default", paddingRight: 36, color: "var(--c-ink-2)" }}>
                {MODELS.map((m) => <option key={m} value={m}>{t("model_analyst_option")}</option>)}
              </select>
              <span style={{ position: "absolute", right: 12, top: "50%", transform: "translateY(-50%)",
                pointerEvents: "none", color: "var(--c-ink-4)" }}>
                <Icon name="chevronDown" s={16} />
              </span>
            </div>
            <div style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)", marginTop: 6 }}>{t("model_only_tier")}</div>
          </div>
        </div>

        <div>
          <FieldLabel hint={isEditing ? t("hint_immutable") : undefined}>{t("visibility_label")}</FieldLabel>
          <div style={{ display: "flex", alignItems: "center", gap: 9, padding: "12px 13px", background: "var(--c-surface-2)",
            borderRadius: "var(--r-2)", border: "1px solid var(--c-line)", color: "var(--c-ink-2)" }}>
            <Icon name={editing?.visibility === "team_wide" ? "globe" : "user"} s={17} />
            <span style={{ fontSize: "0.78125rem", fontWeight: 600 }}>
              {editing?.visibility === "team_wide" ? t("vis_team") : t("vis_me")}
            </span>
            {!isEditing ? <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)" }}>{t("create_private_hint")}</span> : null}
          </div>
        </div>

        <section aria-label={t("permission_summary_title")} style={{ border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", padding: "12px 13px" }}>
          <strong style={{ fontSize: "0.78125rem" }}>{t("permission_summary_title")}</strong>
          <p style={{ margin: "5px 0 8px", fontSize: "0.71875rem", color: "var(--c-ink-3)" }}>{t("permission_summary_note")}</p>
          {sel.size === 0 ? <p style={{ margin: 0, fontSize: "0.75rem" }}>{t("permission_no_tools")}</p> : (
            <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 5 }}>
              {[...sel].sort().map((ref) => {
                const teamTool = teamTools.find((tool) => tool.tool_name === ref);
                return <li key={ref} style={{ fontSize: "0.75rem" }}>
                  {teamTool ? teamToolLabel(teamTool) : nativeTools.includes(ref) ? nativeMeta(ref).title : ref}
                  {" · "}{teamTool ? t(teamToolRiskLabelKey(teamTool.risk)) : nativeTools.includes(ref) ? t("badge_builtin") : t("stale_badge")}
                </li>;
              })}
            </ul>
          )}
        </section>

        <div style={{ border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", background: "var(--c-surface)", overflow: "hidden" }}>
          <button type="button" onClick={() => setSchemaOpen((o) => !o)} style={{
            width: "100%", display: "flex", alignItems: "center", gap: 9, padding: "11px 13px",
            border: "none", background: "transparent", cursor: "pointer", textAlign: "left",
          }}>
            <span style={{ color: "var(--c-ink-4)", transition: "transform .15s",
              transform: schemaOpen ? "rotate(90deg)" : "none", display: "inline-flex" }}>
              <Icon name="chevronRight" s={15} />
            </span>
            <span style={{ fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)" }}>{t("schema_label")}</span>
            <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)" }}>{t("optional_advanced")}</span>
            <div style={{ flex: 1 }} />
            {!schemaOpen ? <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)" }}>{t("schema_default")}</span> : null}
          </button>
          {schemaOpen ? (
            <div style={{ padding: "0 13px 13px" }}>
              <textarea className="bh-mono bh-scroll" spellCheck={false} rows={6} value={schemaText} aria-label={t("schema_label")}
                onChange={(e) => { copilot.touchField("output_schema"); setSchemaText(e.target.value); }}
                placeholder={'{\n  "type": "object",\n  "properties": {\n    "summary": { "type": "string" }\n  },\n  "required": ["summary"]\n}'}
                style={{ width: "100%", resize: "vertical", border: `1px solid ${schemaValid ? "var(--c-line)" : "var(--c-err)"}`,
                  borderRadius: "var(--r-1)", padding: "11px 12px", fontSize: "0.75rem", lineHeight: 1.6,
                  color: "var(--c-ink)", background: "var(--c-bg)", outline: "none" }} />
              <div style={{ fontSize: "0.6875rem", color: schemaValid ? "var(--c-ink-4)" : "var(--c-err)", marginTop: 7 }}>
                {schemaValid ? t("schema_valid_hint") : t("schema_invalid_hint")}
              </div>
            </div>
          ) : null}
        </div>

        {(!unifiedDraftEnabled || isEditing) ? <div style={{ border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", background: "var(--c-surface)", overflow: "hidden" }}>
          <button type="button" onClick={() => setSkillsOpen((o) => !o)} style={{
            width: "100%", display: "flex", alignItems: "center", gap: 9, padding: "11px 13px",
            border: "none", background: "transparent", cursor: "pointer", textAlign: "left",
          }}>
            <span style={{ color: "var(--c-ink-4)", transition: "transform .15s",
              transform: skillsOpen ? "rotate(90deg)" : "none", display: "inline-flex" }}>
              <Icon name="chevronRight" s={15} />
            </span>
            <span style={{ fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)" }}>{t("skills_label")}</span>
            <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)" }}>{t("optional")}</span>
            <div style={{ flex: 1 }} />
            {skillSel.size ? (
              <span className="bh-mono" style={{ fontSize: "0.6875rem", fontWeight: 600, color: "var(--c-accent)",
                background: "var(--c-accent-tint)", border: "1px solid var(--c-accent-line)", borderRadius: 999, padding: "2px 9px" }}>
                {t("selected_count", { n: skillSel.size })}
              </span>
            ) : <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)" }}>{t("none")}</span>}
          </button>
          {skillsOpen ? (
            <div style={{ padding: "0 13px 13px", display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ fontSize: "0.71875rem", color: "var(--c-ink-4)", lineHeight: 1.5 }}>
                {t.rich(revisioned ? "skills_hint_revisioned" : "skills_hint", { code: (chunks) => <code className="bh-mono">{chunks}</code> })}
              </div>
              {skillsCatErr ? (
                <>
                  <Callout tone="warn" icon="alertTriangle">{skillsCatErr.message}</Callout>
                  <ErrorDiagnosticsInline error={skillsCatErr.error} surface="settings" />
                </>
              ) : null}
              {teamSkills.length ? (
                <div>
                  <GroupHeader icon="folder" title={t("group_team_skills")} sub={t("count_n", { n: teamSkills.length })} />
                  <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    {teamSkills.map((s) => (
                      <SkillToggleRow key={s.name} entry={s} badge={t("badge_team")} on={skillSel.has(s.name)} onToggle={() => toggleSkill(s.name)} {...skillStatus(s.name)} />
                    ))}
                  </div>
                </div>
              ) : null}
              {personalSkills.length ? (
                <div>
                  <GroupHeader icon="user" title={t("group_personal_skills")} sub={t("count_n", { n: personalSkills.length })} />
                  <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    {personalSkills.map((s) => (
                      <SkillToggleRow key={s.name} entry={s} badge={t("group_personal_skills")} on={skillSel.has(s.name)} onToggle={() => toggleSkill(s.name)} {...skillStatus(s.name)} />
                    ))}
                  </div>
                </div>
              ) : null}
              {builtinSkills.length ? (
                <div>
                  <GroupHeader icon="sparkles" title={t("group_builtin_skills")} sub={t("count_n", { n: builtinSkills.length })} />
                  <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    {builtinSkills.map((s) => (
                      <SkillToggleRow key={s.name} entry={s} badge={t("badge_builtin")} on={skillSel.has(s.name)} onToggle={() => toggleSkill(s.name)} {...skillStatus(s.name)} />
                    ))}
                  </div>
                </div>
              ) : null}
              {extraSkills.length ? (
                <div style={skillsLoading ? undefined : { border: "1px solid var(--c-warn-line)", borderRadius: "var(--r-3)", background: "var(--c-warn-soft)", padding: 6 }}>
                  <GroupHeader
                    icon={skillsLoading ? "sparkles" : "alertTriangle"}
                    title={skillsLoading ? t("skills_selected") : t("skills_extra_title")}
                    sub={skillsLoading ? t("skills_loading_dir") : t("skills_extra_sub")}
                  />
                  <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    {extraSkills.map((n) => (
                      <label key={n} style={{ display: "flex", gap: 11, alignItems: "center", padding: "10px 12px", borderRadius: "var(--r-2)", cursor: "pointer" }}>
                        <div><Check on onToggle={() => toggleSkill(n)} /></div>
                        <code className="bh-mono" style={{ fontSize: "0.78125rem", color: "var(--c-ink-3)" }}>{n}</code>
                        {!skillsLoading ? <span style={{ fontSize: "0.625rem", fontWeight: 600, color: "var(--c-warn)" }}>{t("not_in_catalog")}</span> : null}
                      </label>
                    ))}
                  </div>
                </div>
              ) : null}
              {!teamSkills.length && !builtinSkills.length && !personalSkills.length && !extraSkills.length ? (
                <div style={{ fontSize: "0.75rem", color: "var(--c-ink-4)", padding: "4px 2px" }}>
                  {skillsCatalog ? t("no_skills_available") : t("loading")}
                </div>
              ) : null}
            </div>
          ) : null}
        </div> : null}

        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 3, paddingTop: 14, borderTop: "1px solid var(--c-line-2)" }}>
          {!continuous ? <button type="button" style={btnGhost()} onClick={() => setStep("tools")}>
            <Icon name="chevronLeft" s={15} />{t("back")}
          </button> : null}
          <div style={{ flex: 1 }} />
          {error ? (
            <span style={{ fontSize: "0.71875rem", color: "var(--c-err)", maxWidth: 240, textAlign: "right", lineHeight: 1.4 }}>{error.message}</span>
          ) : null}
          {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
          {initialDraft && !canSubmit && !error ? (
            <span style={{ fontSize: "0.71875rem", color: "var(--c-warn)", maxWidth: 240, textAlign: "right", lineHeight: 1.4 }}>{t("copilot_blocked")}</span>
          ) : null}
          <button type="button" disabled={busy || !canSubmit || manualSaveStatus === "saving" || manualConflict !== null || unknownSave !== null || manualDraft?.actions.edit === false}
            style={btnPrimary(busy || !canSubmit || manualSaveStatus === "saving" || manualConflict !== null || unknownSave !== null)} onClick={doSubmit}>
            {busy
              ? <><span className="animate-spin" style={{ width: 13, height: 13, borderRadius: "50%",
                  border: "1.6px solid currentColor", borderRightColor: "transparent", display: "inline-block" }} />{isEditing ? t("saving") : t("creating")}</>
              : <><Icon name="bot" s={16} />{isEditing ? t("save_changes") : t(continuous ? "canvas_private_create" : "create_subagent")}</>}
          </button>
        </div>
      </div>
    );
  }

  // ===== CANVAS OBJECT (Canvas Objects Spec part 2, screens 5–7) =====
  function toolLabel(ref: string): string {
    const team = teamTools.find((tool) => tool.tool_name === ref);
    if (team) return teamToolLabel(team);
    if (nativeTools.includes(ref)) return nativeMeta(ref).title;
    return catalog ? t("tool_unavailable") : nativeMeta(ref).title;
  }
  function pluginLabel(id: string): string | null {
    if (id.startsWith("builtin:")) return pluginsFromCatalog(editPluginSection ?? pluginSection, locale).find((item) => item.id === id)?.name ?? null;
    return pluginCatalog.find((item) => item.id === id)?.name ?? null;
  }
  const fieldLabel = (field: string): string => ({
    name: t("slug_label"), display_name: t("display_label"), description: t("desc_label"), system_prompt: t("persona_label"),
    tool_refs: t("conflict_tools"), skill_grants: t("suggestion_field_skill_grants"), capability_ids: t("suggestion_field_capability_ids"),
    builtin_plugin_ids: t("suggestion_field_builtin_plugin_ids"), model: t("model_label"), max_iterations: t("max_iters_label"), output_schema_text: t("schema_label"), output_schema: t("schema_label"),
    text_only_confirmed: t("draft_text_only_confirm"),
  } as Record<string, string>)[field] ?? t("field_other");
  const formatField = (field: string, before: unknown, after: unknown): { before: ChangeValue; after: ChangeValue } => {
    if (field === "tool_refs") return formatListChange(before, after, { label: toolLabel, unavailableLabel: t("tool_unavailable") });
    if (field === "skill_grants") return formatListChange(before, after, { unavailableLabel: t("bound_resource_unavailable") });
    if (field === "capability_ids" || field === "builtin_plugin_ids") return formatListChange(before, after, { label: pluginLabel, unavailableLabel: t("bound_resource_unavailable") });
    if (field === "model") {
      const label = (value: string) => (value === "analyst" ? t("model_analyst_option") : value);
      return { before: formatChangeValue("enum", before, { label }), after: formatChangeValue("enum", after, { label }) };
    }
    if (field === "max_iterations") return { before: formatChangeValue("number", before, { locale }), after: formatChangeValue("number", after, { locale }) };
    if (field === "text_only_confirmed") return { before: formatChangeValue("toggle", before), after: formatChangeValue("toggle", after) };
    return { before: formatChangeValue("text", before), after: formatChangeValue("text", after) };
  };
  const currentField = (field: string): unknown => ({
    name: slug.trim(), display_name: displayName.trim(), description: desc.trim(), system_prompt: persona.trim(),
    tool_refs: [...sel].sort(), skill_grants: boundSkills, capability_ids: boundPlugins, builtin_plugin_ids: builtinPlugins, model, max_iterations: iters,
    output_schema_text: schemaText, text_only_confirmed: textOnlyConfirmed,
  } as Record<string, unknown>)[field];
  const suggestionFor = (fields: string[]) => {
    if (!aiSuggestions) return undefined;
    const pending = aiSuggestions.items.some((item) => aiSuggestions.fieldsOf(item).some((field) => fields.includes(field)));
    if (!pending) return undefined;
    const field = fields.find((name) => aiSuggestions.items.some((item) => aiSuggestions.fieldsOf(item).includes(name))) ?? fields[0];
    return {
      label: t("suggestion_badge"),
      onReveal: () => {
        const row = document.getElementById(suggestionRowId(canvasIdBase, field));
        row?.scrollIntoView({ block: "center", behavior: "smooth" });
        row?.focus({ preventScroll: true });
      },
    };
  };
  function conflictValue(key: ManualConflictField, definition: Partial<ManualSubagentDraftDefinition>, textOnly: boolean): unknown {
    if (key === "text_only_confirmed") return textOnly;
    if (key === "output_schema_text") return definition.output_schema_text ?? (definition.output_schema ? JSON.stringify(definition.output_schema, null, 2) : "");
    if (key === "model") return definition.model ?? "analyst";
    if (key === "max_iterations") return definition.max_iterations ?? 12;
    return definition[key as keyof ManualSubagentDraftDefinition];
  }

  function renderCanvasObject() {
    const titleId = `${canvasIdBase}-title`;
    const name = displayName.trim() || (step === "success" ? savedAgent?.label || t("canvas_created_agent") : t("entry_untitled"));
    const readOnly = manualDraft?.actions.edit === false;
    const status: CanvasObjectStatus = step === "success" ? "created" : busy && finalizingRef.current ? "creating"
      : unknownSave?.creating ? "unconfirmed" : readOnly ? "read_only" : "draft";
    const save: CanvasSaveState | undefined = step === "success" || readOnly ? undefined
      : manualSaveFailed ? "failed" : manualSaveStatus === "saving" ? "saving" : manualDirty ? "dirty" : manualDraft ? "saved" : "new";
    const toolbar = (
      <CanvasObjectToolbar
        status={<ObjectStatusBadge status={status} />}
        save={save}
        savedAt={savedAt}
        onRetrySave={() => { setManualSaveFailed(false); failedAutoSaveSnapshotRef.current = null; void persistManual().catch((cause: unknown) => showError(cause)); }}
      />
    );
    if (step === "success") {
      const definition = recoveredAgent ?? manualDraft?.definition;
      return (
        // The creation receipt carries the name; no separate object header (avoids a duplicate title).
        <CanvasObjectSurface label={name} toolbar={toolbar}>
          {savedAgent ? <AgentCreationReceipt name={savedAgent.label}
            visibility={savedAgent.visibility}
            created={!isEditing}
            summary={receiptNeedsRead ? null : {
              // The created Agent's released definition when it has been read; this session's selection otherwise.
              // A receipt reopened after a reload has no selection of its own, so it waits for the read.
              resources: receiptResources ? resourceSummary(receiptResources) : initialManualDraft?.result ? null : resourceSummary(localResourceCounts),
              visibility: savedAgent.visibility,
            }} readFailed={receiptReadFailed} onRetry={() => setReceiptReadEpoch((value) => value + 1)}
            onManage={() => router.push(`/agents/manage/?agent_id=${encodeURIComponent(savedAgent.id)}`)}
            onOpenEvaluate={() => router.push(`/agents/manage/?agent_id=${encodeURIComponent(savedAgent.id)}&section=evaluate`)}
            onOpenWho={() => router.push(`/agents/manage/?agent_id=${encodeURIComponent(savedAgent.id)}&section=config&config_section=who`)}
            onUse={canDirectUse && (!recoveredAgent || recoveredAgent.actions.bring_to_web_chat) ? () => { void confirmCanvasNavigation().then((allowed) => {
              if (allowed && mountedRef.current) handoffAgentToNewChat({ agentId: savedAgent.id, label: savedAgent.label || t("canvas_created_agent") });
            }); } : undefined} /> : null}
        </CanvasObjectSurface>
      );
    }

    const missing = [
      !desc.trim() ? t("desc_label") : null,
      !persona.trim() ? t("persona_label") : null,
      !nameValid ? t("slug_label") : null,
      toolRequiredBlocking ? t("conflict_tools") : null,
      !schemaValid ? t("schema_label") : null,
    ].filter((item): item is string => Boolean(item));
    const confirmationNeeded = !missing.length && unifiedDraftEnabled && (
      sel.size === 0 && !hasBoundResources && !textOnlyConfirmed && emptyToolsAllowed ? t("create_missing_text_only") : null);
    const creating = busy && finalizingRef.current;
    const footerMessage = creating ? t("creating_detail")
      : manualConflict ? t("conflict_footer")
        : missing.length ? t("create_missing", { fields: missing.join(t("list_separator")) })
          : confirmationNeeded ?? t("create_private_footer");
    const footerTone = creating || (!missing.length && !confirmationNeeded) ? "neutral" : missing.length && triedCreate ? "danger" : "warning";
    const blocked = busy || externalBusy || !canSubmit || manualSaveStatus === "saving" || manualConflict !== null || unknownSave !== null || readOnly;
    const footer = readOnly ? null : (
      <CanvasObjectFooter
        message={footerMessage}
        tone={footerTone}
        secondary={manualConflict ? [{ label: t("conflict_apply"), onClick: resolveManualConflict,
          disabled: conflictFields.some(({ key }) => !manualConflict.choices[key]) }] : undefined}
        primary={{
          label: t("canvas_private_create"),
          busy: creating,
          disabled: blocked && !creating,
          disabledReason: missing.length || confirmationNeeded ? footerMessage : undefined,
          onClick: () => { setTriedCreate(true); setTriedDescribeNext(true); void doSubmit(); },
        }}
      />
    );
    const selectedRefs = [...sel].sort();
    const searchId = `${canvasIdBase}-search`;
    return (
      <CanvasObjectSurface labelledBy={titleId} toolbar={toolbar} footer={footer} busy={busy || externalBusy}>
        <CanvasObjectHeader
          titleId={titleId}
          icon={<BotIcon size={14} aria-hidden />}
          eyebrow={t("canvas_eyebrow")}
          title={name}
          subtitle={t(chatDraftEditEnabled ? "canvas_edit_connected" : "canvas_edit_manual")}
        />
        {aiSuggestions ? (
          <AgentDraftSuggestions model={aiSuggestions} idBase={canvasIdBase} label={fieldLabel} current={currentField} format={formatField} />
        ) : null}
        {unknownSave ? (
          <StateBlock
            icon={<AlertTriangle size={20} />}
            title={t(unknownSave.creating ? "draft_unknown_create_title" : "draft_unknown_save_title")}
            body={t(unknownSave.creating ? "draft_unknown_create" : "draft_unknown_save")}
            secondaryActions={[{ label: t("draft_check_save"), onClick: () => void checkSavedDraft(), busy }]}
          />
        ) : null}
        {error ? <Notice tone="error">{error.message}<ErrorDiagnosticsInline error={error.error} surface="settings" /></Notice> : null}
        {manualConflict && latestConflictValues ? (
          <CanvasSection label={t("conflict_title")}>
            <ChangeRow state="conflict" location={<><AlertTriangle size={14} aria-hidden />{t("conflict_title")}</>}>
              <p className={canvasStyles.hint}>{t("conflict_description")}</p>
            </ChangeRow>
            {conflictFields.map(({ key }) => {
              const values = formatField(key,
                conflictValue(key, manualDefinition, textOnlyConfirmed),
                conflictValue(key, manualConflict.latest.definition, manualConflict.latest.warnings.some((warning) => warning.code === "subagent_text_only_confirmation")));
              return (
                <ChangeRow key={key} location={fieldLabel(key)} before={values.before} after={values.after}
                  beforeLabel={t("conflict_mine_short")} afterLabel={t("conflict_latest_short")}
                  actions={(
                    <SegmentedControl
                      aria-label={fieldLabel(key)}
                      value={manualConflict.choices[key] ?? null}
                      onValueChange={(choice: "mine" | "latest") => setManualConflict((current) => current ? { ...current, choices: { ...current.choices, [key]: choice } } : null)}
                    >
                      <Segment value="mine">{t("conflict_mine")}</Segment>
                      <Segment value="latest">{t("conflict_latest")}</Segment>
                    </SegmentedControl>
                  )} />
              );
            })}
          </CanvasSection>
        ) : null}
        <fieldset disabled={busy || externalBusy || readOnly} className={canvasStyles.fieldsetContents}>
          <CanvasSection labelledBy={`${canvasIdBase}-basic`}>
            <SectionHeading id={`${canvasIdBase}-basic`} title={t("section_basic")} />
            <div className={canvasStyles.fields}>
              <Field id={displayId} label={t("display_label")} suggestion={suggestionFor(["display_name"])}>
                <input id={displayId} className="settings-input" value={displayName} maxLength={MAX_DISPLAY}
                  placeholder={t("display_placeholder")}
                  onChange={(event) => { copilot.touchField("display_name"); setDisplayName(event.target.value); }} />
              </Field>
              <Field id={descId} label={t("desc_label")} hint={t("desc_hint")} suggestion={suggestionFor(["description"])}
                error={triedDescribeNext && !desc.trim() ? t("desc_required") : undefined}>
                <textarea id={descId} className={`settings-input ${canvasStyles.textarea}`} rows={2} value={desc} maxLength={MAX_DESC}
                  placeholder={t("desc_placeholder")} aria-describedby={fieldDescribedBy(descId, { hint: true, error: triedDescribeNext && !desc.trim() })}
                  onChange={(event) => { copilot.touchField("description"); setDesc(event.target.value); }} />
              </Field>
              <Field id={personaId} label={t("persona_label")} suggestion={suggestionFor(["system_prompt"])}
                error={!persona.trim() && (personaTouched || triedDescribeNext) ? t("persona_required") : undefined}>
                <textarea id={personaId} className={`settings-input ${canvasStyles.textarea}`} rows={5} value={persona} maxLength={MAX_PROMPT}
                  placeholder={t("persona_placeholder")} aria-describedby={fieldDescribedBy(personaId, { error: !persona.trim() && (personaTouched || triedDescribeNext) })}
                  onBlur={() => setPersonaTouched(true)}
                  onChange={(event) => { copilot.touchField("system_prompt"); setPersona(event.target.value); }} />
              </Field>
            </div>
          </CanvasSection>

          <CanvasSection labelledBy={`${canvasIdBase}-tools`}>
            <SectionHeading
              id={`${canvasIdBase}-tools`}
              title={t("section_tools")}
              count={resourceSummary(localResourceCounts)}
              action={(
                <Button size="sm" variant="ghost" aria-expanded={toolPickerOpen} aria-controls={`${canvasIdBase}-picker`} onClick={() => setToolPickerOpen((value) => !value)}>
                  {toolPickerOpen ? t("tools_done") : <><PlusIcon size={14} aria-hidden />{t("tools_add")}</>}
                </Button>
              )}
            />
            {(() => {
              const badge = suggestionFor(["tool_refs", "skill_grants", "capability_ids"]);
              return badge ? <span className={canvasStyles.alignStart}><SuggestionBadge label={badge.label} onReveal={badge.onReveal} /></span> : null;
            })()}
            {selectedRefs.length || boundSkills.length || boundPlugins.length || builtinPlugins.length ? (
              <div className={canvasStyles.chips}>
                {selectedRefs.map((ref) => (
                  <span key={ref} className={canvasStyles.chip} data-tone={staleSel.includes(ref) ? "warning" : undefined}>
                    {toolLabel(ref)}
                    <button type="button" className={canvasStyles.chipRemove} aria-label={t("tool_remove", { name: toolLabel(ref) })} onClick={() => toggle(ref)}>
                      <XIcon size={14} aria-hidden />
                    </button>
                  </span>
                ))}
                {boundSkills.map((grant) => (
                  <span key={`${grant.skill_ref.kind}:${grant.skill_ref.id}`} className={canvasStyles.chip}>
                    {grant.skill_ref.name}
                    <button type="button" className={canvasStyles.chipRemove} aria-label={t("tool_remove", { name: grant.skill_ref.name })}
                      onClick={() => setBoundSkills((items) => items.filter((item) => item !== grant))}><XIcon size={14} aria-hidden /></button>
                  </span>
                ))}
                {builtinPlugins.map((id) => (
                  <span key={id} className={canvasStyles.chip}>
                    {pluginLabel(id) ?? t("bound_resource_unavailable")}
                    <button type="button" className={canvasStyles.chipRemove} aria-label={t("tool_remove", { name: pluginLabel(id) ?? t("bound_resource_unavailable") })}
                      onClick={() => setBuiltinPlugins((items) => items.filter((item) => item !== id))}><XIcon size={14} aria-hidden /></button>
                  </span>
                ))}
                {boundPlugins.map((id) => (
                  <span key={id} className={canvasStyles.chip}>
                    {pluginLabel(id) ?? t("bound_resource_unavailable")}
                    <button type="button" className={canvasStyles.chipRemove} aria-label={t("tool_remove", { name: pluginLabel(id) ?? t("bound_resource_unavailable") })}
                      onClick={() => setBoundPlugins((items) => items.filter((item) => item !== id))}><XIcon size={14} aria-hidden /></button>
                  </span>
                ))}
              </div>
            ) : hasBoundResources ? null : (
              <p className={canvasStyles.hint}>{emptyToolsAllowed ? t("text_only_hint") : catalog ? t("tool_required_hint") : t("loading")}</p>
            )}
            {builtinPluginPicker}
            {unifiedDraftEnabled && sel.size === 0 && !hasBoundResources && emptyToolsAllowed ? (
              <label className={canvasStyles.checkRow}>
                <input type="checkbox" checked={textOnlyConfirmed} onChange={(event) => setTextOnlyConfirmed(event.target.checked)} />
                {t("draft_text_only_confirm")}
              </label>
            ) : null}
            {approvalLockedSelected ? <Notice tone="muted">{t("retained_approval_warning")}</Notice> : null}
            {writeSelected ? <p className={canvasStyles.consequence}>{writeConsequence}</p> : null}
            {catalogErr ? <Notice tone="error">{catalogErr.message}<ErrorDiagnosticsInline error={catalogErr.error} surface="settings" /></Notice> : null}
            {toolPickerOpen ? (
              <div id={`${canvasIdBase}-picker`} className={canvasStyles.picker}>
                <Field id={searchId} label={t("search_tools")}>
                  <input id={searchId} type="search" className="settings-input" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t("search_tools")} />
                </Field>
                <div className={canvasStyles.pickerGroup} role="group" aria-label={t("group_team_tools")}>
                  <span className={canvasStyles.pickerGroupTitle}>{t("group_team_tools")}</span>
                  {teamF.length ? teamF.map((tool) => {
                    const locked = tool.selectable === false || Boolean(tool.locked_reason);
                    return (
                      <label key={tool.tool_name} className={canvasStyles.option}>
                        <input type="checkbox" checked={sel.has(tool.tool_name)} disabled={locked && !sel.has(tool.tool_name)} onChange={() => toggle(tool.tool_name)} />
                        <span className={canvasStyles.optionText}>
                          <span className={canvasStyles.optionTitle}>{teamToolLabel(tool)}<RiskBadge risk={tool.risk} /></span>
                          <span className={canvasStyles.optionDescription}>{tool.description}</span>
                        </span>
                      </label>
                    );
                  }) : <span className={canvasStyles.hint}><SearchIcon size={14} aria-hidden /> {catalog ? t("no_match") : t("loading")}</span>}
                </div>
                <div className={canvasStyles.pickerGroup} role="group" aria-label={t("group_native_tools")}>
                  <span className={canvasStyles.pickerGroupTitle}>{t("group_native_tools")}</span>
                  {nativeToolPicker ?? <span className={canvasStyles.hint}>{catalog ? t("no_match") : t("loading")}</span>}
                </div>
              </div>
            ) : null}
          </CanvasSection>

          <CanvasSection labelledBy={`${canvasIdBase}-run`}>
            <SectionHeading id={`${canvasIdBase}-run`} title={t("section_run")} />
            <div>
              <FieldRow title={t("model_label")} description={t("model_only_tier")}
                control={<span className={canvasStyles.readValue}>{t("model_analyst_option")}</span>} />
              <FieldRow title={t("visibility_label")} description={t("create_private_hint")}
                control={<span className={canvasStyles.readValue}>{t("vis_me")}</span>} />
            </div>
          </CanvasSection>

          <CanvasSection labelledBy={`${canvasIdBase}-advanced`}>
            <SectionHeading id={`${canvasIdBase}-advanced`} title={t("section_advanced")} count={t("section_advanced_count")}
              collapsible open={advancedOpen} controls={`${canvasIdBase}-advanced-body`} onToggle={() => setAdvancedOpen((value) => !value)} />
            {advancedOpen ? (
              <div id={`${canvasIdBase}-advanced-body`} className={canvasStyles.fields}>
                <Field id={slugId} label={t("slug_label")} hint={t("slug_hint")} suggestion={suggestionFor(["name"])}
                  error={(slugEdited || triedCreate) && !nameValid ? t("slug_invalid") : undefined}>
                  <input id={slugId} className="settings-input" value={slug} spellCheck={false}
                    aria-describedby={fieldDescribedBy(slugId, { hint: true, error: (slugEdited || triedCreate) && !nameValid })}
                    onChange={(event) => { setSlugEdited(true); setSlug(event.target.value.replace(/\s+/g, "-").toLowerCase()); }} />
                </Field>
                <Field id={`${canvasIdBase}-schema`} label={t("schema_label")} suggestion={suggestionFor(["output_schema_text"])}
                  hint={schemaValid ? t("schema_valid_hint") : undefined} error={schemaValid ? undefined : t("schema_invalid_hint")}>
                  <textarea id={`${canvasIdBase}-schema`} className={`settings-input ${canvasStyles.textarea}`} rows={6} spellCheck={false} value={schemaText}
                    onChange={(event) => { copilot.touchField("output_schema"); setSchemaText(event.target.value); }} />
                </Field>
              </div>
            ) : null}
          </CanvasSection>
        </fieldset>
        {guardDialog}
      </CanvasObjectSurface>
    );
  }

  // ===== SUCCESS =====
  function renderSuccess() {
    const teamVisible = savedAgent?.visibility === "team_wide";
    const chips: [string, string][] = [
      ...(!receiptNeedsRead ? ([
        ["layers", resourceSummary(receiptResources ?? (recoveredAgent
          ? agentResourceCounts({ tool_refs: recoveredAgent.tool_refs })
          : { ...localResourceCounts, skills: localResourceCounts.skills + skillSel.size }))],
        ["brain", t("chip_model")],
      ] as [string, string][]) : []),
      [teamVisible ? "globe" : "user", teamVisible ? t("chip_vis_team") : t("chip_vis_me")],
    ];
    return (
      <div style={{ padding: "30px 22px 24px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center" }}>
        <div style={{ width: 54, height: 54, borderRadius: "50%", background: "var(--c-pos-soft)",
          border: "1.5px solid var(--c-pos-line)", color: "var(--c-pos)", display: "grid", placeItems: "center", marginBottom: 16 }}>
          <Icon name="check" s={28} />
        </div>
        <h3 style={{ margin: 0, fontSize: "1.125rem", fontWeight: 600, color: "var(--c-ink)", letterSpacing: "-.01em",
          display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", justifyContent: "center" }}>
          {t.rich(isEditing ? "success_title_edit" : "success_title_create", {
            slug: () => <span style={{ color: "var(--c-accent)" }}>{savedAgent?.label || displayName.trim() || slug.trim()}</span>,
          })}
        </h3>
        <p style={{ margin: "8px 0 0", fontSize: "0.8125rem", color: "var(--c-ink-3)", lineHeight: 1.6, maxWidth: 440 }}>
          {t("success_body")}
        </p>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 16, padding: "9px 14px",
          background: "var(--c-accent-tint)", border: "1px solid var(--c-accent-line)", borderRadius: 999 }}>
          <Icon name={teamVisible ? "globe" : "user"} s={14} style={{ color: "var(--c-accent)" }} />
          <span style={{ fontSize: "0.75rem", color: "var(--c-accent)", fontWeight: 600 }}>
            {t(teamVisible ? "chip_vis_team" : "chip_vis_me")}
          </span>
        </div>
        <div style={{ display: "flex", gap: 7, flexWrap: "wrap", justifyContent: "center", marginTop: 18 }}>
          {chips.map(([ic, lbl], i) => (
            <span key={i} style={{ fontSize: "0.71875rem", color: "var(--c-ink-3)", background: "var(--c-surface-2)",
              border: "1px solid var(--c-line)", borderRadius: 999, padding: "5px 12px",
              display: "inline-flex", alignItems: "center", gap: 6 }}>
              <Icon name={ic} s={13} style={{ color: "var(--c-ink-4)" }} />{lbl}
            </span>
          ))}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 24 }}>
          {canDirectUse && !isEditing && savedAgent ? <button type="button" style={btnPrimary()}
            onClick={() => { void confirmCanvasNavigation().then((allowed) => { if (allowed && mountedRef.current) handoffAgentToNewChat({ agentId: savedAgent.id, label: savedAgent.label }); }); }}>{t("canvas_start_use")}</button> : null}
          {savedAgent?.id ? (
            <button type="button" style={{ ...btnPrimary(false), padding: "10px 26px" }}
              onClick={() => router.push(`/agents/manage/?agent_id=${encodeURIComponent(savedAgent.id)}`)}>
              <Icon name="sliders" s={16} />{t("manage_agent")}
            </button>
          ) : null}
          <button type="button" style={savedAgent?.id ? linkBtn() : { ...btnPrimary(false), padding: "10px 26px" }} onClick={onClose}>
            {t("done")}
          </button>
          {!isEditing && !unifiedDraftEnabled ? (
            <button type="button" style={linkBtn()} onClick={() => {
              setStep("describe"); setSlug(""); setDisplayName(""); setDesc(""); setPersona(""); setSel(new Set());
              setPersonaTouched(false); setTriedDescribeNext(false);
              setSkillSel(new Set()); setSkillsOpen(false);
              setIters(12); setSchemaOpen(false); setSchemaText(""); setError(null); setSavedAgent(null);
            }}>
              <Icon name="plus" s={14} />{t("create_another")}
            </button>
          ) : null}
        </div>
      </div>
    );
  }

  if (canvasMode) return renderCanvasObject();

  return (
    <div style={wrap}>
      {step !== "success" ? header : null}
      {unknownSave ? <div role="alert" style={{ margin: "10px 18px", fontSize: "0.8125rem" }}>
        <p>{t(unknownSave.creating ? "draft_unknown_create" : "draft_unknown_save")}</p>
        <button type="button" disabled={busy} className="settings-btn settings-btn--secondary" onClick={() => void checkSavedDraft()}>{t("draft_check_save")}</button>
      </div> : null}
      {unifiedDraftEnabled && !isEditing && step !== "success" && manualSaveFailed ? (
        <p role="alert" style={{ margin: "10px 18px", color: "var(--c-err)", fontSize: "0.75rem" }}>{t("draft_save_failed")}</p>
      ) : null}
      {leavePromptOpen && step !== "success" ? (
        <div role="group" aria-label={t("draft_leave_title")} style={{ margin: "8px 18px", padding: 12, border: "1px solid var(--c-warn-line)", borderRadius: 8, background: "var(--c-warn-soft)" }}>
          <strong>{t("draft_leave_title")}</strong>
          <p style={{ margin: "6px 0 12px", fontSize: "0.75rem" }}>{t("draft_leave_description")}</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <button type="button" disabled={busy || manualConflict !== null}
              style={btnPrimary(busy || manualConflict !== null)}
              onClick={() => void saveAndLeave()}>{t("draft_save_leave")}</button>
            <button type="button" style={btnGhost()} onClick={() => {
              finishLeave(false);
            }}>{t("draft_continue_editing")}</button>
            <button type="button" disabled={busy || manualSaveStatus === "saving"}
              style={btnGhost()} onClick={() => {
                if (manualSavesInFlightRef.current === 0 && !externalMutationRef.current) {
                  if (!closeAfterLeaveRef.current && manualDraftRef.current) installManualDraft(manualDraftRef.current);
                  finishLeave(true);
                }
              }}>{t("draft_discard_changes")}</button>
          </div>
        </div>
      ) : null}
      {manualConflict && latestConflictValues ? (
        <div role="group" aria-label={t("conflict_title")} style={{ margin: "8px 18px", padding: 12, border: "1px solid var(--c-warn-line)", borderRadius: 8, background: "var(--c-warn-soft)" }}>
          <strong>{t("conflict_title")}</strong>
          <p style={{ margin: "6px 0 12px", fontSize: "0.75rem" }}>{t("conflict_description")}</p>
          {conflictFields.map(({ key, label }) => (
            <fieldset key={key} style={{ border: "1px solid var(--c-line)", borderRadius: 6, marginBottom: 8, padding: 8 }}>
              <legend>{t(label as never)}</legend>
              {baselineConflictValues ? <div style={{ fontSize: "0.6875rem", color: "var(--c-ink-3)", marginBottom: 6 }}>
                {t("conflict_baseline")}
                <pre style={{ maxHeight: 100, overflow: "auto", whiteSpace: "pre-wrap", margin: "4px 0" }}>{baselineConflictValues[key]}</pre>
              </div> : null}
              {(["mine", "latest"] as const).map((choice) => (
                <label key={choice} style={{ display: "block", marginBottom: 6, fontSize: "0.75rem" }}>
                  <input type="radio" name={`${fieldId}-manual-conflict-${key}`} checked={manualConflict.choices[key] === choice}
                    onChange={() => setManualConflict((current) => current ? { ...current, choices: { ...current.choices, [key]: choice } } : null)} />
                  {t(choice === "mine" ? "conflict_mine" : "conflict_latest")}
                  <pre style={{ maxHeight: 140, overflow: "auto", whiteSpace: "pre-wrap", margin: "4px 0 0 20px" }}>
                    {choice === "mine" ? localConflictValues[key] : latestConflictValues[key]}
                  </pre>
                </label>
              ))}
            </fieldset>
          ))}
          <button type="button" disabled={conflictFields.some(({ key }) => !manualConflict.choices[key])}
            style={btnPrimary(conflictFields.some(({ key }) => !manualConflict.choices[key]))}
            onClick={resolveManualConflict}>{t("conflict_apply")}</button>
        </div>
      ) : null}
      <fieldset disabled={busy || manualDraft?.actions.edit === false} aria-busy={busy} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
      {!continuous ? <Stepper active={stepIdx} t={t} /> : null}
      {continuous && step !== "success" ? <>
        <section aria-label={t("step_describe")}>{renderDescribe()}</section>
        <section aria-label={t("step_tools")}>{renderTools()}</section>
        <section aria-label={t("step_configure")}>{renderConfigure()}</section>
      </> : <>
        {step === "describe" && renderDescribe()}
        {step === "tools" && renderTools()}
        {step === "configure" && renderConfigure()}
      </>}
      </fieldset>
      {step === "success" && renderSuccess()}
    </div>
  );
}
