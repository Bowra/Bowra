"use client";

import { PRODUCT_NAME } from "@/lib/product-identity";

import { useEffect, useState } from "react";

import { appFetch } from "@/lib/base-path";

/**
 * Application version shown to users (account menu version line, Help Center "About").
 *
 * The web bundle version is baked in at build time from `BOWHEAD_VERSION`
 * (`NEXT_PUBLIC_BOWHEAD_VERSION`, the same source the former sidebar version row used); the
 * server version comes from the public `/health` payload. They normally match: a mismatch means
 * the browser still runs an older bundle, which is exactly what support needs to know. No commit
 * hash is baked into the image today, so the copied details carry the web bundle's locale build
 * instead.
 */

export interface AppVersionInfo {
  /** Web bundle version (`dev` outside image builds). */
  web: string;
  /** Locale-specific static build this bundle came from (`zh-CN`, `en-US`, ...), when known. */
  build: string | null;
  /** Server version from `/health`; null until loaded or when unavailable. */
  server: string | null;
}

export function webBuildVersion(): string {
  return process.env.NEXT_PUBLIC_BOWHEAD_VERSION?.trim() || "dev";
}

export function webBuildLocale(): string | null {
  return process.env.NEXT_PUBLIC_BUILD_LOCALE?.trim() || null;
}

/** Short visible label: "<application name> <version>". */
export function versionLabel(applicationName: string, version = webBuildVersion()): string {
  return `${applicationName.trim() || PRODUCT_NAME} ${version}`;
}

/**
 * Plain-text details copied for support. English and line-oriented on purpose (like the error
 * diagnostics block): it is pasted into tickets and read by the vendor, whatever the UI language.
 */
export function formatVersionDetails(
  applicationName: string,
  info: AppVersionInfo,
  separator = "\n",
): string {
  const name = applicationName.trim() || PRODUCT_NAME;
  const lines = [name === PRODUCT_NAME ? `${PRODUCT_NAME} ${info.web}` : `${name} ${info.web} (${PRODUCT_NAME})`];
  lines.push(`Web: ${info.web}${info.build ? ` (${info.build} build)` : ""}`);
  lines.push(`Server: ${info.server ?? "unknown"}`);
  return lines.join(separator);
}

/** Single-line form of the details for the manual-copy prompt (a prompt input cannot hold line breaks). */
export function formatVersionDetailsInline(applicationName: string, info: AppVersionInfo): string {
  return formatVersionDetails(applicationName, info, " · ");
}

let serverVersionCache: string | null = null;
let serverVersionRequest: Promise<string | null> | null = null;

/** Server version from `/health`, fetched at most once per page load (failures are retried on the next call). */
export function loadServerVersion(): Promise<string | null> {
  if (serverVersionCache !== null) return Promise.resolve(serverVersionCache);
  if (serverVersionRequest) return serverVersionRequest;
  serverVersionRequest = appFetch("/health", { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) return null;
      const body = (await response.json()) as { version?: unknown };
      const version = typeof body.version === "string" ? body.version.trim() : "";
      if (version) serverVersionCache = version;
      return version || null;
    })
    .catch(() => null)
    .finally(() => {
      serverVersionRequest = null;
    });
  return serverVersionRequest;
}

export function cachedServerVersion(): string | null {
  return serverVersionCache;
}

/** Test hook: forget the cached server version. */
export function resetServerVersionCacheForTests(): void {
  serverVersionCache = null;
  serverVersionRequest = null;
}

/**
 * Version details; the server part loads lazily while `enabled` is true. `serverUnavailable` turns true
 * when the last `/health` read failed (the next enable retries).
 */
export function useAppVersionInfo(enabled = true): AppVersionInfo & { serverUnavailable: boolean } {
  const [server, setServer] = useState<string | null>(() => cachedServerVersion());
  const [serverUnavailable, setServerUnavailable] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void loadServerVersion().then((value) => {
      if (cancelled) return;
      if (value) setServer(value);
      setServerUnavailable(!value);
    });
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return { web: webBuildVersion(), build: webBuildLocale(), server, serverUnavailable };
}
