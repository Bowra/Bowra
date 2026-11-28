"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES, PRD Q5): delete a named knowledge base. The confirmation names the
 * file count; the files leave knowledge through today's "Remove from knowledge base" and their originals stay in
 * Files. The server compares the count: when files were added or removed meanwhile it answers 409
 * `knowledge_base_files_changed` with the current count, which this dialog shows for a second confirmation.
 * Default knowledge bases are never offered here.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Notice } from "@/components/ui/notice";
import { deleteKnowledgeBase, notifyKnowledgeBasesChanged, type KnowledgeBaseDTO } from "@/lib/knowledge-bases-api";
import { notify } from "@/lib/use-toast";

import { filesChangedCount, knowledgeBaseErrorText } from "./kb-directory-shared";
import { useKnowledgeBaseNamer } from "./use-knowledge-bases";

export function KbDeleteDialog({
  kb,
  onClose,
  onDeleted,
}: {
  kb: KnowledgeBaseDTO;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const t = useTranslations("knowledge_bases.delete");
  const tKb = useTranslations("knowledge_base");
  const name = useKnowledgeBaseNamer()(kb);
  const [fileCount, setFileCount] = useState(kb.file_count);
  const [countChanged, setCountChanged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteKnowledgeBase(kb.id, { file_count: fileCount, revision: kb.revision });
      notifyKnowledgeBasesChanged();
      notify("success", t("deleted_toast", { name }));
      onDeleted();
    } catch (e) {
      const current = filesChangedCount(e);
      if (current !== null) {
        // Confirm again with the count the person has now seen.
        setFileCount(current);
        setCountChanged(true);
      } else {
        setError(knowledgeBaseErrorText(e, tKb, t("failed")));
      }
      // Counts or revisions may be stale elsewhere on the page too.
      notifyKnowledgeBasesChanged();
      setBusy(false);
    }
  };

  return (
    <ConfirmDialog
      title={t("title", { name })}
      description={(
        <>
          <span className="kbd-delete-files" data-testid="kb-delete-files">{t("files", { n: fileCount })}</span>{" "}
          {t("consequence")}
        </>
      )}
      confirmLabel={busy ? t("deleting") : t("confirm")}
      busy={busy}
      onCancel={onClose}
      onConfirm={() => void confirm()}
    >
      {countChanged ? (
        <Notice tone="info" className="kbd-dialog-notice">
          <span data-testid="kb-delete-count-changed">{t("files_changed", { n: fileCount })}</span>
        </Notice>
      ) : null}
      {error ? (
        <Notice tone="error" className="kbd-dialog-notice">
          {error}
        </Notice>
      ) : null}
    </ConfirmDialog>
  );
}
