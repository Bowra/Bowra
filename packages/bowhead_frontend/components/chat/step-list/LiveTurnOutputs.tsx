"use client";

/**
 * What a running Turn has produced so far, under its running card (chat running state R2.1, R2.3): a card for
 * each delivered file as soon as it is saved (a later version updates the same card, marked updated), the report
 * card once a chapter changed, and the work-file line. When the Turn ends the answer's own cards take this place,
 * read from the final payload, so a file never shows twice.
 */
import { useMemo, useSyncExternalStore } from "react";

import { appPath } from "@/lib/base-path";
import { getCanvasSnapshot, selectTurnReportChangesKey, subscribeCanvas } from "@/lib/canvas-store";
import { fileExtensionLabel } from "@/lib/file-type-icon";
import { liveTurnOutputs, type LiveFileCard } from "@/lib/live-turn-outputs";
import { reportChangesOf } from "@/lib/report-changes";
import type { Step } from "@/lib/turn-store";

import { ChatInlineFile, type ChatInlineFileData } from "./ChatInlineFile";
import { ChatInlineReport } from "./ChatInlineReport";
import { TurnWorkFiles } from "./TurnWorkFiles";

function fileData(card: LiveFileCard): ChatInlineFileData {
  const { output } = card;
  const filename = output.filename ?? output.title ?? output.artifactId;
  return {
    id: output.artifactId,
    filename,
    fileType: fileExtensionLabel(filename) || "FILE",
    mime: output.mime,
    downloadUrl: appPath(`/api/v1/files/${encodeURIComponent(output.artifactId)}`),
    sizeBytes: output.size,
    versionNo: output.versionNo,
    lineage: output.lineageId && output.versionNo ? { root: output.lineageId, versionNo: output.versionNo } : undefined,
    updated: card.updated,
  };
}

export function LiveTurnOutputs({ steps, turnId }: { steps: readonly Step[] | undefined; turnId: string | null }) {
  const outputs = useMemo(() => liveTurnOutputs(steps), [steps]);
  const reportKey = useSyncExternalStore(
    subscribeCanvas,
    () => selectTurnReportChangesKey(getCanvasSnapshot(), turnId),
    () => "",
  );
  const report = useMemo(() => {
    if (!reportKey) return null;
    const changes = reportChangesOf({ report_changes: JSON.parse(reportKey) });
    // A chart alone may still turn out to be the chat answer's; the card waits for a chapter.
    return changes && changes.added.sections + changes.updated.sections > 0 ? changes : null;
  }, [reportKey]);
  if (outputs.files.length === 0 && !report && outputs.workFiles === 0) return null;
  return (
    <div className="chat-bubble-artifacts chat-live-outputs" data-testid="live-turn-outputs">
      {outputs.files.map((card) => <ChatInlineFile key={card.key} data={fileData(card)} />)}
      {report ? <ChatInlineReport changes={report} /> : null}
      <TurnWorkFiles count={outputs.workFiles} delivered={outputs.files.length > 0} running turnId={turnId} />
    </div>
  );
}
