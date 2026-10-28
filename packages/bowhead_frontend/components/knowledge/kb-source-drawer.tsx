"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { getKbFile, type KbFileDTO } from "@/lib/kb-api";
import { toast } from "@/lib/use-toast";

import { KbFileDrawer } from "./kb-file-drawer";

/**
 * View a knowledge base file in place on the current page, with its cited excerpt pinned first and highlighted.
 *
 * Knowledge base navigation discovery #1 (2026-09-27): View Original in document matrices previously switched to Files, leaving the matrix,
 * with mismatched background scope, no source positioning, and a closed side panel upon return. Overlay the same file-details drawer above the matrix instead;
 * closing leaves the matrix and side panel intact. Read-only viewing: no scope changes / binding or other management actions (perform those in Files).
 */
export function KbSourceDrawer({
  fileId,
  chunkId,
  highlight,
  onClose,
}: {
  fileId: string;
  chunkId?: string | null;
  highlight?: string;
  onClose: () => void;
}) {
  const t = useTranslations("kbMatrix");
  const [file, setFile] = useState<KbFileDTO | null>(null);

  useEffect(() => {
    let cancelled = false;
    getKbFile(fileId)
      .then((f) => {
        if (!cancelled) setFile(f);
      })
      .catch(() => {
        if (cancelled) return;
        toast({ kind: "info", text: t("view_source_failed") });
        onClose();
      });
    return () => {
      cancelled = true;
    };
    // The caller recreates onClose each render; exclude it from dependencies (fetch once per file)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileId, t]);

  if (!file) return null;
  return (
    <KbFileDrawer
      file={file}
      onClose={onClose}
      hitChunk={
        chunkId
          ? {
              chunk_id: chunkId,
              content: "",
              file_id: file.file_id,
              file_name: file.filename,
              page: null,
              section_title: null,
              role: "",
              score: 0,
              scope: file.scope,
            }
          : null
      }
      hitQuery={highlight ?? ""}
      hitSource="matrix"
    />
  );
}
