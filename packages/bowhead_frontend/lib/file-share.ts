import { apiJson, apiVoid } from "@/lib/api-client";
import { appPath } from "@/lib/base-path";
import type { Audience } from "@/lib/content-share-api";

type FileSharePrincipal = "user" | "group";

export type FileShare = {
  subject_kind: FileSharePrincipal | "workspace" | "team";
  subject_id: string;
  subject_name?: string;
};

export type FileShares = {
  file_id: string;
  sharing_enabled: boolean;
  audience?: Audience;
  shares: FileShare[];
};

export type SharedHtml = {
  filename: string;
  mime: string;
  size: number;
  version_id: string;
  version: number;
};

function fileApi(artifactId: string) {
  return `/api/v1/files/${encodeURIComponent(artifactId)}`;
}

export function getFileShares(artifactId: string, signal?: AbortSignal) {
  return apiJson<FileShares>(`${fileApi(artifactId)}/shares?html=true`, { signal, cache: "no-store" });
}

export function addFileShare(artifactId: string, kind: FileSharePrincipal, principalId: string) {
  return apiJson(`${fileApi(artifactId)}/share`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subject_kind: kind, subject_id: principalId }),
  });
}

export function revokeFileShare(artifactId: string, kind: FileSharePrincipal, principalId: string) {
  return apiVoid(`${fileApi(artifactId)}/share/${kind}/${encodeURIComponent(principalId)}`, { method: "DELETE" });
}

export function putFileShareAudience(artifactId: string, audience: Audience) {
  return apiVoid(`${fileApi(artifactId)}/shares/audience`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ audience }),
  });
}

export function fileShareUrl(fileId: string): string {
  const path = appPath(`/files/view/?file=${encodeURIComponent(fileId)}`);
  return new URL(path, window.location.origin).href;
}

export function getSharedHtml(fileId: string, signal: AbortSignal) {
  return apiJson<SharedHtml>(`/api/v1/files/shared/${encodeURIComponent(fileId)}/view`, { signal, cache: "no-store" });
}

export function sharedHtmlContentPath(fileId: string, versionId: string) {
  return appPath(`/api/v1/files/shared/${encodeURIComponent(fileId)}/content?version_id=${encodeURIComponent(versionId)}`);
}
