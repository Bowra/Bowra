import { track } from "./metrics";

export type ResultsMetricAction =
  | "pane_opened"
  | "navigator_opened"
  | "result_selected"
  | "viewer_mode_changed"
  | "open_failed"
  | "downloaded"
  | "referenced"
  | "unsupported_feedback"
  | "cross_link_opened"
  | "navigator_pinned"
  | "navigator_unpinned";

export type ResultsMetricErrorClass =
  | "unavailable"
  | "unsupported"
  | "too_large"
  | "corrupt"
  | "network"
  | "unknown"
  | "none";

export type ResultsMetricEntryPoint =
  | "header"
  | "navigator"
  | "chat"
  | "report"
  | "code"
  | "source_panel"
  | "deep_link"
  | "unknown";

export function trackResultsWorkspace(event: {
  action: ResultsMetricAction;
  resultType?: "report" | "artifact" | "code" | "none";
  group?: "deliverables" | "evidence" | "none";
  entryPoint?: ResultsMetricEntryPoint;
  mode?: "preview" | "raw" | "details" | "none";
  errorClass?: ResultsMetricErrorClass;
}): void {
  track("bowhead_results_workspace_total", {
    action: event.action,
    result_type: event.resultType ?? "none",
    group: event.group ?? "none",
    entry_point: event.entryPoint ?? "unknown",
    mode: event.mode ?? "none",
    error_class: event.errorClass ?? "none",
  });
}
