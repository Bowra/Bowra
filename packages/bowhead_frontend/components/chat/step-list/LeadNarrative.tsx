"use client";

import { useTranslations } from "next-intl";
import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { scrollToArtifact } from "@/lib/artifact-chip";
import { appFetch, appPath } from "@/lib/base-path";
import { useKbChunkContent } from "@/lib/canvas-pm-adapter/kb-chunk-cache";
import type { KbChunkSummary } from "@/lib/kb-api";
import { quoteLines } from "@/lib/kb-snippet";
import { requestCanvasDataFocus, requestCanvasResultOpen } from "@/lib/canvas-workspace-focus";
import { detectCjkContentLang } from "@/lib/content-lang";
import { Markdown, stripFigureAnchors } from "@/lib/md";
import type { Citation, CitationKbRef } from "@/lib/types";
// prod-bug-bash-038 #2: surface a toast when chip click can't find target
// instead of silent no-op (user perceived chip as "unclickable").
import { toast } from "@/lib/use-toast";

import { Cursor } from "./primitives";
import { CharIcon } from "@/components/char-icon";

/** prod-bug-bash-039 Phase 5 (0.39.3) — turn-scoped citation registry context.
 *
 *  Avoids prop-drilling through ChatBubbleNew → LeadNarrative → ArtifactChip
 *  (3 layers × many call sites). Provider wraps a turn's chat-bubble subtree
 *  with the current `runner.citations`; ArtifactChip reads via `useCitations`
 *  to resolve `[cite:N]` markers (artifactId="cite:N", isCiteMarker=true) to
 *  the real artifact id for scrollIntoView.
 *
 *  Default = empty array — so consumers that don't wrap (mock scenarios, old
 *  call sites) keep current behavior (chip click → toast fallback). The
 *  context value should be referentially stable across renders; callers
 *  typically pass `runner.citations` straight through. */
const CitationsContext = createContext<readonly Citation[]>([]);

export interface CitationsProviderProps {
  citations?: readonly Citation[];
  children: ReactNode;
}

export function CitationsProvider({ citations, children }: CitationsProviderProps) {
  return (
    <CitationsContext.Provider value={citations ?? []}>
      {children}
    </CitationsContext.Provider>
  );
}

/** **Projected** entries from this turn's `lead.final.payload.sources` (viewer-safe).
 *
 *  Chat citation chips can receive an
 *  `artifactId` that is a viewer alias (`source-ref-v1:…`), neither a DOM section id
 *  nor an openable artifact id, so clicking could only show a hint.
 *
 *  ★Joining works because aliases are **identity-derived**: `ViewerSourceAliasMap.alias_for` signs valid identities as
 *  `["source-ref-v1", workspace_id, report_scope_id, identity]`, excluding **slot** and caching by identity.
 *  Thus one artifact gets **the same alias** in `turn.citation_registered` and `lead.final.payload.sources`,
 *  enabling an exact join rather than guessing by sequence (seq and ordinal need not share a source).
 *
 *  ⚠Only `access_state === "linked_accessible"` entries carry `safe_*`; inaccessible ones deliberately
 *  carry only aliases (paired backend tests pin this; do not try to fill them in on the frontend). */
export interface ChatSourceEntry {
  id?: string;
  source_alias?: string;
  access_state?: string;
  safe_label?: string;
  safe_query?: string;
  safe_preview_url?: string;
  ordinal?: number;
}

const ChatSourcesContext = createContext<readonly ChatSourceEntry[]>([]);

export interface ChatSourcesProviderProps {
  sources?: readonly ChatSourceEntry[];
  children: ReactNode;
}

export function ChatSourcesProvider({ sources, children }: ChatSourcesProviderProps) {
  return (
    <ChatSourcesContext.Provider value={sources ?? []}>
      {children}
    </ChatSourcesContext.Provider>
  );
}

export function useChatSources(): readonly ChatSourceEntry[] {
  return useContext(ChatSourcesContext);
}

