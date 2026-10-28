/**
 * running-band-model — **pure** data selection for the 1e running band (no React, timers, or copy).
 *
 * Design authority: Claude Design `chat-ux-2026-08/运行带-实现规格.html` (the selected 1e layout).
 * Implementation criteria: `docs/sprints/chat-interaction-ux-2026-08-07/运行态披露面-不变量清点.md`.
 *
 * Why a separate pure module: most band rules select **data** (which two historical steps, internal-string filtering,
 * step number, recorded issues, ownership). Mixing them with animations/timers forces rendering tests,
 * requiring assertion rewrites for every animation change.
 *
 * ⚠ **This module produces no user-visible wrapper copy**: only structure and already localized backend data;
 * `正在… / 第 N 步 / 已重试 / 接手自 X` are all rendered by component i18n.
 *
 * ⚠ Reuse vocabularies rather than creating another (design §4 explicitly prohibits a second vocabulary):
 *   - Human-readable step names → `tool-catalog.ts`
 *   - Agent identity → `agent-meta.ts` (`peerNames[subagentId]` is authoritative)
 *   - Whether a failure can self-heal → `self-healing.ts`
 */
import type { PeerTerminal } from "@/lib/turn-store";

import { deriveTerminalStatus, formatPeerDuration } from "./agent-grouping";
import type { StepStatus } from "./block-primitives";
import { LEAD_BRAND_NAME, agentDisplayName, agentKeyFor, type AgentKey } from "./agent-meta";
import { countStepIssues, isRecoverableStepFailure } from "./self-healing";
import { getCurrentLocale } from "@/lib/locale";
import { TOOL_CATALOG_KEYS, getToolMeta, writingDisplayKey } from "./tool-catalog";
import type { StepView } from "./types";

/** Total band rows (2 historical + 1 current), the three in always three rows. */
export const BAND_ROWS = 3;
/** History row count = BAND_ROWS - 1 (current occupies its own row). */
export const HISTORY_ROWS = BAND_ROWS - 1;

/**
 * Main-surface forbidden tokens (PRD A5). A match **discards the whole string**, without partial redaction; a truncated
 * `artifact_id=art_5…` is worse than nothing, unreadable without explaining what was cut.
 *
 * ★ These strings were **observed directly** in running-state output on 116 v1.0.89:
 *   `读取 pinned dataset "2025年客流量_94753e7.csv" (artifact_id=art_5…`
 *   `运行: python_sandbox`
 * They come from backend `header_zh` and `input_preview` (the latter passes the first 60 characters unchanged through
 * `prettifyStepLabel`'s string branch, so not being raw JSON does not make it human-readable).
 *
 * ⚠ Deliberately **exclude** `schema`: `查 schema · 27 测度 + 8 维度` is a valid Chinese heading;
 * PRD prohibits raw English schema errors, rather than the word itself. Including it would cause false positives.
 */
const INTERNAL_TOKENS: readonly RegExp[] = [
  /artifact_id/i,
  /\bart_[0-9a-f]{4,}/i,
  /pinned\s+dataset/i,
  /python_sandbox/i,
  /\bpayload\b/i,
  /exit_code/i,
  /soft_refuse/i,
  /subagent_return/i,
  // ★ Backend `_TOOL_HEADER_ZH` labels `subagent_return` as `子 agent 交付`,
  //   jargon of the same family as `派子 agent` (PRD A5 requires human-readable main-surface copy).
  //   📏 On 116 v1.1.10, `正在子 agent 交付` remained from ms 119401 until the band unmounted.
  //   ⚠ **Do not copy the spawn-row skip policy**: active `subagent_return`
  //     means the peer really is delivering; skipping real activity turns an actual wait into an empty gap.
  //     Only the wording is wrong. Filtering that heading lets `humanLabel` naturally fall back to
  //     the **existing** `tool-catalog` copy `整理结果 / Collect results` (via i18n).
  //   ★ Another specified-but-unimplemented case: human-readable catalog copy existed but backend headings shadowed it.
  /子\s*agent\s*交付/,
  /\bprofile\s*[:=]/i,
];

/**
 * Internal-string filter: forbidden tokens or raw-JSON-looking values always return an empty string.
 * Rendering interprets empty as hiding this cell, just like absent backend data.
 *
 * ⚠ **No discard-if-identifier-shaped heuristic here**, deliberately.
 * The prior `/^[a-z][a-z0-9]*([_.][a-z0-9]+)+$/i` also discarded
 * **valid filenames / table names** like `sales_2025.csv` and `fact_orders`,
 * precisely what users most want to see on this row.
 * Prevent `正在web_search` through another path: main copy **prefers human-readable tool-catalog labels**,
 * leaving unrecognized tools empty for rendering's `处理中` fallback (PRD P0-2's exact copy);
 * see `humanLabel`. The blacklist filters internal strings, not shapes.
 */
export function stripInternal(raw: string | null | undefined): string {
  const s = stripToolSlugSuffix((raw ?? "").trim());
  if (!s) return "";
  if (s.startsWith("{") || s.startsWith("[")) return "";
  for (const re of INTERNAL_TOKENS) {
    if (re.test(s)) return "";
  }
  return s;
}

/**
 * Remove trailing (tool name) from headings.
 *
 * ★ Final review caught this batch's costliest issue: backend `_tool_header_zh`
 * (`bowhead_core/subagent.py:5154`'s `_TOOL_HEADER_ZH`) **embeds slugs in headings**:
 *   `"bi_query"  → "查询数据 (bi_query)"`
 *   `"bi_schema" → "查 schema (bi_schema)"`
 *   `"kb_search" → "搜知识库 (kb_search)"`
 *   `"python_sandbox" → "跑代码 (Python)"`
 * These are the product's most common steps. None match the forbidden-token blacklist, so the current row renders
 * `正在查询数据 (bi_query)`, exactly the jargon PRD A5 removes.
 *
 * ⚠ **Every** authored fixture used clean `"查询数据"`, as did the spike,
 * hiding this from all-green local tests and visual self-review (`feedback_my_own_fixtures_are_the_happy_path`).
 *
 * Remove only parenthesized **pure ASCII identifiers**; retain Chinese annotations (`查询数据（2025年）`) unchanged.
 */
function stripToolSlugSuffix(s: string): string {
  return s.replace(/\s*[（(]\s*[A-Za-z][A-Za-z0-9_.+-]*\s*[)）]\s*$/, "").trim();
}

/**
 * This step's main-surface name. **Return empty if unrecognized**, never falling back to raw strings.
 *
 * Order: filtered backend Chinese heading → human-readable tool-catalog label → empty.
 *   · `header_zh` is most specific (e.g. `查 schema · 27 测度 + 8 维度`), so it takes priority;
 *     but it caused 116's internal-string leakage and must pass `stripInternal` first.
 *   · Catalog lookup accepts only **static entries**: `getToolMeta` synthesizes
 *     `{ name: op }` for `teammcp.<source>.<op>`; op is a raw slug (`list_issues`), jargon on the main surface.
 *     Team tools' human-readable copy should come from backend `header_zh` (see tool-catalog comments).
 */
