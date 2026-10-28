"use client";

import { useEffect, useState } from "react";

import { appFetch } from "@/lib/base-path";

const DEV_OVERRIDE_KEY = "bowhead.feature.error_diagnostics";

function readDevelopmentOverride(): boolean | null {
  if (
    process.env.NODE_ENV === "production" ||
    typeof window === "undefined"
  ) {
    return null;
  }
  try {
    const value = window.localStorage?.getItem?.(DEV_OVERRIDE_KEY) ?? null;
    if (value === "1" || value === "true") return true;
    if (value === "0" || value === "false") return false;
  } catch {
    // Storage may be disabled. Fall through to the runtime capability.
  }
  return null;
}

export function useErrorDiagnosticsCapability(
  forcedValue?: boolean,
): boolean {
  const [enabled, setEnabled] = useState(forcedValue ?? false);

  useEffect(() => {
    if (forcedValue !== undefined) {
      setEnabled(forcedValue);
      return;
    }
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
          feature_flags?: { error_diagnostics?: unknown };
        };
        if (!controller.signal.aborted) {
          setEnabled(body.feature_flags?.error_diagnostics === true);
        }
      })
      .catch(() => {
        // Fail closed: the main localized error remains visible without details.
      });
    return () => controller.abort();
  }, [forcedValue]);

  return enabled;
}