/** Extract projected sources from any final payload (backend-contract shape, narrowed here only). */
export function chatSourcesFromPayload(payload: unknown): ChatSourceEntry[] {
  if (!payload || typeof payload !== "object") return [];
  const raw = (payload as { sources?: unknown }).sources;
  if (!Array.isArray(raw)) return [];
  return raw.filter((entry): entry is ChatSourceEntry => Boolean(entry) && typeof entry === "object");
}

/** Find this turn's source by exact alias; undefined on miss (caller falls back to the existing hint). */
export function findChatSource(
  sources: readonly ChatSourceEntry[],
  alias: string,
): ChatSourceEntry | undefined {
  if (!alias) return undefined;
  return sources.find((entry) => entry.source_alias === alias || entry.id === alias);
}

/** Lightweight source card expanded from a citation chip.
 *
 *  Show only data the backend **already deemed safe to deliver**: safe_label / safe_query and
 *  initial original-text rows lazily fetched via `safe_preview_url`. Any failure degrades to name-only:
 *  sources are auxiliary information and should not look broken when fetching fails.
 *
 *  ⚠**Rows are not always available: a known boundary, not a bug**. `safe_preview_url` calls
 *  `/canvas/by-session/{sid}/artifacts/{aid}/preview`, using that route's **own** canvas
 *  closure (`_report_artifact_closure`; no canvas means immediate 404). Thus:
 *    · Referenced artifacts inside canvas closure (report chart data) → rows available;
 *    · Pure-chat artifacts (`canvas_id=None`) → 404 → name and query only.
 *  Rows for the latter require widening the preview route's closure, a **data egress** change
 *  an order of magnitude riskier than projection, outside this scope; do not adjust it incidentally.
 *
 *  ⚠Do not reuse canvas `citation-popover` (709 lines, tightly coupled to `useCanvasContext` +
 *  `useSourcePanel`): generalizing it is a cross-surface refactor that collides with ongoing citation work. */