export function humanLabel(s: StepView, active: boolean): string {
  // `准备中` write-code / write-query rows use writing entries for names (see `tool-catalog.ts::writingDisplayKey`),
  // otherwise the English UI says "Run code" before execution begins.
  const tool = writingDisplayKey(s.stepType, s.toolName) ?? s.toolName ?? "";
  const isStaticTool = Boolean(tool) && TOOL_CATALOG_KEYS.has(tool);
  const catalogLabel = (): string => {
    const meta = isStaticTool ? getToolMeta(tool) : null;
    if (!meta) return "";
    return (active ? meta.activeName ?? meta.name : meta.name) || "";
  };
  // ★ **Catalog takes priority outside Chinese UI** (2026-08-17, prompted by user screenshots):
  //   `header_zh` is inherently **Chinese** (backend `_TOOL_HEADER_ZH`), and formerly always overrode the catalog,
  //   making the whole band Chinese in English UI: on 116 en-US, rows read `加图表` and `跑代码`,
  //   while English catalog names for `add_chart` / `python_sandbox` had always existed.
  //   User decision: **band text follows UI language**.
  //   ⚠ Apply only to **static catalog entries**: `getToolMeta` synthesizes
  //     `{ name: op }` for `teammcp.<source>.<op>` (raw slug, main-surface jargon); backend headings provide Team-tool copy.
  //   ⚠ Explicitly accepted cost: English loses `header_zh` specificity
  //     (`查 schema · 27 测度 + 8 维度`→`Read schema`). Prefer fewer details over mixed languages.
  if (!getCurrentLocale().toLowerCase().startsWith("zh")) {
    const fromCatalog = catalogLabel();
    if (fromCatalog) return fromCatalog;
  }
  const header = stripInternal(s.headerZh);
  if (header && header !== tool) return header;
  return catalogLabel();
}

/**
 * Only two states.
 *
 * ⚠ **Deliberately no `failed`**: the band mounts only while the turn is running; turn failure and
 * `running=false` occur in the **same frame** (`use-turn-runner.ts`'s `error` / `turn_failed`
 * branches both write `ns.running = false` in the same reducer update setting `errorReason`).
 * These conditions are mutually exclusive, making that state unreachable dead code in production; design §2-E is thus omitted.
 * Already-LIVE failure coloring + `FinalFailBanner` handle terminal failures. See inventory §2.1's correction.
 */
export type BandPhase = "preparing" | "running";

/**
 * `rows` = persistent three rows (default); `single` = one row.
 *
 * User decision (inventory §4 ②): collapse to one row when **answer streaming has started and this turn has no steps**.
 * Use explicit signals rather than magic first-N-seconds thresholds, which misclassify slow turns
 * (a turn whose real step arrives at second 12 would collapse then grow again).
 * ⚠ Collapse the **skeleton**, retaining the band itself (existing tests ensure it remains during answer streaming).
 */
export type BandDensity = "rows" | "single";

export interface BandRow {
  /** stepId / `peer:<peerId>`: animation keys must stay stable or every reorder replays entry animation. */
  key: string;
  /**
   * ★ Distinguish skeletons from real steps. Checking only an empty `label` renders a completed step
   * whose forbidden tokens were filtered out as a loading skeleton, making completed work appear active.
   *
   * `peer` = a peer's **terminal state**. Inclusion is **deliberately asymmetric** for fail / done
   * (see rules in `buildBandModel`):
   *   · `fail`: **always include**. Failure is new information, and backend hardcodes its spawn row
   *     as `is_transient`, falsely rendering `已重试`; remove that row and replace it with this one.
   *   · `done`: include only peers with **zero child steps** (direct LLM results / plan-path peers).
   *     Peers with child steps already show success through their own green steps; an extra row adds noise
   *     and renders **before** their actual work.
   * ⚠ The former rule omitted all peer rows when child steps existed; it was overturned
   *   after repeated independent reviews showed hard-failed peers had no red band row under that rule.
   */
  kind: "step" | "skeleton" | "peer";
  /** Human-readable heading; empty if unrecognized (rendering supplies fallback copy). Always empty on `peer` rows:
   *  rendering combines `peerName` + `peerOutcome` through i18n (this module produces no wrapper copy). */
  label: string;
  /** Filtered right-side metadata (summary or duration). Empty for warning steps; rendering shows their original outcome. */
  meta: string;
  /**
   * `done` green✓ / `warn` amber warning (recoverable failure, partial outcome or cancellation) /
   * `fail` red (hard failure; Lead bypassed it but the record remains) / `active` still running.
   */
  tone: "done" | "warn" | "fail" | "active";
  /** Original step outcome, separate from warning severity and retry eligibility. */
  stepState?: StepView["state"];
  /** Ownership display name for `kind:"peer"`. */
  peerName?: string;
  /**
   * Terminal state for `kind:"peer"`, from **existing** `deriveTerminalStatus` (see `bandTerminalStatus`).
   * Only `fail` / `done` on the band; `bail` is unreachable during its lifecycle.
   *
   * Separate from visual `tone`: peer and step outcomes retain their own
   * facts without predicting recovery from a warning classification.
   */
  peerOutcome?: "fail" | "done";
}

export interface BandCurrent {
  agent: AgentKey;
  /** Ownership display name (`agentDisplayName`, honoring Team-configured custom names). */
  agentName: string;
  /**
   * Original backend `agent_name`, **only for `AgentBadge`**. Its contract takes agent_name,
   * not AgentKey: passing a key makes every custom agent generic, collapsing them to one badge.
   */
  agentBadgeName: string;
  /**
   * Current-row owner ID (peer ID or `"lead"`). **Rendering uses it as the throttle reset key**:
   * prose must switch immediately on ownership change, preventing a new agent's name with the previous one's words,
   * the misattribution `proseTailOf`'s `allowFallback:false` prevents;
   * model filtering blocked it but rendering's throttle reintroduced it (caught by independent review).
   */
  owner: string;
  /** Human-readable current action. Rendering uses a generic fallback when empty. */
  what: string;
  /** Filtered supplementary information (label / duration), joined into the footnote. */
  detail: string[];
  /** Previous owner's display name on agent change; null for consecutive work by the same agent. */
  handoffFrom: string | null;
  /**
   * Display name while waiting for this peer to return (its spawn row remains active, with no terminal state).
   * Rendering uses `正在等 X 返回`, more truthful than `正在想下一步` (Lead is waiting, not thinking),
   * and more readable than the backend heading `正在派子 agent` (116 dogfood P1).
   */
  waitingOnPeer: string | null;
  /**
   * Waiting for a peer whose **name has not arrived yet** (no entry in `peerNames`).
   *
   * ★ Observed on 116 dogfood: a frame rendered **`正在等 Bowhead 返回`**, Lead waiting for itself.
   *   Root cause: `displayNameOf` falls back to `AGENT_META.lead.zh` when the name is absent; correct
   *   for `agentName` (Lead owns the spawn step), but **false** for the awaited peer.
   *   Rendering instead uses unnamed copy, never substituting Lead's name.
   */
  waitingOnPeerUnknown: boolean;
  /**
   * Current owning peer's **prose tail** (windowed + filtered), empty if absent.
   *
   * ★ With the running ledger disabled, peer prose has no other surface. User decision: keep prose visible
   * to show some content; it is the band's only character-by-character changing content, and in long-turn gaps
   * it alone indicates whether execution is stuck.
   */
  prose: string;
  /**
   * Current step's tool **category** (`tool-catalog`'s `cat`), for coarse phase copy in the disclosure strip.
   *
   * ★ Two levels are necessary: design strip `正在分析 · 第 7 步 · 3 分 25 秒`, current row
   * `正在按门店汇总客流量并排名`. The first version rendered `what` in both, duplicating one sentence
   * (obvious in spike screenshots); unit tests miss repetition because both surfaces have content.
   * Unrecognized tools yield null, with generic phase copy from rendering.
   */
  toolCat: string | null;
}

