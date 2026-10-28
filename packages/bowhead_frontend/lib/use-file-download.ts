"use client";

import { appFetch as fetch } from "@/lib/base-path";
import { apiFetch, readApiError } from "@/lib/api-client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * chat-file-download #2 — download state machine shared by every chip that
 * offers a "save this file" affordance in chat (agent-produced deliverables
 * via `ChatInlineFile`, and now user-uploaded attachments via `AttachmentChip`
 * / `PinnedAttachmentsStrip`) and published template documents. Extracted from
 * `ChatInlineFile`'s original implementation so consumers share one strategy.
 *
 * Download strategy: small same-origin files use authenticated
 * fetch-with-progress so a rail can animate; a REAL HTTP error (!res.ok →
 * 404/410/401) goes to the error state (never a false "Downloaded"); a
 * cross-origin/CORS fetch reject or a large file falls back to a native
 * `<a download>` navigation, which streams to disk without buffering the
 * blob in JS heap.
 */

/** "done" records browser handoff, not proof of a completed disk save. */
export type FileDownloadStatus = "idle" | "downloading" | "done" | "error";

// Above this, skip the in-memory progress fetch and let the browser stream the
// download to disk natively (no JS-heap buffering, no cross-origin CORS need).
const PROGRESS_FETCH_MAX_BYTES = 16 * 1024 * 1024;
const DONE_RESET_MS = 1800;

/** Trigger a browser "Save As" for an already-fetched blob (or a URL). */
function saveBlobOrUrl(href: string, filename: string, revoke?: () => void): void {
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  if (revoke) revoke();
}

export interface UseFileDownloadArgs {
  downloadUrl: string;
  filename: string;
  mime?: string;
  sizeBytes?: number;
}

export interface UseFileDownloadResult {
  status: FileDownloadStatus;
  /** API code for finite, localized advice; never render a raw server message. */
  errorCode?: string;
  /** 0..100 while status === "downloading" (0 otherwise, unless mid-reset). */
  pct: number;
  /** Idempotent — no-op while already downloading. */
  download: () => void;
}

export function useFileDownload({ downloadUrl, filename, mime, sizeBytes }: UseFileDownloadArgs): UseFileDownloadResult {
  const [status, setStatus] = useState<FileDownloadStatus>("idle");
  const [pct, setPct] = useState(0);
  const [errorCode, setErrorCode] = useState<string>();
  // Guard against double-clicks / unmount races driving overlapping downloads.
  const busy = useRef(false);
  // Track the done→idle timer so it's cleared on unmount (no setState-after-unmount).
  const doneTimer = useRef<number | null>(null);
  const request = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      request.current?.abort();
      if (doneTimer.current !== null) window.clearTimeout(doneTimer.current);
    },
    [],
  );

  const flashDone = useCallback(() => {
    setStatus("done");
    if (doneTimer.current !== null) window.clearTimeout(doneTimer.current);
    doneTimer.current = window.setTimeout(() => {
      setStatus("idle");
      setPct(0); // reset the rail so a re-download starts from 0, not a reverse sweep
      doneTimer.current = null;
    }, DONE_RESET_MS);
  }, []);

  const download = useCallback(() => {
    if (busy.current || status === "downloading") return;
    busy.current = true;
    const controller = new AbortController();
    request.current = controller;
    if (doneTimer.current !== null) window.clearTimeout(doneTimer.current);
    setErrorCode(undefined);

    void (async () => {
      // Large file → native streaming download (no in-memory buffering, no progress).
      if ((sizeBytes ?? 0) > PROGRESS_FETCH_MAX_BYTES) {
        saveBlobOrUrl(downloadUrl, filename);
        busy.current = false;
        flashDone();
        return;
      }

      setStatus("downloading");
      setPct(0);

      let res: Response;
      try {
        // Only application-origin downloads may refresh the application session.
        const sameOrigin = new URL(downloadUrl, window.location.href).origin === window.location.origin;
        res = await (sameOrigin ? apiFetch : fetch)(downloadUrl, {
          signal: controller.signal,
        });
      } catch {
        if (controller.signal.aborted) {
          busy.current = false;
          return;
        }
        // Genuine network/CORS reject: the cross-origin body can't be read, but the
        // URL is valid (gateway 302 → presigned S3 w/ Content-Disposition), so a
        // plain navigation still downloads it. Distinct from an HTTP error (below).
        saveBlobOrUrl(downloadUrl, filename);
        busy.current = false;
        flashDone();
        return;
      }

      if (controller.signal.aborted) {
        busy.current = false;
        return;
      }
      if (!res.ok || !res.body) {
        // Real HTTP error (404 / 410 GONE / 401) — surface it; do NOT claim success.
        const error = await readApiError(res);
        busy.current = false;
        if (controller.signal.aborted) return;
        setErrorCode(error.code);
        setStatus("error");
        return;
      }

      try {
        const total = Number(res.headers.get("content-length")) || sizeBytes || 0;
        const reader = res.body.getReader();
        const chunks: Uint8Array[] = [];
        let received = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (controller.signal.aborted) return;
          if (done) break;
          if (value) {
            chunks.push(value);
            received += value.length;
            if (total > 0) setPct(Math.min(100, Math.round((received / total) * 100)));
          }
        }
        const blob = new Blob(chunks as BlobPart[], {
          type: mime || "application/octet-stream",
        });
        const url = URL.createObjectURL(blob);
        // Keep the URL alive until the browser has consumed the click navigation.
        saveBlobOrUrl(url, filename, () => {
          window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        });
        setPct(100);
        flashDone();
      } catch {
        if (!controller.signal.aborted) setStatus("error");
      } finally {
        busy.current = false;
      }
    })();
  }, [downloadUrl, filename, mime, sizeBytes, status, flashDone]);

  return { status, pct, errorCode, download };
}
