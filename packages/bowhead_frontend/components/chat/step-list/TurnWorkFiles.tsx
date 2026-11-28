"use client";

/**
 * The quiet line under a Turn's outputs (chat running state R2.3): how many work files the Turn saved besides what
 * it delivered. Work files never open by themselves; the line opens Results listing only this Turn's files.
 *
 *   · Finished, with delivered files: "本轮还保存了 2 个工作文件".
 *   · Finished, without: "本轮没有交付文件 · 保存了 3 个工作文件".
 *   · Running: "已保存 3 个工作文件" (no verdict before the Turn has decided what it delivers).
 */
import { useTranslations } from "next-intl";
import { FolderOpen } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { requestResultsTurnFiles } from "@/lib/canvas-workspace-focus";
import { useResultsWorkspaceCapabilitySnapshot } from "@/lib/use-results-workspace-capability";

export function TurnWorkFiles({
  count,
  delivered,
  running = false,
  turnId = null,
}: {
  count: number;
  delivered: boolean;
  running?: boolean;
  turnId?: string | null;
}) {
  const t = useTranslations("attachment");
  const resultsWorkspace = useResultsWorkspaceCapabilitySnapshot();
  if (count <= 0) return null;
  const text = running && !delivered
    ? t("work_files_line_live", { count })
    : delivered
      ? t("work_files_line", { count })
      : t("work_files_line_only", { count });
  const content = (
    <>
      <LucideGlyph icon={FolderOpen} size={13} aria-hidden="true" />
      <span>{text}</span>
    </>
  );
  if (!turnId || !resultsWorkspace) {
    return <p className="chat-work-files" data-testid="turn-work-files">{content}</p>;
  }
  return (
    <p className="chat-work-files" data-testid="turn-work-files">
      <button
        type="button"
        className="chat-work-files__open"
        aria-label={t("work_files_open", { text })}
        onClick={() => requestResultsTurnFiles(turnId)}
      >
        {content}
      </button>
    </p>
  );
}