export interface BandModel {
  phase: BandPhase;
  density: BandDensity;
  /** Step ordinal (= number of steps seen); 0 if none.
   *  ⚠ Count only **tool steps**, excluding peer terminal rows; `第 N 步` refers to tool-step granularity. */
  stepOrdinal: number;
  issueCount: number;
  /**
   * Number of terminal-failed peers this turn (including those **with** child steps).
   *
   * ★ Only 2 history rows: a peer failing at t=20s scrolls out seconds later, leaving no main-surface trace
   * for the remaining 2.5 minutes of a 3-minute turn, the forgetting-a-failure-because-it-scrolls-out behavior
   * explicitly prohibited in inventory §2.1. **History is transient; this count persists**.
   *
   * ⚠ Count **all** terminal-failed peers. The band does not distinguish bail (`bandTerminalStatus`:
   * unreachable during the band's lifecycle), so there is no bail exclusion here either.
   * An earlier comment claimed otherwise after bail removal; independent review caught that stale false claim.
   */
  peerFailCount: number;
  /** Always HISTORY_ROWS entries for `density === "rows"` (pad with skeletons); empty for `single`. */
  history: BandRow[];
  current: BandCurrent | null;
}

function skeletonRow(i: number): BandRow {
  return { key: `__skeleton_${i}`, kind: "skeleton", label: "", meta: "", tone: "done" };
}

function metaOf(s: StepView): string {
  // Prefer summary (`7 查询 · 7 成功 · 7 行` is far more informative than `4.3s`),
  // falling back to duration only when absent. Filter both; summaries can contain internal strings too.
  return stripInternal(s.summary) || stripInternal(s.durationText);
}

/**
 * ⚠ tone must read `state`, rather than only whether a failure self-heals:
 * this module deliberately leaves permanent failures during execution in running state (Lead bypasses and continues);
 * that step then enters history. Checking only self-healing gives it green ✓, falsely reporting a hard failure as success.
 * Likewise, still-running concurrent steps must not receive ✓.
 */
function toneOf(s: StepView): BandRow["tone"] {
  if (s.state === "failed" || s.state === "partial") {
    return isRecoverableStepFailure(s) ? "warn" : "fail";
  }
  // ⚠ Explicitly include `cancelled`: the next line treats anything not done as active;
  // omitting a new terminal state keeps it spinning as if running, precisely the regression this fixes.
  if (s.state === "cancelled") return "warn";
  return s.state === "done" ? "done" : "active";
}

function rowOf(s: StepView): BandRow {
  const tone = toneOf(s);
  return {
    key: s.stepId,
    kind: "step",
    label: humanLabel(s, tone === "active"),
    meta: tone === "warn" ? "" : metaOf(s),
    tone,
    stepState: s.state,
  };
}

export interface PeerIdentity {
  /** turn-store `peerNames`: peerId → backend agent_name. **Authoritative source**. */
  peerNames?: Record<string, string>;
  /** turn-store `peerDisplayNames`: peerId → Team-configured display name. */
  peerDisplayNames?: Record<string, string>;
}

/**
 * Who owns this step.
 *
 * ⚠ **Do not rely only on `StepView.subagentName`**; live SSE dispatch never fills it.
 * The authoritative path is `peerNames[subagentId]`, **the same** source as
 * `agent-grouping.ts` and other trace surfaces. Other fields route all production peer steps to Lead
 * (wrong badge color, Bowhead ownership footnote, no `接手自 X`).
 *
 * ⚠ **Do not** call `agentKeyFor` with an empty name either: it returns `generic` (`专家`),
 * making Lead's steps appear as an expert. Explicitly classify all non-peer steps as lead.
 */
/** Current step's tool category; null outside the static catalog (including teammcp.*). */
function toolCatOf(s: StepView): string | null {
  const tool = s.toolName ?? "";
  if (!tool || !TOOL_CATALOG_KEYS.has(tool)) return null;
  return getToolMeta(tool)?.cat ?? null;
}

/**
 * Which peer owns this row.
 *
 * ★★ **Do not use `subagentId` directly**: on Lead's `subagent_spawn` row it is
 * **Lead's own ID**; peer ID lives in `peerProseKey` (`turn-store.ts:77-82` calls this
 * inversion the 3-strikes bug, having failed three times historically).
 *
 * On the non-plan path, spawn stays active for the peer's **entire lifecycle**. While it produces
 * LLM tokens (`partial_answer` prose arriving), no child tool step is active,
 * so spawn becomes the current step. Reading prose via `subagentId` = `peerProse[leadId]`,
 * always undefined ⇒ **prose stays empty precisely when it is most needed**.
 * ⚠ Every fixture here supplies `subagentId` directly without spawn rows, so all pass
 * (`feedback_my_own_fixtures_are_the_happy_path`).
 *
 * Guard uses **the same source** as `agent-grouping.ts:165-172`: classify spawn under its peer only
 * after the authoritative profile arrives (`peerNames` contains that ID); otherwise leave it in Lead's stream,
 * preventing internal `sub_*` IDs from appearing as agent names.
 */
function rowPeerIdOf(s: StepView, peerNames: Record<string, string>): string | null {
  const spawnFor = s.peerProseKey ?? "";
  if (spawnFor && peerNames[spawnFor]) return spawnFor;
  return s.subagentId ?? null;
}

function agentNameOf(s: StepView, peerNames: Record<string, string>): string | null {
  const peerId = rowPeerIdOf(s, peerNames) ?? "";
  return (peerId ? peerNames[peerId] : undefined) ?? s.subagentName ?? null;
}

/**
 * Handoff identity = **peer ID**, not `AgentKey`.
 *
 * ⚠ Comparing `AgentKey` misses handoffs between two peers using the same profile (`compute` × 2 is common on 116):
 * both sides are `"compute"`, so no announcement occurs. Peer ID actually determines ownership change.
 * All non-peer steps use `"lead"` (Lead's own steps have no subagentId).
 */
function ownerIdOf(s: StepView, peerNames: Record<string, string>): string {
  return rowPeerIdOf(s, peerNames) ?? "lead";
}

function agentKeyOf(s: StepView, peerNames: Record<string, string>): AgentKey {
  const name = agentNameOf(s, peerNames);
  return name ? agentKeyFor(name) : "lead";
}

function displayNameOf(
  s: StepView,
  peerNames: Record<string, string>,
  peerDisplayNames: Record<string, string>,
): string {
  const name = agentNameOf(s, peerNames);
  if (!name) return LEAD_BRAND_NAME;
  const peerId = rowPeerIdOf(s, peerNames) ?? "";
  return agentDisplayName(name, peerId ? peerDisplayNames[peerId] : null);
}

