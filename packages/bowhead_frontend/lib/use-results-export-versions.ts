"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { listExportJobs } from "@/lib/api";
import {
  projectResultExportVersions,
  type ResultExportVersionGroup,
} from "@/lib/results-export-versions";

export function useResultsExportVersions(
  canvasId: string | null,
  enabled: boolean,
): {
  groups: ResultExportVersionGroup[];
  refresh: () => Promise<ResultExportVersionGroup[]>;
} {
  const [groups, setGroups] = useState<ResultExportVersionGroup[]>([]);
  const activeCanvasRef = useRef(canvasId);
  const requestGenerationRef = useRef(0);
  activeCanvasRef.current = canvasId;

  const refresh = useCallback(async (): Promise<ResultExportVersionGroup[]> => {
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    if (!enabled || !canvasId) {
      setGroups([]);
      return [];
    }
    try {
      const next = projectResultExportVersions(
        await listExportJobs(canvasId, 100),
      );
      if (
        activeCanvasRef.current === canvasId
        && requestGenerationRef.current === generation
      ) setGroups(next);
      return next;
    } catch {
      if (
        activeCanvasRef.current === canvasId
        && requestGenerationRef.current === generation
      ) setGroups([]);
      return [];
    }
  }, [canvasId, enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { groups, refresh };
}
