/**
 * AI 优化 of an Agent's 工作说明 (docs/sprints/agent-page-redesign-2026-11-24/slices-2-5-tech-design.md §3.2): the
 * pure rules behind the button. The request is an ordinary Coach proposal (`POST …/change-proposals/coach`) tagged
 * through `source_ref`; the page reads only what it asked for and never judges the model's wording itself.
 *
 * - The goal states mechanism facts: the field to rewrite and the Agent's configured Tools, Plugins, Skills and
 *   knowledge sources by name, each list capped so the goal stays under the endpoint's 8,000 characters.
 * - The rationale is read through three labelled lines (保留 / 新增 / 未配置, decision SD1). Without them the
 *   rationale is shown as written and nothing is tagged.
 * - Applying sends only the groups whose operations (and dependencies) all target `/definition/system_prompt`.
 */

import { ApiError } from "@/lib/api-client";
import type { AgentChangeProposal } from "@/lib/agents-api";

export const SYSTEM_PROMPT_PATH = "/definition/system_prompt";
export const AI_OPTIMIZE_SOURCE_PREFIX = "ai_optimize:";
/** Names per fact list before "and N more". */
export const OPTIMIZE_LIST_CAP = 40;
/** The endpoint accepts a goal of at most 8,000 characters. */
export const OPTIMIZE_GOAL_LIMIT = 8000;
/** An original shorter than this tends to come back with rules the Owner never wrote (optimize spike S2). */
export const SHORT_ORIGINAL_CHARS = 100;
/** One name never takes more than this many characters of the goal. */
const NAME_LIMIT = 80;

/** The `source_ref` of one AI 优化 request; the page finds its own proposals by the prefix. */
export function aiOptimizeSourceRef(id: string): string {
  return `${AI_OPTIMIZE_SOURCE_PREFIX}system_prompt:${id}`;
}

export function isAiOptimizeProposal(proposal: Pick<AgentChangeProposal, "source_type" | "source_ref">): boolean {
  return proposal.source_type === "coach" && typeof proposal.source_ref === "string" && proposal.source_ref.startsWith(AI_OPTIMIZE_SOURCE_PREFIX);
}

/** The newest open AI 优化 proposal: the result that shows under 工作说明, also after a reload. */
export function newestOpenAiOptimizeProposal(proposals: readonly AgentChangeProposal[] | undefined): AgentChangeProposal | null {
  let newest: AgentChangeProposal | null = null;
  for (const proposal of proposals ?? []) {
    if (proposal.status !== "open" || !isAiOptimizeProposal(proposal)) continue;
    if (!newest || proposal.created_at > newest.created_at) newest = proposal;
  }
  return newest;
}

export interface FactListWords {
  /** The whole list when it is empty ("无" / "none"). */
  none: string;
  /** The tail after the cap ("以及另外 3 项"). */
  more: (count: number) => string;
  separator: string;
}

/** At most `cap` names, deduplicated in order, then "and N more". */
export function factList(names: readonly string[], words: FactListWords, cap = OPTIMIZE_LIST_CAP): string {
  const unique = [...new Set(names.map((name) => name.trim()).filter(Boolean))];
  if (!unique.length) return words.none;
  const shown = unique.slice(0, cap).map((name) => (name.length > NAME_LIMIT ? `${name.slice(0, NAME_LIMIT - 1)}…` : name));
  const rest = unique.length - shown.length;
  return rest > 0 ? `${shown.join(words.separator)}${words.separator}${words.more(rest)}` : shown.join(words.separator);
}

export interface OptimizeFacts {
  tools: readonly string[];
  plugins: readonly string[];
  skills: readonly string[];
  /** Already in words: the knowledge mode, with file names when the Agent reads selected files. */
  knowledge: readonly string[];
}

export interface OptimizeGoalValues {
  tools: string;
  plugins: string;
  skills: string;
  knowledge: string;
}