export interface BuildBandInput extends PeerIdentity {
  steps: StepView[];
  /** Answer streaming has started (`runner.answer` nonempty). See `BandDensity`. */
  answerStreaming?: boolean;
  /** turn-store `peerTerminalById`. **Without it, a peer failing before any child steps
   *  is invisible on the band**, losing its presence for the entire turn after the running ledger is disabled. */
  peerTerminalById?: Record<string, PeerTerminal>;
  /** turn-store `peerProse` (peerId → accumulated prose); its only surface after the running ledger is disabled. */
  peerProse?: Record<string, string>;
  /**
   * Peers with **real child steps** (exclude Lead's spawn rows, which are Lead tool calls).
   * Derive from `steps` by default, but callers should supply the **unthrottled** set.
   *
   * ⚠ `steps` is a **prefix** after minimum-dwell throttling, while `step_done`
   * **updates state in place**, leaving later steps queued. Deriving from the prefix first renders
   * `主分析 未完成`, then 450ms later, released steps change it back to `派子 agent · 已重试`,
   * retracting an already disclosed terminal fact.
   */
  peerIdsWithSteps?: ReadonlySet<string>;
  /**
   * Peers with Lead spawn rows; also supply the **unthrottled** set.
   *
   * ⚠ Must share its source with `peerIdsWithSteps` (mixing throttled/unthrottled data causes real bugs):
   * if a zero-child-step peer's `done` arrives before spawn is released, a frame renders
   * an independent key=`peer:<id>` row located by `terminal.evtSeq`; after 450ms spawn is released,
   * the same fact becomes a rewritten key=`<stepId>` row at an earlier position. Changed React keys replay
   * entry animation and shift the row, the jitter `peerRowFrom`'s key comment prohibits.
   */
  peerIdsWithSpawnRow?: ReadonlySet<string>;
}

/** Timeline item: a step or a peer terminal row. */
type TimelineItem =
  | { kind: "step"; step: StepView }
  | { kind: "peer"; row: BandRow };

/**
 * Merge peer terminal rows into the step stream in event order.
 *
 * ★ Ordering uses `evtSeq` (bus's per-turn monotonic event sequence, stamped by `events.py`).
 * Merge rather than sort: walk steps once, emitting peer terminal rows with smaller evtSeq first.
 *
 * ⚠ **Do not always append**: a peer failing at step 3 would remain in the last historical slot,
 * crowding out dozens of subsequent steps. History should show what just happened; two such peers
 * occupy both rows and freeze them for the entire turn.
 *
 * Fallback: peer terminals without `evtSeq` go last (bypass-bus events lack sequence);
 * steps without `evtSeq` are not boundaries, so peer terminals follow them.
 *
 * ⚠ **Accepted known cost, caught by independent review**: merge occurs **after** minimum-dwell throttling,
 * so peer rows ignore `MIN_DWELL_MS`. With three zero-child-step peers reaching terminal state in one frame,
 * the 2-row window cannot fit all; one row **never appears for even a frame**. Accepted because facts remain
 * (persistent `peerFailCount` chip counts them; expansion shows full terminals); only that row's visibility is lost.
 * Fixing this requires throttling the merged timeline, changing the step rhythm already accepted on 116.
 */
function mergeTimeline(
  steps: StepView[],
  peers: { evtSeq?: number; row: BandRow }[],
): TimelineItem[] {
  // undefined goes last: Number.POSITIVE_INFINITY naturally sorts it to the bottom.
  const pending = [...peers].sort(
    (a, b) => (a.evtSeq ?? Number.POSITIVE_INFINITY) - (b.evtSeq ?? Number.POSITIVE_INFINITY),
  );
  const out: TimelineItem[] = [];
  let pi = 0;
  for (const step of steps) {
    const seq = step.evtSeq;
    if (seq !== undefined) {
      while (pi < pending.length && (pending[pi].evtSeq ?? Number.POSITIVE_INFINITY) < seq) {
        out.push({ kind: "peer", row: pending[pi].row });
        pi += 1;
      }
    }
    out.push({ kind: "step", step });
  }
  for (; pi < pending.length; pi += 1) out.push({ kind: "peer", row: pending[pi].row });
  return out;
}

/**
 * Prose tail-window length.
 *
 * ⚠ Deliberately **short**: `.bh-band__foot` uses single-line ellipsis, with CSS cutting the **end**.
 * A long string always shows its beginning, hiding motion even as prose grows.
 * A short tail window advances with prose, conveying activity.
 * ★ Thus it does not promise the latest exact characters (CSS may still cut the end); it conveys liveness, not content.
 */
const PROSE_TAIL_CHARS = 48;

/**
 * Additional lookbehind length for filtering.
 *
 * ★ Independent review found a real leak: cutting `PROSE_TAIL_CHARS` before filtering misses
 * **forbidden tokens crossing the window boundary**. Observed input:
 *   `我先读取 pinned dataset "2025年客流量_94753e7.csv" 里的字段，再按品牌汇总`
 * becomes `inned dataset "…csv" 里的字段…`; `/pinned\s+dataset/` misses `inned`,
 * letting strings expressly prohibited by PRD A5 **reach the main surface unchanged**.
 *
 * Widen filtering beyond display by the longest forbidden token, fully enclosing any token overlapping
 * the visible window; detect in the wide window, display the narrow one. Margin 32; longest fixed token
 * is `subagent_return` (15).
 */
const PROSE_TAIL_GUARD = 32;

/**
 * Whose prose to show now.
 *
 * **Prose belongs only to the current row's owner**: show only that `peerId`'s prose.
 * Fall back to the last nonempty entry only with **no owner at all** (no steps this turn).
 *
 * ⚠ This fallback **must** be restricted. Independent review found a HIGH issue: unconditional fallback activates
 * during Lead's own steps (`subagentId` is never a `peerProse` key), yielding
 * Bowhead with a peer's words from minutes ago, misattributing old prose
 * as a live unstuck signal **throughout the turn**.
 *
 * Do not remove fallback itself: prose-only peer turns with **no steps** (existing
 * `TurnContainer.running-band.test.tsx` case `t-band-peer-prose`) have no current step;
 * without fallback their prose disappears entirely after ledger shutdown.
 *
 * ⚠ Accepted, documented cost: when Lead has steps and a peer produces only prose,
 * that prose does not appear on the band. Correct attribution outweighs an extra entry; expansion shows all prose.
 */
/**
 * Remove the truncated word fragment at the tail window's start, beginning at the nearest punctuation/pause.
 *
 * ★ **A broken head is known, not guessed**: `windowed` is a suffix of `raw`; its preceding character
 *   is absent (nothing cut) or identifies the cut. Punctuation/pause/whitespace ⇒ starts at a sentence
 *   boundary, so **retain** it; otherwise the cut is midword, so **remove** the fragment.
 *   ⚠ The prior guess checked whether punctuation appeared in the first 16 characters; on 116 Chinese prose
 *   almost never trimmed because the usual pause `，` was excluded, and the nearest `。` was often beyond 16 characters.
 *   In 55 observed frames, 6 showed fragments (`营业额`→`业额或客流量），`,
 *   `unknown_words`→`nknown_words\` (unrecognizable), `），`, matching the pre-fix behavior.
 * ⚠ If trimming yields empty, restore the original: prefer a fragment to swallowing all prose;
 *   this cell's sole responsibility is conveying liveness.
 */
/**
 * Pause character set, **shared by both predicates** (first pause inside window / character preceding window).
 * ⚠ Do not duplicate these literals; this sprint repeatedly failed on one predicate with two implementations.
 * **Chinese pauses mainly use `，、：`**; excluding them effectively removes boundaries in Chinese prose.
 */
const PROSE_BREAK_CHARS = new Set([..."。！？!?；;，、：,:"]);
/** 48-character window; trimming more than half is worse than retaining a fragment, since this cell conveys liveness. */
const HEAD_FRAGMENT_MAX = 24;

const isDigitLike = (ch: string) => /[0-9０-９]/.test(ch);
const isSpace = (ch: string) => ch !== "" && /\s/.test(ch);

