// external-capability-onboarding §3.1 — fetch the curated connector-preset catalog when the
// CreateToolCard opens. Entitlement-filtered server-side; [] on error / unlicensed → the picker
// simply omits itself (byte-identical card). Shared by both CreateToolCard mounts.
"use client";

import { useEffect, useState } from "react";

import type { ConnectorPreset } from "@/components/canvas/sections/preset-directory";
import { listConnectorPresets } from "./team-tool-sources-api";

export function useConnectorPresets(active: boolean): ConnectorPreset[] {
  const [presets, setPresets] = useState<ConnectorPreset[]>([]);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    listConnectorPresets()
      .then((p) => {
        if (alive) setPresets(p);
      })
      .catch(() => {
        // presets are a convenience — never block the manual entry path
        if (alive) setPresets([]);
      });
    return () => {
      alive = false;
    };
  }, [active]);
  return presets;
}
