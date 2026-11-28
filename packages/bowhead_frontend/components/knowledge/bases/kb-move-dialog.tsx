"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES): "Move to another knowledge base" (PRD §4.6, mockup ⑦; technical design §3.1 Move).
 *
 * Lists the knowledge bases you can edit, grouped My / Space / Team with their audience. Choosing one runs a dry run that
 * states the access change (how many people gain and lose access) and the files that cannot move, with the reason.
 * Confirming moves the files: originals, versions, citations, matrices and extraction results stay, and access follows
 * the new knowledge base.
 *
 * `onMoved` runs once after the move ran, and the caller closes the dialog there. When some files failed, the dialog
 * first lists them and calls `onMoved` when it is dismissed. A bottom sheet below 640 px.
 */
import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import { kbFileError, kbFileErrorFromCode } from "@/lib/kb-file-error";
import {
  directoryKnowledgeBases,
  moveKnowledgeBaseFiles,
  notifyKnowledgeBasesChanged,
  type KnowledgeBaseMoveResult,
} from "@/lib/knowledge-bases-api";

import { KnowledgeBaseTargetPicker } from "./kb-target-picker";
import { useKnowledgeBaseDirectory, useKnowledgeBaseNamer } from "./use-knowledge-bases";

export interface KbMoveDialogProps {
  files: Array<{ file_id: string; filename: string }>;
  /** The knowledge base the files are in now (marked as current in the list); null or omitted when they differ or are unknown. */
  currentKnowledgeBaseId?: string | null;
  onClose: () => void;
  onMoved: (result: { failedIds: string[]; targetId: string }) => void;
}

type Preview =
  | { state: "idle" }
  | { state: "checking"; targetId: string }
  | { state: "ready"; targetId: string; result: KnowledgeBaseMoveResult }
  | { state: "error"; targetId: string; message: string };

/** Per-file reasons: a file you may not move gets the move rule; everything else the shared knowledge-base wording. */
function useItemReason(): (code: string | undefined) => string {
  const t = useTranslations("knowledge_bases.move");
  const tKb = useTranslations("knowledge_base");
  return (code) => {
    if (code === "kb_file_access_denied" || code === "knowledge_base_level_required") return t("reason_not_yours");
    return tKb(kbFileErrorFromCode(code, "scope"));
  };
}