/**
 * **Pause characters inside numbers are not pauses.**
 *
 * ★★ Two independent review rounds converged on this: `13,500` / `13，500` / `18:30` / `9：30` / `3：1`
 *   contain punctuation separators; treating them as pauses ⇒ starting the window after them ⇒ rendering
 *   `500 万元`, `30`, `1`, **plausible-looking but incorrect numbers**.
 *   In BI this is far worse than a word fragment: fragments look broken, incorrect numbers read normally.
 * ⚠ Initially excluding only **ASCII** `,` `:` was a **point solution**:
 *   Chinese `9：30`, `3：1`, `13，500` use **full-width** characters and still failed (review probes).
 *   The predicate must be **width-independent**: is it between two digits?
 */
function isNumericSeparator(s: string, i: number): boolean {
  const ch = s.charAt(i);
  if (!NUMBER_INNER_CHARS.has(ch)) return false;
  if (!isDigitLike(s.charAt(i - 1)) || !isDigitLike(s.charAt(i + 1))) return false;
  // ★ Commas serve **two roles**: thousands separator and the most common Chinese pause. Checking only digits on both sides
  //   misclassifies real pauses: the comma in `…门店数为12，2026年将继续…` does have digits on both sides,
  //   so `2026` was discarded as a number fragment (third review; observed output `年将继续…`).
  //   Criterion: **exactly three digits after a thousands separator**. `2026` has four, beginning the next sentence.
  if (THOUSANDS_SEPARATORS.has(ch)) return digitRunLength(s, i + 1) === 3;
  return true;
}

/** Thousands-separator candidates (also common pause characters). Colons/decimal points/hyphens lack this ambiguity. */
const THOUSANDS_SEPARATORS = new Set([",", "，"]);

function digitRunLength(s: string, from: number): number {
  let i = from;
  while (i < s.length && isDigitLike(s.charAt(i))) i++;
  return i - from;
}

/**
 * Separators possibly **inside numbers**: thousands, time, ratios, decimal points, dates.
 *
 * ⚠ Only this small subset; **do not test the whole pause set** (second review):
 *   `。` in `…同比增长为12。2026年将继续…` also lies between two digits;
 *   using the whole set misclassifies a **real period** as a numeric separator ⇒ starting after it makes
 *   `headIsClean` false, then `leadingNumberEnd` consumes `2026` ⇒ rendering `年将继续…`.
 *   This breaks the positive control on which this fix depends.
 */
const NUMBER_INNER_CHARS = new Set([...",:，：.-/／·"]);

const isBreakAt = (s: string, i: number) =>
  isSpace(s.charAt(i)) || (PROSE_BREAK_CHARS.has(s.charAt(i)) && !isNumericSeparator(s, i));

/** First **real** pause in the window (also consume following pauses/whitespace); null if absent. */
function findBreak(s: string): { index: number; length: number } | null {
  for (let i = 0; i < s.length; i++) {
    if (!isBreakAt(s, i)) continue;
    let j = i;
    while (j < s.length && isBreakAt(s, j)) j++;
    return { index: i, length: j - i };
  }
  return null;
}

/**
 * Length of the initial number fragment: consecutive digits plus intervening numeric separators (including decimal points/date hyphens).
 *
 * ⚠ Caller must derive `startsOnSeparator` from the **original string**: when the cut falls on a separator
 * (`24,558,771` → `,771`), `isNumericSeparator` **cannot detect it inside the window**;
 * its preceding-digit check needs a character outside the window. See `proseTailOf`.
 */
function leadingNumberEnd(s: string, startsOnSeparator: boolean): number {
  let i = startsOnSeparator ? 1 : 0;
  while (i < s.length && (isDigitLike(s.charAt(i)) || isNumericSeparator(s, i))) i++;
  return i;
}

function dropLeadingFragment(s: string, midNumber: boolean, startsOnSeparator: boolean): string {
  // ① A midnumber cut must **at least** discard the number fragment, even without a later pause.
  //    Half a number is far more dangerous than half a word: `500万元` reads as a complete amount.
  const from = midNumber ? leadingNumberEnd(s, startsOnSeparator) : 0;
  const rest0 = s.slice(from);
  // ② Advance to the nearest real pause.
  const b = findBreak(rest0);
  if (b && b.index <= HEAD_FRAGMENT_MAX) {
    const rest = rest0.slice(b.index + b.length);
    if (rest.trim()) return rest;
  }
  return rest0.trim() ? rest0 : s;
}

function proseTailOf(
  peerId: string | null,
  peerProse: Record<string, string>,
  { allowFallback }: { allowFallback: boolean },
): { text: string; peerId: string | null } {
  let owner = peerId;
  let raw = ((peerId ? peerProse[peerId] : undefined) ?? "").trim();
  if (!raw && allowFallback) {
    for (const [k, v] of Object.entries(peerProse)) {
      if ((v ?? "").trim()) { raw = v.trim(); owner = k; }
    }
  }
  if (!raw) return { text: "", peerId: null };

  // ⚠ **Cut the tail before checking**, never reverse: a forbidden match discards the whole string, and prose grows monotonically;
  //   checking all prose means one `payload` occurrence hides it **for the rest of the turn**.
  //   Window first, so the effect heals as the window moves past it.
  const win = raw.slice(-(PROSE_TAIL_CHARS + PROSE_TAIL_GUARD));
  const tailStart = Math.max(0, win.length - PROSE_TAIL_CHARS);

  // ⚠ **Do not call `stripInternal` directly**, although the `INTERNAL_TOKENS` vocabulary is shared:
  //   it handles **whole strings**, and its first two rules misclassify windows cut from the middle:
  //   ① Discarding strings starting with `{` / `[` makes a cut advancing one character each frame flash away prose
  //      whenever it passes a `[` (e.g. `[1]` citation marker);
  //   ② `stripToolSlugSuffix` handles headings, not prose.
  //   Reuse only the **vocabulary**, with a separate predicate: discard only forbidden tokens **overlapping the visible window**.
  //   ★ Tokens within the margin (characters 49–80) do not count; they are outside the 48 visible characters;
  //     filtering them would let one `python_sandbox` swallow the next 32 prose characters too, causing flashes.
  for (const re of INTERNAL_TOKENS) {
    const rx = new RegExp(re.source, re.flags.includes("g") ? re.flags : `${re.flags}g`);
    let m: RegExpExecArray | null;
    while ((m = rx.exec(win)) !== null) {
      if (m.index + m[0].length > tailStart) return { text: "", peerId: null };
      if (m.index === rx.lastIndex) rx.lastIndex += 1;   // Prevent infinite loops on zero-width matches.
    }
  }

  // ★ Trim head: a last-N-character tail window **almost always begins with a partial word**.
  //   On 116, `合规风险告。 Now let me…` appeared; `告。` was the residual ending of
  //   the previous `…报告。`, cut by the window and appearing broken on screen.
  //   (The design mentioned only possible CSS truncation at the end, missing this start.)
  //   ★ **No guessing** needed: `windowed` is a suffix of `raw`; inspect its preceding character.
  //     Preceding punctuation/pause ⇒ starts at a sentence boundary (observed on 116); retain;
  //     preceding ordinary character ⇒ midword cut; trim.
  const windowed = win.slice(tailStart);
  const absStart = raw.length - windowed.length;
  const headIsClean = absStart === 0 || isBreakAt(raw, absStart - 1);
  //   ⚠ A midnumber cut is detected from the **window's first character**, not the preceding character
  //     (caught in second review): decimal `12.` / date `2025-08-` / full-width `2025／08／`
  //     are neither digits nor pause characters; checking them misses every case ⇒ renders `5%的同比增幅`
  //     (actual 12.5%), or `15日的门店营业数据` (actual 2025-08-15).
  //     A broken head (`!headIsClean`) makes initial digits attach to earlier content, necessarily a number fragment.
  //   ★★ Another case: cut **exactly on a separator** (`24,558,771` → `,771`).
  //     📏 On 116 v1.1.5 dogfood, observed `771 | 16.0% |` (actual 24,558,771).
  //     Window-only checks cannot detect this: `charAt(0)` is `,`, not a digit, and
  //     `isNumericSeparator` needs the **preceding** character, outside the window.
  //     Both checks must therefore use the **original string**: `isNumericSeparator(raw, absStart)`.
  const startsOnSeparator = !headIsClean && isNumericSeparator(raw, absStart);
  const midNumber = !headIsClean && (isDigitLike(windowed.charAt(0)) || startsOnSeparator);
  const text = (
    headIsClean ? windowed : dropLeadingFragment(windowed, midNumber, startsOnSeparator)
  ).trim();
  return { text, peerId: text ? owner : null };
}


