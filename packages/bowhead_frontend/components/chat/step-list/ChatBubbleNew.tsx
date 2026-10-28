"use client";

import { scanFigureAnchors } from "@/lib/md";

import type { ChartSpec } from "@/lib/chart/types";

import { ChatInlineChart } from "./ChatInlineChart";
import { ChatInlineFile } from "./ChatInlineFile";
import type { ChatInlineFileData } from "./ChatInlineFile";
import { ChatInlineImage } from "./ChatInlineImage";
import type { ChatInlineImageData } from "./ChatInlineImage";
import { ChatInlineKpi } from "./ChatInlineKpi";
import type { KpiRowSectionData } from "./ChatInlineKpi";
// Consolidation (2026-09-08): chat tables no longer have a separate renderer. `ChatInlineTable` merely
// truncated to 8 rows before handing off to data-table, while `ChartTable` already collapses and offers
// "展开全部 N 行"; retaining it would leave another implementation that could diverge.
import { DataTableSection } from "@/components/canvas/sections/data-table";
import type { DataTableSectionData } from "@/components/canvas/sections/data-table";
import { LeadNarrative } from "./LeadNarrative";

/**
 * Optional inline artifact attached to a chat bubble. Discriminated
 * union mirrors the shape `turn-store` will derive from `answerPayload`
 * (Phase 1.2 only ships the renderer; emit happens in Phase 1.3).
 */
export type ChatInlineArtifact =
  | { kind: "chart"; id: string; spec: ChartSpec }
  | { kind: "kpi"; id: string; data: KpiRowSectionData }
  | { kind: "table"; id: string; data: DataTableSectionData }
  // subagent-unification P5a — a downloadable produced deliverable (any file
  // type: deck/xlsx/docx/pdf/html). `data.id === id` (the artifact id).
  | { kind: "file"; id: string; data: ChatInlineFileData }
  // multimodal-v1 — generated raster image deliverable: inline preview +
  // corner download (single component, replaces the generic file chip).
  //
  // chat-inline-figure: `id` is an artifact id (`art_*`, used by `ChatInlineImage` to open
  // Results), while `[figure:]` anchors use the canvas section namespace (`sec_*`).
  // The backend provides `section_id`; use it here as an **alias** so anchors also target image figures.
  | { kind: "image"; id: string; sectionId?: string; data: ChatInlineImageData };

export interface ChatBubbleNewProps {
  /** Streaming or finalized answer prose. */
  text: string;
  /** When true, paint the streaming caret after the last char. */
  isStreaming?: boolean;
  /** Inline artifacts rendered below the prose, in array order. */
  artifacts?: ChatInlineArtifact[];
}

/**
 * Streaming answer bubble. Renders:
 *   · LeadNarrative — paragraphs of prose, **bold** allowed, streaming caret
 *   · inline artifacts (chart / kpi / table) under the prose, in order
 *
 * Phase 1.7-B (chat-redesign-2026-05-21 tech-design §4.5): the per-bubble
 * byline is removed — `<BotByline>` mounts once at the BotTurn flow top
 * (TurnContainer level) so the Bowhead identity + time + ✓ verified pill
 * are not duplicated for every narrative/answer chunk in the same turn.
 *
 * Narrative remains pure read-only prose. M2-C lets stable file/image result
 * cards open the same object in Results while keeping download independent;
 * it does not add a generic "open canvas" action to prose.
 */
export function ChatBubbleNew({
  text,
  isStreaming = false,
  artifacts = [],
}: ChatBubbleNewProps) {
  // chat-inline-figure-2026-08 batch 2: artifacts named by `[figure:<id>]` in the body **move beside the argument**;
  // unnamed ones **still all remain at the end** (PRD §4: models under-reference rather than overproduce;
  // measured report reference rate was only 42.4%, so hiding unreferenced figures would remove over half).
  //
  // ⚠ Prescan rather than record during rendering: React render order is unreliable, and renderFigureSlot
  // side effects driving the trailing list in that same render would conflict. `scanFigureAnchors` and
  // `rehypeFigureSlots` share the anchor-line criterion: one source for inline rendering and trailing-list exclusion.
  // Look up anchors by canvas section id (`sec_*`); chart/kpi/table `id` is already a section id,
  // while image `id` is an artifact id and its section id uses the `sectionId` alias. Register both keys
  // to unify the anchor namespace across all kinds.
  const byId = new Map<string, ChatInlineArtifact>();
  for (const a of artifacts) {
    byId.set(a.id, a);
    const alias = (a as { sectionId?: string }).sectionId;
    if (alias) byId.set(alias, a);
  }
  // No prescan while streaming: the trailing pile already requires `!isStreaming`; anchors are stripped, not slotted.
  // Streaming rerenders for every token, so a body containing anchors would otherwise split the full text each time (O(n²)).
  //
  // ⚠ Track **artifacts themselves**, not ids: image figures have two keys (`art_*` primary + `sec_*` alias).
  // Tracking ids makes an anchor's `sec_*` differ from the trailing filter's `a.id` (`art_*`), rendering the same figure
  // both inline and at the end (caught by this component's tests). Object identity avoids this mismatch.
  //
  // ⚠ Deduplicate **artifacts**, not anchor strings: images have two keys
  // (`art_*` primary + `sec_*` alias); if both occur in the body, the plugin's string-id deduplication
  // treats them as distinct anchors → **renders the same figure twice** (caught by this file's matrix cases).
  // Resolve anchors to artifacts in document order; allow only the **first matching anchor id** to render,
  // treating all others (including the other key for the same artifact) as unknown → hide the anchor line without duplicate rendering.
  const referenced = new Set<ChatInlineArtifact>();
  const renderableAnchors = new Set<string>();
  if (!isStreaming) {
    for (const id of scanFigureAnchors(text)) {
      const hit = byId.get(id);
      if (!hit || referenced.has(hit)) continue;
      referenced.add(hit);
      renderableAnchors.add(id);
    }
  }
  const tail = artifacts.filter((a) => !referenced.has(a));

  return (
    <div style={{ margin: "20px 0 0" }}>
      <LeadNarrative
        text={text}
        isStreaming={isStreaming}
        size={15.5}
        renderFigureSlot={(id) => {
          // Accept only prescan-selected anchors (the first occurrence for each artifact).
          // Unknown / hallucinated / stale / duplicate artifact → null → silently hide the anchor, never display the literal.
          if (!renderableAnchors.has(id)) return null;
          const a = byId.get(id);
          return a ? <ArtifactSlot artifact={a} /> : null;
        }}
      />

      {tail.length > 0 && !isStreaming ? (
        <div className="chat-bubble-artifacts">
          {tail.map((a) => (
            <ArtifactSlot key={a.id} artifact={a} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

interface ArtifactSlotProps {
  artifact: ChatInlineArtifact;
}

function ArtifactSlot({ artifact }: ArtifactSlotProps) {
  if (artifact.kind === "chart") return <ChatInlineChart spec={artifact.spec} />;
  if (artifact.kind === "kpi") return <ChatInlineKpi data={artifact.data} />;
  if (artifact.kind === "table") return <DataTableSection data={artifact.data} surface="chat" />;
  if (artifact.kind === "file") return <ChatInlineFile data={artifact.data} />;
  if (artifact.kind === "image") return <ChatInlineImage data={artifact.data} />;
  return null;
}
