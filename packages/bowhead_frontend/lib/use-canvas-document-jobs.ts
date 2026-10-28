"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiJson } from "./api-client";
import { parseDocumentJob } from "./document-draft-client";
import { documentCanvasScopeKey, type DocumentCanvasScope } from "./document-canvas-navigation";
import type { DocumentJob } from "./document-drafts";
import { spaceHeader } from "./space-storage";

export const documentJobActive = (job: Pick<DocumentJob, "state">) =>
  ["queued", "preparing", "planning", "rendering"].includes(job.state);
export function documentPublicationKey(job: DocumentJob): string | null {
  const source = job.draft?.files["candidate.pptx"];
  const pdf = job.draft?.files["candidate.pdf"];
  return job.state === "published" && job.draft_digest && source && pdf
    ? JSON.stringify([
        job.id,
        job.origin_turn_id,
        job.draft_digest,
        source.artifact_id,
        source.sha256,
        pdf.artifact_id,
        pdf.sha256,
      ])
    : null;
}
type PageState = {
  scope: string | null;
  jobs: DocumentJob[];
  loading: boolean;
  error: boolean;
  denied: boolean;
  next: string | null;
  revision: number;
};
const empty = (scope: string | null): PageState => ({
  scope,
  jobs: [],
  loading: false,
  error: false,
  denied: false,
  next: null,
  revision: 0,
});

/** One scoped, bounded page reader owns discovery and refresh; content adapters do not poll. */
export function useCanvasDocumentJobs(scope: DocumentCanvasScope | null, running: boolean, hasActiveViews: boolean) {
  const key = documentCanvasScopeKey(scope);
  const [page, setPage] = useState(() => empty(key));
  const [cursors, setCursors] = useState<string[]>([""]);
  const [refresh, setRefresh] = useState(0);
  const scopeRef = useRef(key);
  scopeRef.current = key;
  const current = useRef(page);
  current.current = page;
  const cursor = page.scope === key ? (cursors.at(-1) ?? "") : "";
  const epoch = useRef(0);
  useEffect(() => {
    setCursors([""]);
    setPage(empty(key));
  }, [key]);
  useEffect(() => {
    if (!scope) return;
    const original = ++epoch.current;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let busy = false;
    const isCurrent = () => !controller.signal.aborted && epoch.current === original && scopeRef.current === key;
    const schedule = (delay: number) => {
      clearTimeout(timer);
      timer = setTimeout(() => void read(), delay);
    };
    async function read() {
      if (!isCurrent() || busy) return;
      if (document.hidden) {
        schedule(5000);
        return;
      }
      busy = true;
      setPage((previous) => ({ ...(previous.scope === key ? previous : empty(key)), loading: true }));
      try {
        const response = await apiJson<{ jobs: unknown[]; next_cursor?: string | null }>(
          `/api/v1/sessions/${encodeURIComponent(scope!.sessionId)}/documents${cursor ? `?after_id=${encodeURIComponent(cursor)}` : ""}`,
          { signal: controller.signal, headers: spaceHeader(scope!), cache: "no-store" },
        );
        if (!isCurrent()) return;
        if (!Array.isArray(response.jobs) || response.jobs.length > 100) throw new Error("Invalid document page");
        const jobs = response.jobs.map((job) => {
          const id = (job as { id: unknown })?.id;
          if (typeof id !== "string" || !id.trim() || id.length > 256) throw new Error("Invalid document identity");
          return parseDocumentJob(job, id);
        });
        if (new Set(jobs.map((job) => job.id)).size !== jobs.length) throw new Error("Duplicate document identity");
        const next = response.next_cursor ?? null;
        if (next !== null && (typeof next !== "string" || !next.trim() || next.length > 256 || cursors.includes(next)))
          throw new Error("Invalid document page cursor");
        setPage((previous) => ({
          scope: key,
          jobs,
          next,
          loading: false,
          error: false,
          denied: false,
          revision: previous.revision + 1,
        }));
        failures = 0;
        if (running || hasActiveViews || jobs.some(documentJobActive)) schedule(2000);
        else if (jobs.some((job) => job.usage?.settlement_pending)) schedule(5000);
      } catch (error) {
        if (!isCurrent()) return;
        const denied = error instanceof ApiError && [401, 403, 404].includes(error.status);
        setPage((previous) => ({
          ...(denied || previous.scope !== key ? empty(key) : previous),
          loading: false,
          error: true,
          denied,
        }));
        if (!denied && ++failures < 3) schedule(5000);
      } finally {
        busy = false;
      }
    }
    void read();
    const visible = () => {
      if (!document.hidden) {
        clearTimeout(timer);
        void read();
      }
    };
    window.addEventListener("focus", visible);
    document.addEventListener("visibilitychange", visible);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener("focus", visible);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [key, cursor, running, hasActiveViews, refresh]);
  const retry = useCallback(() => setRefresh((value) => value + 1), []);
  const forget = useCallback((jobId: string) => {
    setPage((previous) =>
      previous.jobs.some((job) => job.id === jobId)
        ? { ...previous, jobs: previous.jobs.filter((job) => job.id !== jobId) }
        : previous,
    );
  }, []);
  const nextPage = useCallback(() => {
    const value = current.current;
    if (value.scope !== scopeRef.current || value.loading || !value.next) return;
    setCursors((previous) => [...previous, value.next!]);
    setPage((previous) => ({ ...previous, jobs: [], next: null, loading: true }));
  }, []);
  const previousPage = useCallback(() => {
    setCursors((previous) => (previous.length > 1 ? previous.slice(0, -1) : previous));
    setPage((previous) => ({ ...previous, jobs: [], next: null, loading: true }));
  }, []);
  return {
    ...(page.scope === key ? page : empty(key)),
    retry,
    forget,
    nextPage,
    previousPage,
    hasPrevious: cursors.length > 1,
  };
}