/**
 * The request text: `render` is the localized message (`agent_optimize.goal`) with these facts filled in. Each list
 * holds at most 40 names; if the goal would still exceed the endpoint's limit, the lists shrink until it fits.
 */
export function buildOptimizeGoal(render: (values: OptimizeGoalValues) => string, facts: OptimizeFacts, words: FactListWords): string {
  let goal = "";
  for (const cap of [OPTIMIZE_LIST_CAP, 20, 10, 5, 0]) {
    goal = render({
      tools: factList(facts.tools, words, cap),
      plugins: factList(facts.plugins, words, cap),
      skills: factList(facts.skills, words, cap),
      knowledge: factList(facts.knowledge, words, cap),
    });
    if (goal.length <= OPTIMIZE_GOAL_LIMIT) return goal;
  }
  return goal.slice(0, OPTIMIZE_GOAL_LIMIT);
}

export interface RationaleLabels {
  kept: string;
  added: string;
  unconfigured: string;
  /** The word that stands for an empty list. */
  none: string;
}

export interface ParsedRationale {
  kept: string;
  added: string[];
  unconfigured: string[];
}

const COLONS = /[:：]\s*$/;
const QUOTES: Array<[string, string]> = [["「", "」"], ["“", "”"], ["\"", "\""], ["『", "』"], ["'", "'"], ["‘", "’"]];

function labelBase(label: string): string {
  return label.trim().replace(COLONS, "").trim();
}

/** The text after `label` when the line starts with it (either colon), else null. */
function afterLabel(line: string, label: string): string | null {
  const base = labelBase(label);
  if (!base) return null;
  const text = line.replace(/^[\s>*•·-]+/, "");
  if (!text.startsWith(base)) return null;
  const rest = text.slice(base.length);
  const match = /^\s*[:：]\s*/.exec(rest);
  return match ? rest.slice(match[0].length).trim() : null;
}

