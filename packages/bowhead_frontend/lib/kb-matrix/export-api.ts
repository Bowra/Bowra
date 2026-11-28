import { appFetch as fetch, appPath } from "@/lib/base-path";

/**
 * Document matrix step 1 (S5): Excel export (`GET /api/v1/kb/matrices/{id}/export.xlsx`).
 *
 * The workbook is fetched with the Space headers (a bare `<a download>` sends none) and saved under the
 * name the server gives in `Content-Disposition`. The URL goes through `appPath()` so a deployment prefix is
 * kept and no build-time API origin is used. A refused export throws `MatrixApiError` with the server code.
 */

import { spaceHeader } from "@/lib/space-storage";

import { matrixJsonOrThrow } from "./api-error";

export function matrixExportUrl(matrixId: string): string {
  return appPath(`/api/v1/kb/matrices/${encodeURIComponent(matrixId)}/export.xlsx`);
}

/** The `filename*=UTF-8''…` (or plain `filename=`) of a Content-Disposition header, if any. */
export function dispositionFilename(header: string | null): string | null {
  if (!header) return null;
  const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      // A malformed encoding falls through to the plain name.
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : null;
}

/** Download the matrix as `.xlsx`; `fallbackName` names the file when the server gives no name. */
export async function downloadMatrixExport(matrixId: string, fallbackName: string): Promise<void> {
  const r = await fetch(matrixExportUrl(matrixId), { headers: { ...spaceHeader() } });
  if (!r.ok) await matrixJsonOrThrow(r, "exportMatrix");
  const blob = await r.blob();
  const filename = dispositionFilename(r.headers.get("content-disposition")) || `${fallbackName || "matrix"}.xlsx`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
