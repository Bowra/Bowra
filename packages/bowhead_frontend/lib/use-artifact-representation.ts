"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { appFetch } from "./base-path";
import { useOptionalAuthSession } from "./auth/auth-session-context";
import {
  previewUsesTextDto,
  type ArtifactViewerProfile,
} from "./artifact-viewer-profile";
import type { ResultsViewerMode } from "./results-workspace-route";
import {
  classifyResultsError,
  classifyResultsResponse,
  ResultsWorkspaceError,
  type ResultsWorkspaceErrorClass,
} from "./results-workspace-errors";
import type { PreviewLike } from "@/components/canvas/artifact-content-previews";

export interface ArtifactRepresentation extends PreviewLike {
  representation: ResultsViewerMode;
  revision: string;
  encoding?: string;
  returned_byte_count?: number;
  total_byte_count?: number;
  details?: {
    id: string;
    filename: string;
    mime: string;
    size: number;
    source: string;
    created_at: string;
    kind?: string;
  };
}

const CACHE_BYTES = 16 * 1024 * 1024;
class RepresentationError extends ResultsWorkspaceError {
  constructor(
    errorClass: ResultsWorkspaceErrorClass,
    readonly scopeUnavailable = false,
  ) {
    super(errorClass);
  }
}

export function representationPath(
  sessionId: string,
  artifactId: string,
  scope: "session" | "report",
  mode: ResultsViewerMode,
) {
  const id = encodeURIComponent(artifactId);
  const base =
    scope === "report"
      ? `/api/v1/canvas/by-session/${encodeURIComponent(sessionId)}/artifacts/${id}`
      : `/api/v1/files/${id}`;
  return `${base}/representations/${mode}`;
}

export async function readRepresentation(
  path: string,
  mode: ResultsViewerMode,
  signal: AbortSignal,
): Promise<ArtifactRepresentation> {
  const response = await appFetch(path, { signal, cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const code = body?.detail?.error?.code;
    const classes: Record<string, ResultsWorkspaceErrorClass> = {
      artifact_representation_unsupported: "unsupported",
      artifact_representation_too_large: "too_large",
      artifact_representation_corrupt: "corrupt",
      artifact_representation_unavailable: "unavailable",
      artifact_scope_unavailable: "unavailable",
    };
    throw new RepresentationError(
      classes[code] ?? classifyResultsResponse(response.status),
      code === "artifact_scope_unavailable" ||
        [401, 403, 404, 410].includes(response.status),
    );
  }
  const data = (await response.json()) as ArtifactRepresentation;
  if (
    data.representation !== mode ||
    typeof data.revision !== "string" ||
    !data.revision
  ) {
    throw new RepresentationError("corrupt");
  }
  return data;
}

/** Current-object cache only: three independent modes, no cross-object or persistent grants. */
export function useArtifactRepresentation({
  sessionId,
  artifactId,
  scope,
  mode,
  profile,
}: {
  sessionId: string;
  artifactId: string;
  scope: "session" | "report";
  mode: ResultsViewerMode;
  profile: ArtifactViewerProfile;
}) {
  const auth = useOptionalAuthSession();
  const principal = JSON.stringify([
    auth?.status,
    auth?.me?.id,
    auth?.me?.team_id,
    auth?.me?.auth_session_generation,
  ]);
  const scopeKey = JSON.stringify([
    principal,
    auth?.me?.capability_revision,
    scope,
    sessionId,
    artifactId,
  ]);
  const cache = useMemo(
    () =>
      new Map<
        ResultsViewerMode,
        {
          data: ArtifactRepresentation;
          metadataRevision: string;
          bytes: number;
        }
      >(),
    [scopeKey],
  );
  const [permissionEpoch, setPermissionEpoch] = useState(0);
  const key = JSON.stringify([scopeKey, permissionEpoch, mode, profile]);
  const currentKey = useRef(key);
  currentKey.current = key;
  const sequence = useRef(0);
  const [state, setState] = useState<{
    key: string;
    data: ArtifactRepresentation | null;
    error: ResultsWorkspaceErrorClass | null;
    scopeUnavailable: boolean;
  }>({ key: "", data: null, error: null, scopeUnavailable: false });
  const enabled = !auth || auth.status === "authenticated";
  const retry = useCallback(() => {
    cache.clear();
    setPermissionEpoch((epoch) => epoch + 1);
  }, [cache]);

  useEffect(() => {
    let needsRevalidation = document.visibilityState === "hidden" || !document.hasFocus();
    const onBlur = () => {
      // Focusing a child iframe blurs this window, but not the whole document.
      // Only a real page/window departure should invalidate its reading state.
      if (!document.hasFocus()) needsRevalidation = true;
    };
    const revalidate = () => {
      if (!needsRevalidation || document.visibilityState === "hidden") return;
      needsRevalidation = false; // visibility + focus can describe the same return.
      retry();
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") needsRevalidation = true;
      else revalidate();
    };
    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", revalidate);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("focus", revalidate);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [retry]);

  useEffect(() => {
    if (!enabled) {
      cache.clear();
      return;
    }
    const request = ++sequence.current;
    const controller = new AbortController();
    const live = () =>
      !controller.signal.aborted &&
      currentKey.current === key &&
      sequence.current === request;
    setState({ key, data: null, error: null, scopeUnavailable: false });
    void (async () => {
      // Every mode activation rechecks scope before showing even cached content.
      // Details never reads bytes and is also the safe metadata/revision gate for native media.
      const details = await readRepresentation(
        representationPath(sessionId, artifactId, scope, "details"),
        "details",
        controller.signal,
      );
      if (!live()) return;
      if (details.details?.id !== artifactId)
        throw new RepresentationError("corrupt");
      for (const [cachedMode, entry] of cache) {
        if (entry.metadataRevision !== details.revision)
          cache.delete(cachedMode);
      }
      const cached = cache.get(mode);
      const data =
        mode === "details" ||
        (mode === "preview" && !previewUsesTextDto(profile))
          ? details
          : (cached?.data ??
            (await readRepresentation(
              representationPath(sessionId, artifactId, scope, mode),
              mode,
              controller.signal,
            )));
      if (!live()) return;
      const bytes = JSON.stringify(data).length * 2; // Conservative decoded UTF-16 bound incl. metadata.
      cache.delete(mode);
      while (
        cache.size &&
        [...cache.values()].reduce((sum, entry) => sum + entry.bytes, bytes) >
          CACHE_BYTES
      ) {
        cache.delete(cache.keys().next().value!);
      }
      if (bytes <= CACHE_BYTES)
        cache.set(mode, { data, metadataRevision: details.revision, bytes });
      setState({ key, data, error: null, scopeUnavailable: false });
    })().catch((error: unknown) => {
      if (!live()) return;
      const scopeUnavailable =
        error instanceof RepresentationError && error.scopeUnavailable;
      if (scopeUnavailable) cache.clear();
      else cache.delete(mode);
      setState({
        key,
        data: null,
        error: classifyResultsError(error),
        scopeUnavailable,
      });
    });
    return () => controller.abort();
  }, [artifactId, cache, enabled, key, mode, profile, scope, sessionId]);

  // Mask on the first render, not one effect later (A→B→A also checks request sequence).
  const visible = enabled && state.key === key ? state : null;
  return {
    contextKey: scopeKey,
    data: visible?.data ?? null,
    error: visible?.error ?? null,
    scopeUnavailable: visible?.scopeUnavailable ?? false,
    loading: !visible || (!visible.data && !visible.error),
    retry,
  };
}
