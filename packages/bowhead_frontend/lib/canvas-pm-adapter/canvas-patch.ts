/**
 * Canvas PATCH client — call backend PATCH /api/v1/canvas/{cid}/sections/{sid}.
 *
 * Backend endpoint (shipped, verified against P3.1 design):
 *   PATCH /api/v1/canvas/{cid}/sections/{sid}
 *   body: { patch: <partial section>, expected_rev: <int> }
 *   200 → { rev: <new rev> }
 *   409 → { current_rev: <server rev> } (revision conflict)
 *   404/500 → generic error
 */

import { apiFetch, HttpStatusError } from "@/lib/api-client";
import { reportSaveFailureMessage } from "@/lib/report-save-errors";
import type { ReportSection } from "@/lib/types";

export type PatchResult =
  | { ok: true; rev: number }
  | { ok: false; status: "conflict"; currentRev: number }
  | { ok: false; status: "not_found" }
  /** message is a localized user-facing sentence; error is the raw failure (status code, request ID), for Copy Diagnostics only. */
  | { ok: false; status: "error"; message: string; error?: unknown };

export async function patchCanvasSection(
  canvasId: string,
  sectionId: string,
  patch: Partial<ReportSection>,
  expectedRev?: number,
): Promise<PatchResult> {
  try {
    // Feishu 07.06 #12: this autosave PATCH fires on debounced typing during editing. Previously used bare fetch,
    // producing 401 after the 1h access token expired, so users reported "一打字就报报告保存失败". apiFetch
    // (=fetchWithRefresh) automatically calls /refresh and retries once on 401; 401 after renewal means actual logout.
    const res = await apiFetch(
      `/api/v1/canvas/${encodeURIComponent(canvasId)}/sections/${encodeURIComponent(sectionId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          patch,
          ...(expectedRev !== undefined ? { expected_rev: expectedRev } : {}),
        }),
      },
    );

    if (res.status === 200) {
      const data = await res.json();
      return { ok: true, rev: data.rev };
    }
    if (res.status === 409) {
      const data = await res.json().catch(() => ({}));
      return { ok: false, status: "conflict", currentRev: data.current_rev ?? -1 };
    }
    if (res.status === 404) {
      return { ok: false, status: "not_found" };
    }
    const error = new HttpStatusError("patch section", res);
    return {
      ok: false,
      status: "error",
      message: reportSaveFailureMessage(error),
      error,
    };
  } catch (e) {
    return {
      ok: false,
      status: "error",
      message: reportSaveFailureMessage(e),
      error: e,
    };
  }
}