/**
 * Peer terminal classification **on the band**.
 *
 * ⚠ Always passing `false` for `peerFailureRecovered` is **deliberate**: production derives it from
 * `cardFinalState === "completed"`; the only writer is
 * `step_list_card_finalized` (emitted at turn completion), whereas the band requires `runner.running`.
 * They overlap only for one or two frames between `card_finalized` and `done`;
 * **`bail` is effectively unreachable during the band's lifecycle**.
 *
 * Thus the band has only two states: `fail` (red), `done` (green).
 * ★ An earlier amber bail state + dedicated glyph + two i18n entries were dead code, caught by independent review:
 *   the same reasoning as design §2-E's unreachable terminal-failure state → **omit**, yet
 *   opposite conclusions were drawn for equivalent facts. Actual behavior would only flash red to amber
 *   one frame before unmount, worse than omitting it.
 * ★ Reporting that a peer did not finish during execution is **truthful**: it did not.
 *   Discounting failure because Lead took over is a **retrospective** judgment, valid only after turn completion;
 *   then the ledger (`AgentTrace` → `deriveTerminalStatus` with real `peerFailureRecovered`)
 *   correctly renders bail. Clear roles: band reports the present, ledger reports retrospectively.
 *
 * Still **reuse** `deriveTerminalStatus` (inventory §2.5 explicitly prohibits a new implementation in the band).
 */
function bandTerminalStatus(terminal: PeerTerminal): StepStatus {
  return deriveTerminalStatus(terminal, false);
}

/**
 * Build a row from peer terminal facts. Caller supplies `key`:
 * independent terminal rows use `peer:<id>`; **rewritten spawn rows** retain their `stepId`,
 * avoiding replay of entry animation from a changed React key when the terminal arrives.
 */
function peerRowFrom(
  peerId: string,
  terminal: PeerTerminal,
  peerNames: Record<string, string>,
  peerDisplayNames: Record<string, string>,
  key: string,
): BandRow | null {
  const status = bandTerminalStatus(terminal);
  if (status !== "fail" && status !== "done") return null;
  const name = peerNames[peerId] ?? "";
  return {
    key,
    kind: "peer",
    label: "",
    meta: formatPeerDuration(terminal.durationMs) ?? "",
    tone: status,
    // ⚠ **Leave names empty** if unavailable; rendering uses the two unnamed i18n entries.
    //   Previously fell back to `AGENT_META.generic.zh` (`专家`), yielding
    //   `专家 didn't finish` in en-US: hardcoded Chinese in English UI, caught by independent review.
    //   A real path: `peer_dispatch_failed` before `agent_spawned` leaves
    //   neither peerNames nor peerDisplayNames containing this peer yet.
    peerName: name
      ? agentDisplayName(name, peerDisplayNames[peerId] ?? null)
      : (peerDisplayNames[peerId] ?? ""),
    peerOutcome: status,
  };
}

/**
 * Build independent terminal rows for peers whose rows are not carried elsewhere.
 *
 * Caller computes exclusions (`rowsCarriedElsewhere`): peers with real child steps,
 * or successful peers carried by rewritten spawn rows. **Failed peers never belong to this set**;
 * see `BandRow.kind`'s asymmetric fail/done rules.
 */
function peerTerminalRows(
  peerTerminalById: Record<string, PeerTerminal>,
  rowsCarriedElsewhere: ReadonlySet<string>,
  peerNames: Record<string, string>,
  peerDisplayNames: Record<string, string>,
): { evtSeq?: number; row: BandRow }[] {
  const out: { evtSeq?: number; row: BandRow }[] = [];
  for (const [peerId, terminal] of Object.entries(peerTerminalById)) {
    if (rowsCarriedElsewhere.has(peerId)) continue;
    const row = peerRowFrom(peerId, terminal, peerNames, peerDisplayNames, `peer:${peerId}`);
    if (row) out.push({ evtSeq: terminal.evtSeq, row });
  }
  return out;
}

/**
 * Compute the band's three rows from the step stream.
 *
 * Five invariants, all locked down by tests:
 *   ① `history.length` always equals `HISTORY_ROWS` (density `rows`); pad with skeletons
 *      to maintain constant height;
 *   ② Empty steps mean phase = preparing, not no band; occupy space even during the opening three seconds;
 *   ③ No active step means preparing copy, **never** reporting completed work as `正在…`;
 *   ④ Current step is the **last** active one (newest rather than oldest under concurrency);
 *   ⑤ **Concurrency never loses a step**: history contains the last two non-current steps in occurrence order.
 */