export function KbMoveDialog({ files, currentKnowledgeBaseId = null, onClose, onMoved }: KbMoveDialogProps) {
  const t = useTranslations("knowledge_bases.move");
  const tCommon = useTranslations("knowledge_bases.common");
  const tKb = useTranslations("knowledge_base");
  const itemReason = useItemReason();
  const nameOf = useKnowledgeBaseNamer();
  const { directory, loading, error: directoryError, refresh } = useKnowledgeBaseDirectory();
  const labelId = useId();
  const [targetId, setTargetId] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview>({ state: "idle" });
  const [moving, setMoving] = useState(false);
  const [moveError, setMoveError] = useState<string | null>(null);
  /** The move ran and some files failed: list them until the dialog is dismissed. */
  const [outcome, setOutcome] = useState<KnowledgeBaseMoveResult | null>(null);
  const requestRef = useRef(0);
  const fileIds = files.map((file) => file.file_id);
  const fileKey = fileIds.join("\n");
  const filename = (id: string) => files.find((file) => file.file_id === id)?.filename ?? "";

  const current = currentKnowledgeBaseId
    ? directoryKnowledgeBases(directory).find((kb) => kb.id === currentKnowledgeBaseId) ?? null
    : null;

  // Dry run for the chosen target; a later choice discards an earlier answer.
  useEffect(() => {
    if (!targetId) return;
    const request = ++requestRef.current;
    setPreview({ state: "checking", targetId });
    setMoveError(null);
    moveKnowledgeBaseFiles({ file_ids: fileKey.split("\n"), target_knowledge_base_id: targetId, dry_run: true })
      .then((result) => {
        if (requestRef.current === request) setPreview({ state: "ready", targetId, result });
      })
      .catch((e: unknown) => {
        if (requestRef.current === request) setPreview({ state: "error", targetId, message: tKb(kbFileError(e, "scope")) });
      });
  }, [targetId, fileKey, tKb]);

  const finish = (result: KnowledgeBaseMoveResult) => {
    onMoved({ failedIds: result.results.filter((item) => !item.ok).map((item) => item.file_id), targetId: result.target_knowledge_base_id });
  };

  const close = () => {
    if (moving) return;
    if (outcome) finish(outcome);
    else onClose();
  };

  const runMove = async () => {
    if (!targetId || preview.state !== "ready") return;
    setMoving(true);
    setMoveError(null);
    try {
      const result = await moveKnowledgeBaseFiles({ file_ids: fileIds, target_knowledge_base_id: targetId });
      notifyKnowledgeBasesChanged();
      if (result.failed > 0) setOutcome(result);
      else finish(result);
    } catch (e) {
      setMoveError(t("move_failed", { error: tKb(kbFileError(e, "scope")) }));
    } finally {
      setMoving(false);
    }
  };

  const ready = preview.state === "ready" && preview.targetId === targetId ? preview.result : null;
  const movable = ready?.movable ?? 0;
  const blocked = ready ? ready.results.filter((item) => !item.ok) : [];
  const unchanged = ready?.unchanged ?? 0;

  const description = files.length === 1
    ? current
      ? t("desc_one", { name: files[0].filename, current: nameOf(current) })
      : t("desc_one_unknown", { name: files[0].filename })
    : t("desc_many", { n: files.length });

  const footer = outcome ? (
    <Button variant="primary" onClick={() => finish(outcome)} data-testid="kb-move-done">{t("done")}</Button>
  ) : (
    <>
      <Button variant="secondary" onClick={onClose} disabled={moving}>{tCommon("cancel")}</Button>
      <Button
        variant="primary"
        onClick={() => void runMove()}
        disabled={!ready || movable === 0 || moving}
        loading={moving}
        data-testid="kb-move-confirm"
      >
        {moving ? t("moving") : files.length > 1 && movable > 0 ? t("confirm_n", { n: movable }) : t("confirm")}
      </Button>
    </>
  );

  return (
    <Dialog title={t("title")} onClose={close} busy={moving} presentation="sheet" footer={footer} width={520}>
      <div className="kb-move" data-testid="kb-move-dialog">
        {outcome ? (
          <div className="kb-move__outcome" data-testid="kb-move-outcome">
            {outcome.succeeded - outcome.unchanged > 0 ? (
              <Notice tone="success">{t("moved_n", { n: outcome.succeeded - outcome.unchanged })}</Notice>
            ) : null}
            <Notice tone="error">
              <span className="kb-move__list-title">{t("failed_title", { n: outcome.failed })}</span>
              <ul className="kb-move__list">
                {outcome.results.filter((item) => !item.ok).map((item) => (
                  <li key={item.file_id}>{t("item_reason", { name: filename(item.file_id), reason: itemReason(item.error_code) })}</li>
                ))}
              </ul>
            </Notice>
          </div>
        ) : (
          <>
            <p className="kb-move__desc">{description}</p>
            <div className="kb-move__label" id={labelId}>{t("target_label")}</div>
            {directoryError && !directory ? (
              <Notice tone="error">
                {t("directory_failed")}{" "}
                <Button variant="ghost" size="sm" onClick={refresh}>{tCommon("retry")}</Button>
              </Notice>
            ) : (
              <KnowledgeBaseTargetPicker
                directory={directory}
                loading={loading}
                value={targetId}
                onChange={setTargetId}
                purpose="move"
                excludeId={currentKnowledgeBaseId}
                labelledBy={labelId}
              />
            )}
            <div className="kb-move__preview" aria-live="polite" data-testid="kb-move-preview">
              {!targetId ? (
                directory ? <p className="kb-move__hint">{t("choose_hint")}</p> : null
              ) : preview.state === "checking" ? (
                <p className="kb-move__hint">{t("checking")}</p>
              ) : preview.state === "error" ? (
                <Notice tone="error">{preview.message}</Notice>
              ) : ready ? (
                <>
                  {movable > 0 ? (
                    <Notice tone="info">
                      <span data-testid="kb-move-access">
                        {(ready.gains ?? 0) > 0 || (ready.losses ?? 0) > 0
                          ? t("access_change", { gains: ready.gains ?? 0, losses: ready.losses ?? 0 })
                          : t("access_same")}
                      </span>
                    </Notice>
                  ) : null}
                  {unchanged > 0 ? <p className="kb-move__hint">{t("unchanged_n", { n: unchanged })}</p> : null}
                  {blocked.length > 0 ? (
                    <Notice tone="muted">
                      <span className="kb-move__list-title">{t("blocked_title", { n: blocked.length })}</span>
                      <ul className="kb-move__list" data-testid="kb-move-blocked">
                        {blocked.map((item) => (
                          <li key={item.file_id}>{t("item_reason", { name: filename(item.file_id), reason: itemReason(item.error_code) })}</li>
                        ))}
                      </ul>
                    </Notice>
                  ) : null}
                  {movable === 0 && blocked.length > 0 ? <p className="kb-move__hint">{t("nothing_movable")}</p> : null}
                  {movable > 0 ? <p className="kb-move__hint">{t("kept_note")}</p> : null}
                </>
              ) : null}
            </div>
            {moveError ? <Notice tone="error">{moveError}</Notice> : null}
          </>
        )}
      </div>
    </Dialog>
  );
}
