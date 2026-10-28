import { isNetworkFailure } from "@/lib/transport-error";
/**
 * Workbench report-header Clear Report / Delete Report failures → user-facing sentence keys under `canvas.*`.
 * Select only by status code; never show raw text such as “clear canvas: 500”. Status codes and request IDs go into Copy Diagnostics.
 */
export type ReportHeaderActionFailedKey =
  | "hdr_clear_failed_plain"
  | "hdr_delete_failed_plain"
  | "hdr_action_forbidden"
  | "hdr_action_gone"
  | "hdr_action_network";

export function reportHeaderActionFailedKey(
  action: "clear" | "delete",
  error: unknown,
): ReportHeaderActionFailedKey {
  const status =
    error && typeof error === "object" && typeof (error as { status?: unknown }).status === "number"
      ? (error as { status: number }).status
      : undefined;
  if (status === 403) return "hdr_action_forbidden";
  if (status === 404 || status === 410) return "hdr_action_gone";
  if (status === undefined && isNetworkFailure(error)) return "hdr_action_network";
  return action === "clear" ? "hdr_clear_failed_plain" : "hdr_delete_failed_plain";
}