export function buildBandModel({
  steps,
  answerStreaming = false,
  peerNames = {},
  peerDisplayNames = {},
  peerTerminalById = {},
  peerProse = {},
  peerIdsWithSteps,
  peerIdsWithSpawnRow,
}: BuildBandInput): BandModel {
  // ── Each peer appears once on the band, **where its terminal actually occurred** ─────────
  //
  // Peer terminal outcomes own their timeline position and severity. Failed
  // peers replace their spawn row to avoid duplicate outcomes; successful peers
  // with child steps need no extra row. Zero-child successful peers retain the
  // rewritten spawn position. Use unthrottled child membership so queued steps
  // cannot retract a terminal outcome as the visible prefix advances.
  const childStepPeerIds =
    peerIdsWithSteps
    ?? new Set(
      steps.filter((st) => !st.peerProseKey)
        .map((st) => st.subagentId).filter((v): v is string => Boolean(v)),
    );
  const failPeerIds = new Set(
    Object.entries(peerTerminalById)
      .filter(([, t]) => bandTerminalStatus(t) === "fail")
      .map(([id]) => id),
  );
  /** This peer's success is carried by its rewritten spawn row (zero child steps + spawn row present). */
  const spawnRowPeerIds =
    peerIdsWithSpawnRow
    ?? new Set(steps.map((st) => st.peerProseKey).filter((v): v is string => Boolean(v)));
  const doneViaSpawnRow = new Set(
    [...spawnRowPeerIds].filter((id) => !childStepPeerIds.has(id) && !failPeerIds.has(id)
      && peerTerminalById[id] !== undefined),
  );

  // Count observed failed/partial steps, including hard failures; no retry receipt is inferred.
  const issueCount = countStepIssues(steps);
  // Independent terminal rows are **omitted** for successful peers with child steps (already represented by green steps),
  // or success carried by rewritten spawn. Failures always receive rows.
  const ownedPeerIds = new Set(
    [...childStepPeerIds, ...doneViaSpawnRow].filter((id) => !failPeerIds.has(id)),
  );
  const peerRows = peerTerminalRows(
    peerTerminalById, ownedPeerIds, peerNames, peerDisplayNames,
  );
  // Count peers **with** child steps too: although they lack a historical row, the fact that an agent did not finish
  // must remain persistently visible in the disclosure strip.
  const peerFailCount = Object.values(peerTerminalById).filter(
    (t) => bandTerminalStatus(t) === "fail",
  ).length;
  // ⚠ A pure-chat turn has **no execution facts this turn**, rather than merely zero steps:
  //   collapsing a zero-step turn with peer terminals to one row discards that row.
  const density: BandDensity =
    answerStreaming && steps.length === 0 && peerRows.length === 0 ? "single" : "rows";

  // Current step = **last** active one (newest under concurrent peers).
  // ★★ Remove failed peers' spawn rows **before selecting the current step** (eighth independent review).
  //    `peer_dispatch_failed` emits **inside** the `subagent_spawn` tool
  //    (`subagent.py:5930`, before the tool returns), creating a real window where
  //    terminal is already failed but spawn still has `state:"active"`.
  //    Filtering only historical candidates (`others`) previously rendered history as `主分析 未完成`
  //    while current simultaneously showed `正在派子 agent` + spinner: two faces for one fact, one untruthful,
  //    precisely the shape this rule removes.
  // ⚠ `stepOrdinal` still counts unfiltered `steps`: that step **really occurred**;
  //    `第 N 步` counts executed tool steps, not surviving on-screen rows.
  const visibleSteps = steps.filter((st) => {
    // Failed peer spawn rows duplicate the red terminal fact and would misreport it.
    if (st.peerProseKey && failPeerIds.has(st.peerProseKey)) return false;
    // Failed peer child steps **still marked active**: cancellation / timeout may leave backend without a later
    // `step_done`; those steps never finish. Retaining them shows `主分析 · 正在查询数据` + spinner on the current row,
    // while history simultaneously says `主分析 未能完成`, two faces for one fact again (ninth review).
    // ⚠ Remove only **active** steps; completed child steps are real facts and remain.
    if (st.state === "active" && st.subagentId && failPeerIds.has(st.subagentId)) return false;
    return true;
  });

  // ★★ **Spawn rows cannot be current rows** (116 dogfood P1).
  //    On 116, current stayed `正在派子 agent` unchanged for 20 seconds while disclosure advanced from step 51
  //    to 59 and retries 7→9. Two problems combined:
  //    ① `派子 agent` is jargon (backend `_TOOL_HEADER_ZH`), against PRD A5's human-readable main-surface requirement;
  //    ② `subagent_spawn` remains active for the peer's **entire lifecycle**, and selecting
  //       the last active step falls back to it whenever no other step is active, regardless of peer activity,
  //       freezing the current row and violating interaction principle 2:
  //       continuous change during waiting.
  //    Semantically it delegates work rather than performing the current work. After skipping it:
  //    active peer child step → show that step; none → `正在等 X 返回` (below).
  //    ⚠ Attribute accurately: both issues **predate this sprint**; after ③ disabled the ledger,
  //      the band became the only main-surface source instead of having a neighboring ledger, amplifying existing issues.
  //    ⚠ **Use tool name, not `peerProseKey`, as the predicate** (2026-08-16, 116 dogfood):
  //      📏 Observed `正在派子 agent` from ms 75603 to 181201 (**105 seconds**),
  //      the exact shape P1 should remove. Unit repro: with `peerProseKey`, the same spawn step gives
  //      `what=""`; removing that field gives `what="派子 agent"`.
  //      Semantically **tool name** determines whether a row is spawn; `peerProseKey` instead identifies
  //      prose ownership, so substituting it answers a different question.
  //      ❗ **Why that row lacked `peerProseKey` remains unknown**, as independent review noted:
  //        `applyPeerProseKey` runs on `peer_dispatched` (`turn-store-dispatch.ts:498`),
  //        not on first prose, so the no-prose-yet explanation is **incorrect**; do not repeat it.
  //        ⚠ Effects extend beyond this branch: `doneViaSpawnRow`, failed-peer spawn filtering,
  //        and `rowPeerIdOf` prose ownership use the same field and **all fail** in that state.
  //        This rule addresses only the symptom of spawn becoming current.
  //      Retain both checks: tool name is authoritative; `peerProseKey` covers old events lacking tool names.
  const isSpawnRow = (st: StepView): boolean =>
    st.toolName === "subagent_spawn" || Boolean(st.peerProseKey);
  let activeIdx = -1;
  for (let i = visibleSteps.length - 1; i >= 0; i -= 1) {
    if (visibleSteps[i].state === "active" && !isSpawnRow(visibleSteps[i])) {
      activeIdx = i; break;
    }
  }
  // ⚠ No active step = **gap** (previous step finished, next not started; PRD identifies this as the longest
  //    interval in long turns). **Do not** use the last step as current: that puts completed work
  //    beside `正在…` + spinner + running seconds, misreporting for the entire gap. Use preparing copy.
  const cur = activeIdx >= 0 ? visibleSteps[activeIdx] : null;

  // History = last HISTORY_ROWS steps **excluding current**, in occurrence order.
  // ⚠ Former `steps.slice(0, activeIdx)` made concurrent completed steps after active
  //    **disappear entirely** (neither historical nor current). Users saw the step count increase
  //    while a row vanished, contributing to the impression that execution was stuck.
  const others = activeIdx >= 0
    ? [...visibleSteps.slice(0, activeIdx), ...visibleSteps.slice(activeIdx + 1)]
    : visibleSteps;
  // Merge steps and zero-child-step peer terminals into one timeline, then take the last HISTORY_ROWS items.
  // ⚠ Merge **before** tail slicing: slicing steps then adding peers always puts peers last,
  //   equivalent to the rejected always-append policy.
  // Spawn handling (same source as the rule above):
  //   · Failed peer → entire row already removed by `visibleSteps` (both current and historical;
  //     otherwise current reports `正在派子 agent` for an already failed peer).
  //   · Done peer with zero child steps → rewrite as peer row in place (retain position without an extra row).
  //   · Otherwise → ordinary step row as usual.
  const rowForStep = (st: StepView): BandRow => {
    const spawnFor = st.peerProseKey ?? "";
    if (spawnFor && doneViaSpawnRow.has(spawnFor)) {
      const terminal = peerTerminalById[spawnFor];
      const row = terminal
        ? peerRowFrom(spawnFor, terminal, peerNames, peerDisplayNames, st.stepId)
        : null;
      // Without terminal duration, retain this step's own meta rather than replacing existing information with blank.
      if (row) return row.meta ? row : { ...row, meta: metaOf(st) };
    }
    return rowOf(st);
  };
  const tail = mergeTimeline(others, peerRows)
    .slice(-HISTORY_ROWS)
    .map((it) => (it.kind === "step" ? rowForStep(it.step) : it.row));
  const history: BandRow[] = [];
  if (density === "rows") {
    for (let i = 0; i < HISTORY_ROWS - tail.length; i += 1) history.push(skeletonRow(i));
    history.push(...tail);
  }

  if (!cur) {
    // Both cases enter here: ① no steps yet (opening); ② previous finished, next not started (gap).
    // Both mean thinking about the next step to users, so share preparing copy.
    // ⚠ `agentName` must exist; omitting a footnote row makes this state shorter, whereas constant height
    //    applies from the first frame. This row implements it, rather than decorating.
    // ⚠ Ownership comes from the last **visible** step, but genuine opening must use **original** `steps`:
    //   conflating them misclassifies all steps removed by visibleSteps as opening,
    //   entering `allowFallback: true` and attaching prose from a peer that never executed steps
    //   to the current row, precisely the misattribution restricted fallback prevents (ninth review).
    const last = visibleSteps.length > 0 ? visibleSteps[visibleSteps.length - 1] : null;
    const trulyOpening = steps.length === 0;
    // Last visible step is spawn for a peer without a terminal yet = waiting for that peer to return.
    // More truthful than `正在想下一步` (Lead is waiting, not thinking), and more readable than `正在派子 agent`.
    // ⚠ **Require `peerProseKey`** (independent review): the freshness guard below
    //   `peerTerminalById[st.peerProseKey ?? ""]` is **ineffective for keyless rows**
    //   (`peerTerminalById[""]` always undefined). Since `isSpawnRow` also recognizes tool names,
    //   an already failed peer's spawn row can still be classified as waiting,
    //   rendering `fail` in history and `正在等一个 Agent 返回` as current in the same frame,
    //   the two-faces-for-one-fact bug explicitly addressed above.
    //   Without identifying the peer, **do not** claim to await it; gap copy is more truthful.
    const waitingSpawn = visibleSteps.find(
      (st) => st.state === "active" && isSpawnRow(st)
        && Boolean(st.peerProseKey)
        && peerTerminalById[st.peerProseKey ?? ""] === undefined,
    ) ?? null;
    return {
      phase: "preparing",
      density,
      stepOrdinal: steps.length,
      issueCount,
      peerFailCount,
      history,
      current: last
        ? {
          agent: agentKeyOf(waitingSpawn ?? last, peerNames),
          agentName: displayNameOf(waitingSpawn ?? last, peerNames, peerDisplayNames),
          agentBadgeName: agentNameOf(waitingSpawn ?? last, peerNames) ?? "lead",
          owner: ownerIdOf(waitingSpawn ?? last, peerNames),
          // ⚠ **Do not** use `displayNameOf` fallback when the name is missing; it supplies Lead's name,
          //   rendering `正在等 Bowhead 返回`, Lead waiting for itself (one observed frame on 116).
          waitingOnPeer: waitingSpawn && agentNameOf(waitingSpawn, peerNames)
            ? displayNameOf(waitingSpawn, peerNames, peerDisplayNames)
            : null,
          waitingOnPeerUnknown: waitingSpawn !== null && !agentNameOf(waitingSpawn, peerNames),
          what: "", detail: [], handoffFrom: null, toolCat: null,
          // ★ Gaps need prose most: everything else in this state is static.
          // Steps imply an owner, **no fallback** (otherwise Lead's steps gain another peer's prose).
          prose: proseTailOf(
            rowPeerIdOf(last, peerNames), peerProse, { allowFallback: false },
          ).text,
        }
        // No visible steps has two cases that **must stay distinct**:
        //   · Genuine opening (original `steps` also empty) → no owner; allow fallback.
        //   · Steps exist but `visibleSteps` removed all (all peers failed) → **no fallback**,
        //     or prose from a peer that never executed steps gets attached (ninth review).
        : !trulyOpening
          ? {
            agent: "lead", agentName: LEAD_BRAND_NAME, agentBadgeName: "lead",
            owner: "lead", waitingOnPeer: null, waitingOnPeerUnknown: false,
            what: "", detail: [], handoffFrom: null, toolCat: null, prose: "",
          }
        : ((): BandCurrent => {
          // No steps = no owner, precisely the fallback branch.
          const fb = proseTailOf(null, peerProse, { allowFallback: true });
          // ⚠ Assign this row to the prose owner. Always assigning Lead renders
          //   Bowhead with another peer's words, the same misattribution just blocked in the model
          //   (caught independently; particularly apparent with concurrent prose-only peers).
          const name = fb.peerId ? peerNames[fb.peerId] : undefined;
          if (!name || !fb.peerId) {
            return {
              agent: "lead", agentName: LEAD_BRAND_NAME, agentBadgeName: "lead",
              owner: "lead",
              waitingOnPeer: null,
              waitingOnPeerUnknown: false,
              what: "", detail: [], handoffFrom: null, toolCat: null, prose: fb.text,
            };
          }
          return {
            agent: agentKeyFor(name),
            agentName: agentDisplayName(name, peerDisplayNames[fb.peerId] ?? null),
            agentBadgeName: name,
            owner: fb.peerId,
            waitingOnPeer: null,
            waitingOnPeerUnknown: false,
            what: "", detail: [], handoffFrom: null, toolCat: null, prose: fb.text,
          };
        })(),
    };
  }

  // Handoff = current owner differs from the **immediately preceding step**.
  // ⚠ A previous step **still running** is not a handoff (final review): with concurrent peers,
  //   `接手自 X` is **false** while X still works; it has not handed over.
  //   Handoff means previous owner finished and ownership changed, requiring prev to leave active.
  const prevIdx = activeIdx > 0 ? activeIdx - 1 : -1;
  const prev = prevIdx >= 0 ? visibleSteps[prevIdx] : null;
  const handoff =
    prev && prev.state !== "active"
    && ownerIdOf(prev, peerNames) !== ownerIdOf(cur, peerNames);
  // ⚠ Do not announce when both **display names match** (observed on 116 dogfood): one plan turn spawned three `compute`
  //   peers all named `主分析`, producing the footnote
  //   `主分析 · 2025年 天津 营业额 按种类大类 明细 · 接手自 主分析`,
  //   with identical names for current agent and handoff source: uninformative to users
  //   and apparently buggy. Different peer IDs are **internal facts**; matching display names mean **no user-visible owner change**.
  const prevName = prev ? displayNameOf(prev, peerNames, peerDisplayNames) : "";
  const curName = displayNameOf(cur, peerNames, peerDisplayNames);
  const announceHandoff = handoff && prevName !== curName;

  return {
    phase: "running",
    density,
    stepOrdinal: steps.length,
    issueCount,
    peerFailCount,
    history,
    current: {
      agent: agentKeyOf(cur, peerNames),
      agentName: curName,
      agentBadgeName: agentNameOf(cur, peerNames) ?? "lead",
      // ⚠ Use the **non-active** name: rendering wraps current copy in `band_doing` (`正在{what}`),
      //   while catalog `activeName` already contains `正在` (e.g. `正在整理结果`); passing true renders
      //   `正在正在整理结果`. Previously backend headings almost always prevented catalog fallback;
      //   filtering jargon above now reaches it. History has no wrapper and still uses active names (see :291).
      what: humanLabel(cur, false),
      detail: [stripInternal(cur.label), stripInternal(cur.durationText)].filter(Boolean),
      owner: ownerIdOf(cur, peerNames),
      waitingOnPeer: null,
      waitingOnPeerUnknown: false,
      handoffFrom: announceHandoff ? prevName : null,
      toolCat: toolCatOf(cur),
      prose: proseTailOf(
        rowPeerIdOf(cur, peerNames), peerProse, { allowFallback: false },
      ).text,
    },
  };
}
