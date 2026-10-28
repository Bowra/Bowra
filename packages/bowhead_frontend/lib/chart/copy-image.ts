/**
 * Batch 2 §3.4: copy as image, with secure-context fallback.
 *
 * ⚠ **116 production uses plain HTTP** (direct IP access to an internal host); navigator.clipboard and
 * ClipboardItem exist only in secure contexts. Without a check, clicking does nothing and logs
 * an error, leaving users unaware; this sprint targets such silent failures.
 * Without capability, callers fall back to "下载 PNG" and explain why (tech-design §3.4).
 *
 * Pure functions + explicit host injection let tests construct all three environments (secure context/plain HTTP/write denied)
 * directly; capability checks in component effects are untestable (batch 3 matchMedia lesson).
 */

export interface ClipboardHost {
  isSecureContext?: boolean;
  ClipboardItem?: unknown;
  navigator?: { clipboard?: { write?: (items: unknown[]) => Promise<void> } };
}

export type CopyImageResult =
  | { ok: true }
  | { ok: false; reason: "unsupported" | "blob_failed" | "write_failed" };

function resolveHost(host?: ClipboardHost): ClipboardHost | null {
  if (host) return host;
  if (typeof window === "undefined") return null;
  return window as unknown as ClipboardHost;
}

/** Can the current environment write images to the clipboard? The entry button uses this to show "复制为图片" or a fallback. */
export function canCopyImage(host?: ClipboardHost): boolean {
  const h = resolveHost(host);
  if (!h) return false;
  return Boolean(h.isSecureContext) && typeof h.ClipboardItem === "function" && typeof h.navigator?.clipboard?.write === "function";
}

/** canvas → clipboard. **Never throws**; callers use reason to decide the notice. */
export async function copyCanvasToClipboard(
  canvas: HTMLCanvasElement,
  host?: ClipboardHost,
): Promise<CopyImageResult> {
  const h = resolveHost(host);
  if (!h || !canCopyImage(h)) return { ok: false, reason: "unsupported" };

  const blob = await new Promise<Blob | null>((resolve) => {
    try {
      canvas.toBlob((b) => resolve(b), "image/png");
    } catch {
      resolve(null);
    }
  });
  if (!blob) return { ok: false, reason: "blob_failed" };

  try {
    const ItemCtor = h.ClipboardItem as new (items: Record<string, Blob>) => unknown;
    await h.navigator!.clipboard!.write!([new ItemCtor({ "image/png": blob })]);
    return { ok: true };
  } catch {
    // Permission denied / document unfocused: show a notice rather than fail silently
    return { ok: false, reason: "write_failed" };
  }
}
