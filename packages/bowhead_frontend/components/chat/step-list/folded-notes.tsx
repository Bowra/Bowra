"use client";

/**
 * folded-notes — Lead's interim text inside the running view (chat running state PRD R1.9, spike §6.3).
 *
 * Lead writes several text segments in a Turn; most are written between steps ("我先查一下各门店的数据…").
 * They used to stream below the band and then vanish when the Turn ended (the `narrative_superseded` flag hid
 * every segment in 95% of such Turns), including drafts the reader had already read. Now a segment that a
 * later step follows folds into the card as a Bowhead note at its place in time, live and after the Turn:
 *
 *   · The open segment (no step after it yet) still streams below the card; the last segment is the answer.
 *   · A segment whose next step is `review_delivery` is labelled "初稿" (a draft written before the check).
 *   · A note that repeats the answer shows "已被下面的回答替换" instead of its text, so the reader never sees
 *     the same words twice. Overlap uses the answer dedup's own test (TurnContainer).
 *
 * Built from beats alone, so live, reloaded and replayed Turns agree.
 */
import { createContext, useContext, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { MessageSquareText as LucideMessage } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import type { TurnBeat } from "@/lib/turn-store";

import { LeadNarrative, stripInternalMarkers } from "./LeadNarrative";
import type { StepView } from "./types";

export interface FoldedNote {
  /** Beat position + segment id: segment ids can repeat after plan, lite or draft beats. */
  key: string;
  text: string;
  /** The step that follows the note; the note sits right before it. */
  beforeStepId: string;
  /** Followed by Lead's delivery check: a draft of the answer. */
  draft: boolean;
  /** Repeats the answer, which is shown below. */
  replaced: boolean;
}

export interface FoldResult {
  notes: FoldedNote[];
  /** The narrative beats that are now notes; the text flow leaves them out. */
  folded: ReadonlySet<TurnBeat>;
}

const EMPTY_FOLD: FoldResult = { notes: [], folded: new Set() };

export interface FoldAnswer {
  /**
   * The Turn's final answer exists (the Turn ended). While a Turn runs the runner's text accumulates every Lead
   * segment, so it is no answer to compare against; the answer dedup waits for the end for the same reason.
   */
  hasAnswer: boolean;
  /** The backend withdrew Lead's narrative (`narrative_superseded`): a draft was replaced by the answer. */
  superseded: boolean;
  /** Whether a segment's text repeats the answer (the answer dedup's own test). */
  repeatsAnswer: (beat: Extract<TurnBeat, { kind: "narrative" }>) => boolean;
}

/** Lead text segments followed by a later step become notes. */
export function foldNarrative(beats: readonly TurnBeat[], steps: readonly StepView[], answer: FoldAnswer): FoldResult {
  const toolOf = new Map(steps.map((step) => [step.stepId, step.toolName]));
  const notes: FoldedNote[] = [];
  const folded = new Set<TurnBeat>();
  for (let i = 0; i < beats.length; i += 1) {
    const beat = beats[i];
    if (beat.kind !== "narrative" || beat.text.trim() === "") continue;
    let next: Extract<TurnBeat, { kind: "step" }> | null = null;
    for (let j = i + 1; j < beats.length; j += 1) {
      const candidate = beats[j];
      if (candidate.kind === "step") {
        next = candidate;
        break;
      }
    }
    if (!next) continue;
    const draft = toolOf.get(next.stepId) === "review_delivery";
    // Lead writing again after its delivery check is the rewrite that replaces the draft, before the Turn ends.
    const nextIndex = beats.indexOf(next);
    const rewritten = draft && beats.some((later, j) => j > nextIndex && later.kind === "narrative" && later.text.trim() !== "");
    notes.push({
      key: `${i}:${beat.segId}`,
      text: beat.text,
      beforeStepId: next.stepId,
      draft,
      replaced: rewritten || (answer.hasAnswer && ((draft && answer.superseded) || answer.repeatsAnswer(beat))),
    });
    folded.add(beat);
  }
  return notes.length > 0 ? { notes, folded } : EMPTY_FOLD;
}

const FoldedNotesContext = createContext<readonly FoldedNote[]>([]);

export function FoldedNotesProvider({ notes, children }: { notes: readonly FoldedNote[]; children: ReactNode }) {
  return <FoldedNotesContext.Provider value={notes}>{children}</FoldedNotesContext.Provider>;
}

export function useFoldedNotes(): readonly FoldedNote[] {
  return useContext(FoldedNotesContext);
}

/** The note's first readable line, for the collapsed row. */
function firstLine(text: string): string {
  const plain = stripInternalMarkers(text).replace(/[*_`#>]+/g, "").trim();
  return plain.split(/\n+/).map((line) => line.trim()).find(Boolean) ?? "";
}

/** One folded note: "Bowhead" is the group or block it sits in; the row shows its first line and opens in place. */
export function FoldedNoteRow({ note }: { note: FoldedNote }) {
  const t = useTranslations("step_list");
  const [open, setOpen] = useState(false);
  const text = note.replaced ? t("note_replaced") : firstLine(note.text);
  const label = note.draft ? `${t("note_draft")} · ${text}` : text;
  return (
    <div className="bh-note" data-row-kind="note" data-note-replaced={note.replaced ? "1" : undefined}>
      <div className="bh-note__row">
        <span className="bh-note__glyph" aria-hidden>
          <LucideGlyph icon={LucideMessage} size={13} aria-hidden />
        </span>
        <span className="bh-note__lab">{label}</span>
        <button type="button" className="bh-note__toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          {open ? t("note_collapse") : t("note_expand")}
        </button>
      </div>
      {open ? (
        <div className="bh-note__body">
          <LeadNarrative text={note.text} isStreaming={false} size={13} />
        </div>
      ) : null}
    </div>
  );
}
