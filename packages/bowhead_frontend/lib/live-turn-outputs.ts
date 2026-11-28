/**
 * What a running Turn has produced so far (chat running state R2.1, R2.3), read from its steps' saved files.
 *
 * `step_done.result_meta.output_artifacts` entries carry the stored `kind`, `lineage_id` and `version_no`. A
 * `deliverable` becomes a file card as soon as it is saved; a later version of the same file (same lineage)
 * replaces that card, marked updated, and so does a new version of a file from an earlier Turn. Every other saved
 * file (sandbox outputs, query results, drafts) counts as a work file, as the finished payload's `work_files` does;
 * slide previews belong to their deck and count as nothing. Entries without a kind come from older servers and
 * show nothing. When the Turn ends, the answer's own file list replaces these cards.
 */
import type { StepOutputArtifact } from "@/components/chat/step-list/types";

import type { Step } from "./turn-store";

export interface LiveFileCard {
  /** The file's lineage (its first version's id), or its own id. */
  key: string;
  output: StepOutputArtifact;
  updated: boolean;
}

export interface LiveTurnOutputs {
  files: LiveFileCard[];
  workFiles: number;
}

const NOT_COUNTED = new Set(["deck_preview", "user_upload"]);

export function liveTurnOutputs(steps: readonly Step[] | undefined): LiveTurnOutputs {
  const files = new Map<string, LiveFileCard>();
  const work = new Set<string>();
  for (const step of steps ?? []) {
    for (const output of step.outputArtifacts ?? []) {
      if (!output.kind || NOT_COUNTED.has(output.kind)) continue;
      if (output.kind === "deliverable" && output.dataRole !== "stdout") {
        const key = output.lineageId ?? output.artifactId;
        const shown = files.get(key);
        if (shown?.output.artifactId === output.artifactId) continue;
        files.set(key, { key, output, updated: Boolean(shown) || (output.versionNo ?? 1) > 1 });
        continue;
      }
      work.add(output.artifactId);
    }
  }
  return { files: [...files.values()], workFiles: work.size };
}
