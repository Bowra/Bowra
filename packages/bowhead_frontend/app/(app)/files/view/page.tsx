"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { ArtifactPreview } from "@/components/canvas/artifact-preview";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { getSharedHtml, sharedHtmlContentPath, type SharedHtml } from "@/lib/file-share";
import { formatByteSize } from "@/lib/format";
import type { ResultCapability } from "@/lib/results-workspace-model";
import { useResultsCapabilities } from "@/lib/use-results-capabilities";
import { withHttpFailureReason } from "@/lib/user-facing-error";

/**
 * A file shared with the person is listed nowhere else, so this page is where it gets pinned (result pins acceptance
 * F4). Its "⋯" pins the version shown: the link's lineage root may name a pruned first version.
 */
const SHARED_FILE_CAPABILITIES: ReadonlySet<ResultCapability> = new Set<ResultCapability>(["download", "pin_result"]);

export default function SharedFilePage() {
  return <Suspense fallback={null}><SharedFileRoute /></Suspense>;
}

function SharedFileRoute() {
  const fileId = useSearchParams().get("file")?.trim();
  const { status, me } = useAuthRedirect();
  useResultsCapabilities();
  const t = useTranslations("file_share");

  if (status === "error") return <main className="shared-html-page" role="alert">{t("load_failed")}</main>;
  if (status !== "authed" || !me) return <main className="shared-html-page" role="status">{t("loading")}</main>;
  if (!fileId) return <main className="shared-html-page" role="alert">{t("unavailable")}</main>;
  return <SharedFileViewer key={JSON.stringify([fileId, me.id, me.team_id, me.auth_session_generation])} fileId={fileId} />;
}

function SharedFileViewer({ fileId }: { fileId: string }) {
  const t = useTranslations("file_share");
  const [file, setFile] = useState<SharedHtml | null>(null);
  const [failure, setFailure] = useState<Error | null>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => {
    setFile(null);
    setFailure(null);
    setRevision((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void getSharedHtml(fileId, controller.signal).then((value) => {
      if (!controller.signal.aborted) setFile(value);
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setFailure(cause instanceof Error ? cause : new Error());
    });
    return () => controller.abort();
  }, [fileId, revision]);

  if (failure) {
    const unavailable = failure instanceof ApiError && failure.status === 404;
    return <main className="shared-html-page">
      <p role="alert">{unavailable ? t("unavailable") : withHttpFailureReason(t("load_failed"), failure, { read: true })}</p>
      {unavailable ? null : <Button onClick={refresh}>{t("retry")}</Button>}
    </main>;
  }
  if (!file) return <main className="shared-html-page" role="status">{t("loading")}</main>;

  const href = sharedHtmlContentPath(fileId, file.version_id);
  return <main className="shared-html-page result-file-viewer">
    <ArtifactPreview key={file.version_id}
      fileId={file.version_id}
      filename={file.filename}
      mime={file.mime}
      sizeBytes={file.size}
      sizeLabel={formatByteSize(file.size)}
      downloadHref={href}
      contentHref={href}
      capabilities={SHARED_FILE_CAPABILITIES}
      resultRole="deliverable"
      preview={null}
      onContentVersionChanged={refresh}
    />
  </main>;
}
