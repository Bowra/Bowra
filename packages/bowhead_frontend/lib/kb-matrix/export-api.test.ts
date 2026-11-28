/**
 * Document matrix step 1 (S5): the Excel export download keeps the deployment prefix, sends the Space headers,
 * saves under the server's file name, and throws the server's code when refused.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.__BOWHEAD_BASE_PATH__ = undefined;
  window.sessionStorage.clear();
  window.localStorage.clear();
});

async function loadWithBasePath(prefix: string) {
  vi.resetModules();
  window.__BOWHEAD_BASE_PATH__ = prefix;
  return import("./export-api");
}

describe("matrix export download", () => {
  it.each([
    ["", "/api/v1/kb/matrices/mx%201/export.xlsx"],
    ["/bowhead", "/bowhead/api/v1/kb/matrices/mx%201/export.xlsx"],
  ])("builds the export URL under the deployment prefix %j", async (prefix, url) => {
    const { matrixExportUrl } = await loadWithBasePath(prefix);
    expect(matrixExportUrl("mx 1")).toBe(url);
  });

  it("fetches through the prefix with the Space headers and saves under the server's name", async () => {
    const { downloadMatrixExport } = await loadWithBasePath("/bowhead");
    window.localStorage.setItem("bowhead.space_id", "ws_a");
    const fetchMock = vi.fn(
      async () =>
        new Response(new Blob(["xlsx"]), {
          status: 200,
          headers: { "content-disposition": "attachment; filename*=UTF-8''%E4%BE%9B%E5%BA%94%E5%95%86_%E5%90%88%E5%90%8C.xlsx" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const createObjectURL = vi.fn(() => "blob:matrix");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL }));
    const saved: { href: string; download: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ href: this.getAttribute("href") ?? "", download: this.download });
    });

    await downloadMatrixExport("mx_1", "Contracts");

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/bowhead/api/v1/kb/matrices/mx_1/export.xlsx");
    const headers = new Headers(init.headers);
    expect(headers.get("X-Space-Id")).toBe("ws_a");
    expect(headers.get("X-Bowhead-Public-Base")).toBe(`${window.location.origin}/bowhead`);
    expect(saved).toEqual([{ href: "blob:matrix", download: "供应商_合同.xlsx" }]);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:matrix");
  });

  it("falls back to the matrix name when the server gives none", async () => {
    const { downloadMatrixExport } = await loadWithBasePath("");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Blob(["xlsx"]), { status: 200 })));
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => undefined }));
    const names: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      names.push(this.download);
    });
    await downloadMatrixExport("mx_1", "Contracts");
    expect(names).toEqual(["Contracts.xlsx"]);
  });

  it("throws the server's code when the export is refused", async () => {
    const { downloadMatrixExport } = await loadWithBasePath("");
    // The module graph was reset, so compare against the class the reloaded client throws.
    const { MatrixApiError } = await import("./api-error");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: { error: { code: "matrix_not_found" } } }), { status: 404 })),
    );
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click");
    const err = await downloadMatrixExport("mx_gone", "x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(MatrixApiError);
    expect(err).toMatchObject({ code: "matrix_not_found", status: 404 });
    expect(click).not.toHaveBeenCalled();
  });

  it("reads extended and plain file names from Content-Disposition", async () => {
    const { dispositionFilename } = await loadWithBasePath("");
    expect(dispositionFilename("attachment; filename*=UTF-8''a%20b.xlsx")).toBe("a b.xlsx");
    expect(dispositionFilename('attachment; filename="plain.xlsx"')).toBe("plain.xlsx");
    expect(dispositionFilename("attachment; filename*=UTF-8''%E0%A4%A.xlsx; filename=\"ok.xlsx\"")).toBe("ok.xlsx");
    expect(dispositionFilename("attachment")).toBeNull();
    expect(dispositionFilename(null)).toBeNull();
  });
});
