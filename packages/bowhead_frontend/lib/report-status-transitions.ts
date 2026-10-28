import type { ReportStatus } from "@/lib/api";

/**
 * Manual report status transitions from the stored state: mark/clear reviewed, archive/restore. Shared by the status
 * chip menu (non-default states) and the report row ⋯ (the default `active` state shows no chip).
 */
export function reportStatusTransitions(stored: string): { key: string; to: ReportStatus }[] {
  if (stored === "archived") return [{ key: "restore", to: "active" }];
  const out: { key: string; to: ReportStatus }[] =
    stored === "verified"
      ? [{ key: "unverify", to: "active" }]
      : [{ key: "verify", to: "verified" }];
  out.push({ key: "archive", to: "archived" });
  return out;
}
