"use client";

import { useEffect, useState, type RefObject } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Download } from "lucide-react";

import { CharIcon } from "@/components/char-icon";
import { fmtSize } from "@/components/chat/step-list/ChatInlineFile";
import { DialogShell } from "@/components/ui/dialog";
import { appPath } from "@/lib/base-path";
import { formatDateTime } from "@/lib/locale";
import { listResultVersions, type ResultFileVersionDTO } from "@/lib/results-api";

/**
 * File versions (file-versions design R2): the live versions of one file, newest first, each downloadable.
 * Same dialog pattern as the report exports list.
 */
export function FileVersionsDrawer({
  sessionId,
  artifactId,
  open,
  onClose,
  returnFocusRef,
}: {
  sessionId: string | null;
  artifactId: string | null;
  open: boolean;
  onClose: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const t = useTranslations("results_workspace");
  const locale = useLocale();
  const [versions, setVersions] = useState<ResultFileVersionDTO[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!open || !sessionId || !artifactId) return;
    const controller = new AbortController();
    setState("loading");
    listResultVersions({ sessionId, artifactId, signal: controller.signal })
      .then((rows) => {
        setVersions(rows);
        setState("ready");
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setVersions([]);
        setState("failed");
      });
    return () => controller.abort();
  }, [open, sessionId, artifactId, attempt]);

  if (!open) return null;

  return (
    <DialogShell
      presentation="center"
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      label={t("file_versions_title")}
      backdropClassName="rc-drawer-scrim"
      panelClassName="rc-exports-drawer"
      panelProps={{ "data-testid": "file-versions-drawer" } as Record<string, string>}
    >
      <div className="rc-exports-head">
        <span className="rc-exports-title">{t("file_versions_title")}</span>
        <span className="rc-exports-scope">{t("file_versions_scope")}</span>
        <button type="button" className="rc-export-toast-x" onClick={onClose} aria-label={t("file_versions_close")}>
          <CharIcon ch="✕" />
        </button>
      </div>
      <div className="rc-exports-list bh-scroll" aria-busy={state === "loading" || undefined}>
        {state === "loading" ? <div className="rc-exports-empty">…</div> : null}
        {state === "failed" ? (
          <div className="rc-exports-empty" role="alert">
            {t("file_versions_failed")}{" "}
            <button type="button" className="rc-pill-btn" onClick={() => setAttempt((value) => value + 1)}>
              {t("file_versions_retry")}
            </button>
          </div>
        ) : null}
        {state === "ready" && versions.length === 0 ? <div className="rc-exports-empty">{t("file_versions_empty")}</div> : null}
        {state === "ready"
          ? versions.map((version) => {
              const label = t("file_version_label", { number: version.version_no });
              const meta = [
                version.latest ? t("file_version_current") : null,
                fmtSize(version.size) || null,
                formatDateTime(version.created_at, locale, { dateStyle: "medium", timeStyle: "short" }) || null,
              ].filter(Boolean).join(" · ");
              return (
                <div key={version.artifact_id} className="rc-exports-item" data-status="done" data-testid="file-version-row">
                  <div className="rc-exports-item-body">
                    <div className="rc-exports-item-name" title={version.filename}>
                      {label} · {version.filename}
                    </div>
                    <div className="rc-exports-item-meta">{meta}</div>
                  </div>
                  <a
                    className="rc-exports-dl"
                    href={appPath(`/api/v1/files/${encodeURIComponent(version.artifact_id)}`)}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={t("file_version_download", { number: version.version_no })}
                    aria-label={t("file_version_download", { number: version.version_no })}
                  >
                    <CharIcon ch="↓" icon={Download} />
                  </a>
                </div>
              );
            })
          : null}
      </div>
    </DialogShell>
  );
}
