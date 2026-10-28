"use client";

import { useEffect, useState } from "react";

import { appFetch } from "@/lib/base-path";

const DEV_OVERRIDE_KEY = "bowhead.feature.session_archive";

function readDevelopmentOverride(): boolean | null {
  if (
    process.env.NODE_ENV === "production" ||
    typeof window === "undefined"
  ) {
    return null;
  }
  let value: string | null = null;
  try {
    value = window.localStorage?.getItem?.(DEV_OVERRIDE_KEY) ?? null;
  } catch {
    return null;
  }
  if (value === "1" || value === "true") return true;
  if (value === "0" || value === "false") return false;
  return null;
}

export function useSessionArchiveCapability(): boolean {
  const [enabled, setEnabled] = useState(false);

  useEffect(() => {
    const developmentOverride = readDevelopmentOverride();
    if (developmentOverride !== null) {
      setEnabled(developmentOverride);
      return;
    }
    const controller = new AbortController();
    void appFetch("/health", {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) return;
        const body = (await response.json()) as {
          feature_flags?: { session_archive?: unknown };
        };
        if (!controller.signal.aborted) {
          setEnabled(
            body.feature_flags?.session_archive === true,
          );
        }
      })
      .catch(() => {
        // Fail closed: a health outage must not reveal an unsupported action.
      });
    return () => controller.abort();
  }, []);

  return enabled;
}