function unquote(item: string): string {
  // A sentence-ending stop after the closing quote belongs to the rationale, not to the quoted sentence.
  const text = item.trim().replace(/(["”」』'’])[。.]$/, "$1");
  for (const [open, close] of QUOTES) {
    if (text.length > open.length + close.length && text.startsWith(open) && text.endsWith(close)) return text.slice(open.length, -close.length).trim();
  }
  return text;
}

function items(value: string, none: string): string[] {
  const noneWord = labelBase(none).replace(/[。.]$/, "");
  return value.split("|").map(unquote).map((item) => item.replace(/[。.]$/, "").trim() === noneWord ? "" : item).filter((item) => item.length > 0);
}

/**
 * The three labelled lines the request asked for. Null when any label is missing: the page then shows the rationale
 * as written, with no tags (decision SD1).
 */
export function parseOptimizeRationale(rationale: string, labels: RationaleLabels): ParsedRationale | null {
  let kept: string | null = null;
  let added: string[] | null = null;
  let unconfigured: string[] | null = null;
  for (const line of rationale.split(/\r?\n/)) {
    const keptText = afterLabel(line, labels.kept);
    if (keptText !== null && kept === null) { kept = keptText; continue; }
    const addedText = afterLabel(line, labels.added);
    if (addedText !== null && added === null) { added = items(addedText, labels.none); continue; }
    const unconfiguredText = afterLabel(line, labels.unconfigured);
    if (unconfiguredText !== null && unconfigured === null) unconfigured = items(unconfiguredText, labels.none);
  }
  if (kept === null || added === null || unconfigured === null) return null;
  return { kept, added, unconfigured };
}

export interface SuggestionLine {
  text: string;
  /** The line contains one of the model's own quoted added sentences, verbatim. */
  added: boolean;
}

/** The suggestion line by line; a line is tagged 新增 only when it holds a quoted added sentence exactly. */
export function taggedSuggestionLines(text: string, added: readonly string[]): SuggestionLine[] {
  const quoted = added.map((item) => item.trim()).filter((item) => item.length >= 2);
  return text.split(/\r?\n/).map((line) => ({ text: line, added: quoted.some((item) => line.includes(item)) }));
}

/** The page's own caution: a short original may come back with rules the Owner never wrote. */
export function isShortOriginal(text: string): boolean {
  return [...text.trim()].length < SHORT_ORIGINAL_CHARS;
}

/** The rewritten 工作说明 a proposal carries; null when it does not rewrite that field as text. */
export function proposedSystemPrompt(proposal: Pick<AgentChangeProposal, "operations">): string | null {
  let value: string | null = null;
  for (const operation of proposal.operations) {
    if (operation.path !== SYSTEM_PROMPT_PATH) continue;
    value = (operation.op === "replace" || operation.op === "add") && typeof operation.value === "string" ? operation.value : null;
  }
  return value;
}

/**
 * The groups to apply: each one whose operations, and those of every group it depends on, all target 工作说明.
 * The endpoint accepts every authoring path (S2), so a group touching anything else is never sent.
 */
export function systemPromptGroupIds(proposal: Pick<AgentChangeProposal, "operations" | "atomic_groups">): string[] {
  const groups = new Map(proposal.atomic_groups.map((group) => [group.id, group]));
  const onlyPrompt = (id: string, seen: Set<string>): boolean => {
    if (seen.has(id)) return true;
    seen.add(id);
    const group = groups.get(id);
    if (!group || !group.operation_indexes.length) return false;
    if (!group.operation_indexes.every((index) => proposal.operations[index]?.path === SYSTEM_PROMPT_PATH)) return false;
    return group.depends_on.every((dependency) => onlyPrompt(dependency, seen));
  };
  const selected = new Set<string>();
  for (const group of proposal.atomic_groups) {
    if (!onlyPrompt(group.id, new Set())) continue;
    const add = (id: string) => {
      if (selected.has(id)) return;
      selected.add(id);
      groups.get(id)?.depends_on.forEach(add);
    };
    add(group.id);
  }
  return proposal.atomic_groups.map((group) => group.id).filter((id) => selected.has(id));
}

export type OptimizeFailureKind =
  | "model_unconfigured"
  | "unknown"
  | "running"
  | "no_change"
  | "bad_format"
  | "guarded"
  | "owner_required"
  | "version_conflict"
  | "merge_conflict"
  | "stale"
  /** A proxy answered 502/504 (or the connection dropped): the server may still finish, so the page keeps asking. */
  | "detached"
  | "other";

const FAILURE_KINDS: Record<string, OptimizeFailureKind> = {
  team_model_unconfigured: "model_unconfigured",
  agent_coach_generation_unknown: "unknown",
  agent_coach_generation_failed: "unknown",
  agent_coach_generation_running: "running",
  no_change: "no_change",
  agent_coach_no_change: "no_change",
  parse_failed: "bad_format",
  bad_output: "bad_format",
  marker_echoed: "bad_format",
  url_guard: "guarded",
  secret_guard: "guarded",
  agent_change_proposal_path_forbidden: "guarded",
  agent_owner_required: "owner_required",
  agent_not_active: "owner_required",
  agent_working_copy_version_conflict: "version_conflict",
  agent_change_merge_conflict: "merge_conflict",
  agent_change_proposal_stale: "stale",
};

/** Which row of the error table (§3.2) a failed request or apply belongs to. */
export function optimizeFailureKind(cause: unknown): OptimizeFailureKind {
  if (cause instanceof ApiError) {
    const named = cause.code ? FAILURE_KINDS[cause.code] : undefined;
    if (named) return named;
    if ((cause.status === 502 || cause.status === 504) && (!cause.code || cause.nonApiResponse)) return "detached";
    return "other";
  }
  // fetch rejects with a TypeError when the connection drops: the request may have reached the server.
  if (cause instanceof TypeError) return "detached";
  return "other";
}