function ChatSourceCard({
  source,
  fallbackLabel,
  onClose,
}: {
  source: ChatSourceEntry;
  fallbackLabel: string | null;
  onClose: () => void;
}) {
  const tStep = useTranslations("step_list");
  const [rows, setRows] = useState<{ columns: string[]; rows: Record<string, unknown>[]; total: number | null } | null>(null);
  const url = source.safe_preview_url;

  useEffect(() => {
    if (!url) return;
    let cancelled = false;
    void appFetch(`${url}?mode=rows&max_rows=5`)
      .then((r) => (r.ok ? r.json() : null))
      .then((preview) => {
        if (cancelled || !preview || preview.error) return;
        const cols = Array.isArray(preview.columns) ? preview.columns.map(String) : [];
        const body = Array.isArray(preview.rows) ? preview.rows : [];
        if (!cols.length || !body.length) return;
        setRows({ columns: cols.slice(0, 5), rows: body.slice(0, 5), total: preview.total_rows ?? null });
      })
      .catch(() => {
        // On fetch failure show only the name, without an error state.
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  const title = source.safe_label || fallbackLabel || "";
  return (
    <span className="bh-chat-source-card" role="note">
      <span className="bh-chat-source-card-head">
        <span className="bh-chat-source-card-title">{title}</span>
        <button type="button" onClick={onClose} aria-label={tStep("cite_source_close")}>
          <CharIcon ch="×" aria-hidden />
        </button>
      </span>
      {source.safe_query && source.safe_query !== title ? (
        <span className="bh-chat-source-card-query">{source.safe_query}</span>
      ) : null}
      {rows ? (
        <span className="bh-chat-source-card-rows">
          <table>
            <thead>
              <tr>
                {rows.columns.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.rows.map((row, i) => (
                <tr key={i}>
                  {rows.columns.map((c) => (
                    <td key={c}>{row[c] == null ? "" : String(row[c])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {rows.total != null && rows.total > rows.rows.length ? (
            <span className="bh-chat-source-card-more">
              {tStep("cite_source_rows", { shown: rows.rows.length, total: rows.total })}
            </span>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}

/** KB chunk location in its file: page → chapter → paragraph N; use the first populated item. */
export function kbChunkLocation(chunk: KbChunkSummary, tStep: ReturnType<typeof useTranslations>): string | null {
  if (chunk.page != null && chunk.page > 0) return tStep("cite_kb_page", { page: chunk.page });
  if (chunk.section_title) return chunk.section_title;
  if (chunk.chunk_index > 0) return tStep("cite_kb_paragraph", { n: chunk.chunk_index });
  return null;
}

/** KB deep link: open the file drawer with the cited chunk first. */
export function kbCitationHref(kb: CitationKbRef): string {
  const qs = new URLSearchParams();
  if (kb.scope) qs.set("scope", kb.scope);
  if (kb.spaceId) qs.set("space", kb.spaceId);
  qs.set("file_id", kb.fileId);
  qs.set("chunk_id", kb.chunkId);
  return appPath(`/knowledge/?${qs.toString()}`);
}

function KbDocIcon({ size }: { size: "chip" | "title" }) {
  // Two sizes in styles/ui-controls-legacy.css .legacy-kb-doc-icon--chip / --title (formerly inline styles, workbench/canvas batch 1).
  return <span aria-hidden className={`bh-kb-doc-icon legacy-kb-doc-icon legacy-kb-doc-icon--${size}`} />;
}

/** Source card expanded from a KB citation (design 08 · B).
 *
 *  Appears only when backend allows viewing (`linked_accessible` + KB location). Citations do not store original text; fetch under
 *  the current viewer's permissions (`kb_space_id` supplies the Space header, same gate as the KB page):
 *    · Chunk still exists → filename, location, original text (up to 8 lines, locate/highlight by longest match to prose before citation);
 *    · Chunk gone (file reprocessed) → "原文已更新", never present current content as the cited text;
 *    · Read failure → location and open entry only. */
function KbChatSourceCard({
  kb,
  title,
  context,
  onClose,
}: {
  kb: CitationKbRef;
  title: string;
  context: string;
  onClose: () => void;
}) {
  const tStep = useTranslations("step_list");
  // Fetch only the cited chunk, not the whole file's chunk list.
  const state = useKbChunkContent(kb.fileId, kb.chunkId, kb.spaceId, { single: true });
  const scopeKey = kb.scope === "personal" || kb.scope === "space" || kb.scope === "team" ? kb.scope : null;
  const location = state.status === "ready" ? kbChunkLocation(state.chunk, tStep) : null;
  const loc = [tStep("cite_kb_prefix"), scopeKey ? tStep(`cite_kb_scope_${scopeKey}`) : null, location]
    .filter(Boolean)
    .join(" · ");
  const lines = state.status === "ready" ? quoteLines(state.content, context) : [];
  const isTable = state.status === "ready" && state.content.trimStart().startsWith("|");
  return (
    <span className="bh-chat-source-card bh-kb-source-card" role="note">
      <span className="bh-chat-source-card-head">
        <span className="bh-chat-source-card-title bh-kb-source-card-title">
          <KbDocIcon size="title" />
          {title}
        </span>
        <button type="button" onClick={onClose} aria-label={tStep("cite_source_close")}>
          <CharIcon ch="×" aria-hidden />
        </button>
      </span>
      <span className="bh-chat-source-card-query">{loc}</span>
      {state.status === "ready" ? (
        <span className={isTable ? "bh-kb-source-quote bh-kb-source-quote-table" : "bh-kb-source-quote"}>
          {lines.map((line, i) => (
            <span key={i} className="bh-kb-source-quote-line">
              {line.before}
              {line.match ? <mark>{line.match}</mark> : null}
              {line.after}
            </span>
          ))}
        </span>
      ) : state.status === "empty" ? (
        <span className="bh-kb-source-note">{tStep("cite_kb_updated")}</span>
      ) : state.status === "error" ? (
        <span className="bh-kb-source-note">{tStep("cite_kb_unavailable")}</span>
      ) : (
        <span className="bh-chat-source-card-more">{tStep("cite_kb_loading")}</span>
      )}
      {(kb.sourceCheckState === "source_temporarily_unavailable" || (state.status === "ready" && state.chunk.current_source_state && state.chunk.current_source_state !== "eligible")) && <span className="bh-kb-source-note">{tStep("cite_kb_current_warning")}</span>}
      {state.status === "ready" && state.chunk.historical_generation && <span className="bh-kb-source-note">{tStep("cite_kb_historical")}</span>}
      <span className="bh-kb-source-acts">
        <a href={kbCitationHref(kb)}>{tStep("cite_kb_open")}</a>
      </span>
    </span>
  );
}

/** Hook used by ArtifactChip. Exported for tests + future call sites. */
export function useCitations(): readonly Citation[] {
  return useContext(CitationsContext);
}

export interface LeadNarrativeProps {
  text: string;
  /** When true, a blinking caret follows the last char. */
  isStreaming?: boolean;
  /** Font scale — answer block uses slightly larger size. */
  size?: number;
  /**
   * chat-inline-figure-2026-08 batch 2: supplying this enables inline figure slots; a standalone body line
   * `[figure:<id>]` renders its artifact. Return `null` for an unknown id (anchor silently disappears).
   *
   * **Opt-in**: only answer bubbles pass it; intermediate narrative omits it ⇒ current behavior unchanged.
   */
  renderFigureSlot?: (figureId: string) => React.ReactNode | null;
}

/**
 * Streaming narrative bubble — Lead's voice between steps.
 *
 * Both modes render through the same full `<Markdown>` (react-markdown +
 * remark-gfm), so lists / nested lists / tables / bold keep the same
 * structure while streaming and after the turn settles (live == replay).
 * The only differences: streaming appends the caret and strips `[figure:]`
 * anchors (figure slots only render on the settled answer bubble).
 *
 * Artifact `[data:art_*]` / `[cite:N]` tokens become inline chips in the
 * HAST stage (`rehypeArtifactChips`) in both modes. react-markdown escapes
 * by default — no HTML serialization path (no XSS surface).
 */
// Defense-in-depth: strip internal `<response_kind>` / `<headline>` markers at
// the render boundary. The backend streaming scanner extracts these to SSE
// events and strips them from narrative deltas, but its stateful per-delta
// substate has documented "disorder" edge cases (tag split across deltas) that
// leak a raw `<headline>…</headline>` into the prose (user-reported). The FE
// sees the fully-accumulated text, so a post-hoc sanitize here reliably catches
// the complete tag regardless of how it streamed — mirrors the backend's
// `_POST_HOC_SANITIZE_RE` (response_kind_scanner.py:130). See
// [[feedback_llm_internal_syntax_unreliable]].
const _INTERNAL_MARKER_RE =
  /(?:<\s*response_kind\s*>|_kind\s*>)\s*(?:chat|canvas)\s*<\/\s*response_kind\s*>|<\s*headline\s*>[\s\S]*?<\/\s*headline\s*>/gi;
// Mid-stream: an opened-but-not-yet-closed marker at the tail (e.g. `<headline>导`).
const _DANGLING_MARKER_RE = /<\s*(?:response_kind|headline)\s*>[^<]*$/i;

/** Exported so narrative/answer dedup compares the same prose the user sees. */
export function stripInternalMarkers(text: string): string {
  if (!text || (!text.includes(">") && !text.toLowerCase().includes("headline"))) {
    return text;
  }
  return text.replace(_INTERNAL_MARKER_RE, "").replace(_DANGLING_MARKER_RE, "");
}

export function LeadNarrative({
  text,
  isStreaming = false,
  size = 15,
  renderFigureSlot,
}: LeadNarrativeProps) {
  // Figure anchors render figures only in **answer bubbles** (the location supplying renderFigureSlot);
  // **strip** them in the other two cases, keeping `[figure:` literals off every surface:
  //   · Streaming: shim bypasses rehype; without stripping, anchors flash until streaming completes;
  //   · Surfaces without a callback (intermediate narrative / fallbackNarrative) have no plugin,
  //     so unstripped anchors render as ordinary text. Lead prose gets anchors only after batch 3 enables them;
  //     these two paths could not encounter them before, but must block them afterward.
  const stripped = stripInternalMarkers(text);
  const clean = renderFigureSlot && !isStreaming ? stripped : stripFigureAnchors(stripped);
  // P2 batch 6: CJK answers carry their language so the content measure (40 CJK / ~75 Latin characters
  // per line, keyed off :lang()) fits the answer, not the UI language.
  const lang = detectCjkContentLang(clean) ?? undefined;
  if (isStreaming) {
    // Artifacts already require `!isStreaming`, so no figures during streaming matches existing behavior.
    return renderStreaming(clean, size, lang);
  }
  return renderFinalized(clean, size, renderFigureSlot, lang);
}

// Streaming and completion **share one** `<Markdown>` renderer (live == persist == replay).
//
// Streaming previously used a lightweight shim splitting only blank lines and recognizing only **bold**; single `\n` within paragraphs became
// ordinary whitespace in `<p>`, collapsing lists, nested lists, and headings into one paragraph,
// restored only by rerendering persisted content after submission. One text, two structures means live ≠ replay.
// The shim intended to prevent flicker from incomplete tables, but rendered tables as vertical bars, worse than flicker;
// an incomplete Markdown header is temporarily ordinary text, becoming a table when the separator line arrives.
// Also removes 0.39.8's font-size jump on typing→completion: both states now output the same structure.
function renderStreaming(text: string, _size: number, lang?: string) {
  return (
    <div style={{ margin: "6px 0" }} className="lead-narrative-md" lang={lang}>
      <Markdown
        markdown={text}
        preserveLineBreaks
        compactWebLinks
        renderArtifactChip={(artifactId, context) => <ArtifactChip artifactId={artifactId} context={context} streaming />}
      />
      <Cursor />
    </div>
  );
}

function renderFinalized(
  text: string,
  _size: number,
  renderFigureSlot?: (figureId: string) => React.ReactNode | null,
  lang?: string,
) {
  // chat-ux-2026-08 batch D: **no more segmentation**.
  //
  // Previously split markdown at each `[data:art_*]` token, rendering a separate
  // `<Markdown>` (block `.v2-md`) per segment, with a chip between blocks; a sentence-ending
  // `[1]` then occupied its own line, stretching 4 list items to 8 lines (116 measured DOM:
  // alternating `div.v2-md[356x22]` / `span.bh-artifact-chip[35x20]`, all block siblings).
  // The chip itself was inline-flex; the problem was its flanking block containers, unfixable with CSS alone.
  //
  // Now the full text uses **one** `<Markdown>`; `rehypeArtifactChips` replaces tokens
  // with inline elements in place during HAST processing, then this callback renders `<ArtifactChip>`.
  // Preserve list/table/paragraph structure, with chips in their original inline locations.
  return (
    <div style={{ margin: "6px 0" }} className="lead-narrative-md" lang={lang}>
      <Markdown
        markdown={text}
        preserveLineBreaks
        compactWebLinks
        renderArtifactChip={(artifactId, context) => <ArtifactChip artifactId={artifactId} context={context} />}
        renderFigureSlot={renderFigureSlot}
      />
    </div>
  );
}

/**
 * p0-followup F4 — inline chip for `[data:art_*]` references. Renders as a
 * subtle pill carrying the artifact id (hover tooltip) with onClick that
 * scrolls the matching canvas section into view when present.
 */
// prod-bug-bash-039 P2-3: module-level dedup with 30s TTL so chip click toast
// doesn't spam on archived sessions (where canvas state is gone). Same chip
// id within 30s = silent; outside window = fresh toast (handles lazy-load).
const _toastedChipIds = new Map<string, number>();
const TOAST_DEDUP_TTL_MS = 30_000;

function ArtifactChip({ artifactId, context, streaming = false }: { artifactId: string; context?: string; streaming?: boolean }) {
  const tStep = useTranslations("step_list");
  // prod-bug-bash-039 Phase 5 (0.39.3) — resolve `cite:N` pseudo-id via the
  // turn-scoped citation registry context. For [cite:N] markers we:
  //   1. Parse N from "cite:N"
  //   2. Look up citations[N-1] for the real artifact id + label
  //   3. Render chip with the seq number visible (`[N]`) for cross-reference
  //   4. Click scrolls to the *resolved* artifact id (not the pseudo cite:N)
  //
  // Legacy [data:art_xxx] path: artifactId is the real id. 0.39.8 #2 fix —
  // also look up legacy `art_xxx` IDs in the citations registry by
  // artifactId so they get a resolved seq + label (otherwise the chip
  // rendered the literal "[数据]" placeholder forever, surfacing as
  // user-visible noise in archived turns).
  const citations = useCitations();
  // Projected aliases join exactly to this turn's sources using identity-derived aliases;
  // see comments above `ChatSourceEntry`). Accessible entries carry safe_label / safe_query /
  // safe_preview_url; clicking expands a source card rather than a "这是数据来源引用" hint.
  // ⚠Hooks must precede `return null` below (drops a chip with neither seq nor label);
  // otherwise hook calls become conditional, misaligning this component's hook order whenever a citation takes that return.
  // `resolvedId` is already resolved here, permitting a safe join.
  const chatSources = useChatSources();
  const [sourceOpen, setSourceOpen] = useState(false);
  // Put the source card at the **end** of its paragraph/list item (design 08 · B: card after paragraph), not immediately after the citation:
  // citations often sit mid-sentence, so immediate expansion splits it, pushing its final period onto a lone line below the card (observed on 116).
  // If no containing block is found, fall back to after the citation. ⚠Like the two hooks above, this must precede `return null`.
  const chipRef = useRef<HTMLSpanElement>(null);
  const [cardSlot, setCardSlot] = useState<HTMLElement | "inline" | null>(null);
  useLayoutEffect(() => {
    if (!sourceOpen) return;
    const block = chipRef.current?.closest("p, li, td, th, blockquote, h1, h2, h3, h4, h5, h6");
    if (!block) {
      setCardSlot("inline");
      return () => setCardSlot(null);
    }
    const slot = document.createElement("span");
    slot.className = "bh-chat-source-slot";
    // Citations in table cells: place the card after the whole table; inside a cell it widens the column and overflows the table horizontally
    // (measured on a 412px narrow screen).
    const table = block.matches("td, th") ? block.closest("table") : null;
    if (table) table.insertAdjacentElement("afterend", slot);
    else block.appendChild(slot);
    setCardSlot(slot);
    return () => {
      slot.remove();
      setCardSlot(null);
    };
  }, [sourceOpen]);
  let resolvedId = artifactId;
  let seq: number | null = null;
  let label: string | null = null;
  // source-accessibility batch 4: six event-surface states + chart_section anchor.
  let accessState: Citation["accessState"] | undefined;
  let safeAnchorId: string | undefined;
  let kind: Citation["kind"] | undefined;
  let kb: CitationKbRef | undefined;
  if (artifactId.startsWith("cite:")) {
    const n = Number(artifactId.slice(5));
    if (Number.isInteger(n) && n >= 1) {
      seq = n;
      const cite = citations.find((c) => c.seq === n);
      if (cite) {
        resolvedId = cite.artifactId;
        label = cite.label;
        accessState = cite.accessState;
        safeAnchorId = cite.safeAnchorId;
        kind = cite.kind;
        kb = cite.kb;
      }
    }
  } else {
    // 0.39.8 #2 — legacy `art_xxx` raw id. Try to find a registered citation
    // with matching artifactId so the chip resolves to a real seq instead of
    // falling back to "[数据]" literal text.
    const cite = citations.find((c) => c.artifactId === artifactId);
    if (cite) {
      seq = cite.seq;
      label = cite.label;
      accessState = cite.accessState;
      safeAnchorId = cite.safeAnchorId;
    }
  }
  // 0.39.8 #2 — drop chip silently when raw `art_xxx` has no citation
  // registry hit (archived pre-citation-reform turn, or LLM emitted
  // `[data:art_xxx]` for an artifact never registered). Showing the literal
  // "[数据]" placeholder polluted prose readability with no actionable
  // content. User-reported (s_c788a7a47b): "这里还是有[数据][数据]".
  // `findChatSource` is not a hook; calculate only after resolving `resolvedId`. The two hooks above
  // must run unconditionally (before the return below), so keep the two concerns separate.
  const source = findChatSource(chatSources, resolvedId);
  // KB chunks (design 08 · A1): same superscript + small document icon; accessible citations open the KB source card.
  const isKb = kind === "kb_chunk";
  const canOpenKb = isKb && accessState === "linked_accessible" && Boolean(kb);
  // Do not expand sources while the answer generates: every token reflows the body, moving cards before paragraphs are finished.
  const openable = canOpenKb || source?.access_state === "linked_accessible";
  const canOpenSource = openable && !streaming;
  if (seq === null && label === null) {
    return null;
  }
  // feedback-triage-2026-08-24 (customer reported "数据引用按钮点击后无响应"):
  // This previously only called `scrollToArtifact(resolvedId)`, searching the **currently rendered DOM** via
  // `[data-artifact-id]` / `[data-section-id]` / `[id]`. Two problems:
  //   ① With canvas closed, the target section is absent from DOM and cannot be found;
  //   ② Backend now sends viewer-safe aliases `source-ref-v1:…`, while canvas has only
  //      `data-section-id="sec_*"`; these families **never** match, a structurally inevitable failure
  //      and therefore require resolving the alias before navigation.
  // ① Has a solution: workbench already listens to `requestCanvasDataFocus`, opening the results panel
  //    and navigating there, beyond searching current DOM. Real artifact ids use this route.
  // ② Has no solution: aliases deliberately are not artifact ids; they cannot fetch data or locate sections.
  //    Opening them requires source metadata in chat scope (canvas has SourcePanel + sources[],
  //    chat has neither), a separate registered task. For now, be honest:
  //    replace "未在画布中找到" (sounds broken) with an explanation that this is a data source reference.
  const isViewerAlias = resolvedId.startsWith("source-ref-v1:");
  // Only **real artifact ids** go to workbench, which navigates to `?result=<artifactId>`.
  // Aliases, unresolved `cite:N`, and canvas sections `sec_*` are not artifact ids; passing them would navigate to
  // nonexistent results. Reuse artifact-preview-cache.ts's existing `art_` prefix criterion.
  const isArtifactId = resolvedId.startsWith("art_");
  // Batch 4: three negative states have no clickable action (§4.2); clicks show the same plain-language explanation (deduplicated toast).
  const deniedState = accessState === "linked_restricted" || accessState === "linked_missing" || accessState === "unsupported";
  const stateTooltip = accessState === "linked_restricted"
    ? tStep(isKb ? "cite_kb_restricted_tip" : "cite_state_restricted")
    : accessState === "linked_missing"
      ? tStep("cite_state_missing")
      : accessState === "unsupported"
        ? tStep("cite_state_unsupported")
        : null;
  const handleClick = () => {
    if (deniedState) {
      const lastToast = _toastedChipIds.get(resolvedId);
      const now = Date.now();
      if (!lastToast || now - lastToast > TOAST_DEDUP_TTL_MS) {
        _toastedChipIds.set(resolvedId, now);
        const text = isKb && accessState === "linked_restricted" ? tStep("cite_kb_restricted") : stateTooltip;
        toast({ kind: "info", text: text ?? tStep("cite_source_only") });
      }
      return;
    }
    if (canOpenSource) {
      setSourceOpen((open) => !open);
      return;
    }
    if (openable) return; // Generating: wait for completion before expanding.
    // Batch 4: chart_section anchors (delivered only for OWNED). Canvas rendered → scroll directly;
    // canvas closed → ask workbench to open the report, then retry scrolling with a time limit (section must mount first).
    if (safeAnchorId) {
      if (scrollToArtifact(safeAnchorId)) return;
      requestCanvasResultOpen({ type: "report" }, { entryPoint: "chat" });
      let attempts = 0;
      const retry = () => {
        if (scrollToArtifact(safeAnchorId!) || attempts >= 6) return;
        attempts += 1;
        window.setTimeout(retry, 350);
      };
      window.setTimeout(retry, 350);
      return;
    }
    if (scrollToArtifact(resolvedId)) return;
    // Canvas closed / target unrendered: ask workbench to open the results panel.
    // ⚠Do not treat `requestCanvasDataFocus`'s return as success: it is simply
    // `dispatchEvent`'s result, false only when a listener calls preventDefault;
    // it also returns true without listeners. (No repo consumer listens to the companion rejectCanvasDataFocus result event:
    // a dead channel.) Choose paths by id shape, not return value.
    if (isArtifactId) {
      requestCanvasDataFocus(resolvedId, { entryPoint: "chat" });
      return;
    }
    const lastToast = _toastedChipIds.get(resolvedId);
    const now = Date.now();
    if (!lastToast || now - lastToast > TOAST_DEDUP_TTL_MS) {
      _toastedChipIds.set(resolvedId, now);
      toast({ kind: "info", text: tStep(isViewerAlias ? "cite_source_only" : "cite_not_found") });
    }
  };
  // Replace native `title` hover details with an immediate tooltip. Batch 4 (tech-design §4.2): no state
  // uses alias/raw id as copy; remove `cite_tooltip` ("引用 N · <43-character alias>") and
  // `resolvedId` fallback in favor of six-state plain language; keep alias only in `data-artifact-ref` for investigation.
  const tooltip = label ?? stateTooltip ?? tStep("cite_source_only");
  // chat-ux-2026-08 batch D: plain numbers, no brackets.
  // Old `[N]` came from **block-sibling** rendering: chips occupied a line, and brackets made them look like references.
  // Now true superscripts sit in sentences (`vertical-align:.42em` + `.72em`); brackets are noise,
  // particularly heavy in Chinese prose. Superscript position + background convey semantics.
  const display = seq !== null ? String(seq) : tStep("cite_fallback");
  const chip = (
    <span
      ref={chipRef}
      role="button"
      tabIndex={0}
      aria-expanded={canOpenSource ? sourceOpen : undefined}
      onClick={handleClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleClick();
        }
      }}
      // chat-ux-2026-08 batch D: true inline superscript via display:inline + vertical-align, without increasing line height;
      // em font size scales with body text. Former inline styles moved to styles/ui-controls-legacy.css .legacy-cite-chip;
      // restricted/missing/unsupported sources add .is-denied (workbench/canvas batch 1).
      className={deniedState ? "bh-artifact-chip bh-tip legacy-cite-chip is-denied" : "bh-artifact-chip bh-tip legacy-cite-chip"}
      data-artifact-ref={resolvedId}
      data-cite-seq={seq !== null ? seq : undefined}
      data-tip={tooltip}
      aria-label={tooltip}
    >
      {isKb ? <KbDocIcon size="chip" /> : null}
      {display}
    </span>
  );
  if (!canOpenSource) return chip;
  // Cards are **inline blocks**, not overlays: chat bubbles lack floating-ui (canvas's
  // 709-line citation-popover is tightly coupled to useCanvasContext, unsuitable here). Inline expansion needs neither
  // positioning calculations nor risks bubble-overflow clipping. See cardSlot above for placement.
  const card = !sourceOpen ? null : canOpenKb ? (
    <KbChatSourceCard kb={kb!} title={label ?? ""} context={context ?? ""} onClose={() => setSourceOpen(false)} />
  ) : (
    <ChatSourceCard source={source!} fallbackLabel={label} onClose={() => setSourceOpen(false)} />
  );
  return (
    <>
      {chip}
      {!card || !cardSlot ? null : cardSlot === "inline" ? card : createPortal(card, cardSlot)}
    </>
  );
}
