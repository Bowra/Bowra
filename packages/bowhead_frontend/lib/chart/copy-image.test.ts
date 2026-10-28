/**
 * Batch 2 §3.4 · Copy as Image capability checks and fallback.
 * Production 116 uses plain HTTP; this path must actually fall back, never fail silently.
 */

import { describe, expect, it, vi } from "vitest";

import { canCopyImage, copyCanvasToClipboard, type ClipboardHost } from "./copy-image";

function secureHost(write = vi.fn().mockResolvedValue(undefined)): ClipboardHost {
  class FakeClipboardItem {
    constructor(public items: Record<string, Blob>) {}
  }
  return {
    isSecureContext: true,
    ClipboardItem: FakeClipboardItem,
    navigator: { clipboard: { write } },
  };
}

function canvasWith(blob: Blob | null): HTMLCanvasElement {
  return {
    toBlob: (cb: (b: Blob | null) => void) => cb(blob),
  } as unknown as HTMLCanvasElement;
}

describe("canCopyImage", () => {
  it("安全上下文 + ClipboardItem + clipboard.write → 可用", () => {
    expect(canCopyImage(secureHost())).toBe(true);
  });

  it("★裸 HTTP（isSecureContext=false）→ 不可用（116 生产就是这个）", () => {
    expect(canCopyImage({ ...secureHost(), isSecureContext: false })).toBe(false);
  });

  it("没有 ClipboardItem 构造器（老浏览器）→ 不可用", () => {
    expect(canCopyImage({ ...secureHost(), ClipboardItem: undefined })).toBe(false);
  });

  it("有 clipboard 但没有 write（只读实现）→ 不可用", () => {
    expect(canCopyImage({ ...secureHost(), navigator: { clipboard: {} } })).toBe(false);
  });

  it("完全没有 navigator → 不可用，且不抛", () => {
    expect(() => canCopyImage({ isSecureContext: true })).not.toThrow();
    expect(canCopyImage({ isSecureContext: true })).toBe(false);
  });
});

describe("copyCanvasToClipboard", () => {
  it("正常路径：写进剪贴板", async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    const result = await copyCanvasToClipboard(canvasWith(new Blob(["x"])), secureHost(write));
    expect(result).toEqual({ ok: true });
    expect(write).toHaveBeenCalledTimes(1);
  });

  it("★能力不足 → reason=unsupported（调用方据此降级成下载 PNG）", async () => {
    const result = await copyCanvasToClipboard(canvasWith(new Blob(["x"])), {
      ...secureHost(),
      isSecureContext: false,
    });
    expect(result).toEqual({ ok: false, reason: "unsupported" });
  });

  it("toBlob 给不出 blob → reason=blob_failed，不抛", async () => {
    const result = await copyCanvasToClipboard(canvasWith(null), secureHost());
    expect(result).toEqual({ ok: false, reason: "blob_failed" });
  });

  it("★写入被拒（权限/失焦）→ reason=write_failed，不抛（用户看得到提示）", async () => {
    const write = vi.fn().mockRejectedValue(new Error("NotAllowedError"));
    const result = await copyCanvasToClipboard(canvasWith(new Blob(["x"])), secureHost(write));
    expect(result).toEqual({ ok: false, reason: "write_failed" });
  });

  it("toBlob 自身抛（老 WebView）→ 归到 blob_failed，不连累调用方", async () => {
    const canvas = {
      toBlob: () => {
        throw new Error("not implemented");
      },
    } as unknown as HTMLCanvasElement;
    await expect(copyCanvasToClipboard(canvas, secureHost())).resolves.toEqual({
      ok: false,
      reason: "blob_failed",
    });
  });
});
