"use client";

import { appPath } from "./base-path";
import type {
  ResultsRouteTarget,
  ResultsViewerMode,
} from "./results-workspace-route";
import { trackInteraction as track } from "./metrics";

export const CANVAS_FOCUS_DATA_EVENT = "bowhead:canvas-focus-data";
export const CANVAS_FOCUS_DATA_RESULT_EVENT = "bowhead:canvas-focus-data-result";
export const CANVAS_OPEN_RESULT_EVENT = "bowhead:results-open";

export interface CanvasReportReturnContext {
  label: string;
  sectionId?: string;
}

export interface CanvasFocusDataDetail {
  artifactId: string;
  entryPoint?: "chat" | "report" | "code" | "source_panel" | "unknown";
  history?: "push" | "replace";
  mode?: ResultsViewerMode;
  reportContext?: CanvasReportReturnContext;
}

export interface CanvasFocusDataResultDetail {
  artifactId: string;
  accepted: boolean;
  reason?: "report_only";
}

export interface CanvasOpenResultDetail {
  target: ResultsRouteTarget;
  entryPoint?: CanvasFocusDataDetail["entryPoint"];
  history?: CanvasFocusDataDetail["history"];
}

export function requestCanvasResultOpen(
  target: ResultsRouteTarget,
  options: Omit<CanvasOpenResultDetail, "target"> = {},
): boolean {
  if (typeof window === "undefined") return false;
  track("bowhead_canvas_data_focus_total", {
    action: "request",
    entry_point: options.entryPoint ?? "unknown",
    result_type: target.type,
  });
  return window.dispatchEvent(
    new CustomEvent<CanvasOpenResultDetail>(CANVAS_OPEN_RESULT_EVENT, {
      detail: { target, ...options },
      cancelable: true,
    }),
  );
}

export function requestCanvasDataFocus(
  artifactId: string,
  options: Omit<CanvasFocusDataDetail, "artifactId"> = {},
): boolean {
  if (typeof window === "undefined" || !artifactId) return false;
  track("bowhead_canvas_data_focus_total", {
    action: "request",
    entry_point: options.entryPoint ?? "unknown",
    result_type: "artifact",
  });
  return window.dispatchEvent(
    new CustomEvent<CanvasFocusDataDetail>(CANVAS_FOCUS_DATA_EVENT, {
      detail: { artifactId, ...options },
      cancelable: true,
    }),
  );
}

export function rejectCanvasDataFocus(
  artifactId: string,
  reason: CanvasFocusDataResultDetail["reason"],
): void {
  if (typeof window === "undefined" || !artifactId) return;
  track("bowhead_canvas_data_focus_total", {
    action: "reject",
    entry_point: "unknown",
    result_type: "artifact",
    reason: reason ?? "unknown",
  });
  window.dispatchEvent(
    new CustomEvent<CanvasFocusDataResultDetail>(CANVAS_FOCUS_DATA_RESULT_EVENT, {
      detail: { artifactId, accepted: false, reason },
    }),
  );
}

export function workspaceDataFocusHref(
  sessionId: string | null | undefined,
  artifactId: string,
): string {
  const params = new URLSearchParams();
  if (sessionId) params.set("session", sessionId);
  params.set("result", artifactId);
  params.set("mode", "preview");
  return appPath(`/?${params.toString()}`);
}

export function filesFocusHref(fileId: string): string {
  return appPath(`/files/?focus=${encodeURIComponent(fileId)}`);
}
