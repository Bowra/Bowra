import { describe, expect, it } from "vitest";

import {
  consumeReportExportParam,
  parseReportExportParam,
  reportAutoExportReadiness,
  reportExportHrefForSession,
  reportHrefForSession,
  reportShareUrl,
} from "./report-share";

describe("report-share", () => {
  it("builds a session report href", () => {
    expect(reportHrefForSession("s 1")).toBe("/?session=s%201&report=1");
  });

  it("builds an absolute permissioned report URL", () => {
    expect(reportShareUrl("s_123", "https://bowhead.example")).toBe(
      "https://bowhead.example/?session=s_123&report=1",
    );
  });

  describe("报告库「下载」→ 工作台导出", () => {
    it("带上导出意图", () => {
      expect(reportExportHrefForSession("s_123", "xlsx")).toBe("/?session=s_123&report=1&export=xlsx");
      expect(reportExportHrefForSession("s 1", "pdf")).toBe("/?session=s%201&report=1&export=pdf");
    });

    it("★分享链接绝不能带导出意图——否则接收方一打开就自动下载", () => {
      expect(reportHrefForSession("s_123")).not.toContain("export");
      expect(reportShareUrl("s_123", "https://bowhead.example")).not.toContain("export");
    });

    it("解码只认库页菜单里真有的三种", () => {
      expect(parseReportExportParam("?export=pdf")).toBe("pdf");
      expect(parseReportExportParam("?export=PPTX")).toBe("pptx");
      expect(parseReportExportParam(new URLSearchParams("export=xlsx&session=s_1"))).toBe("xlsx");
    });

    it("非法/缺失一律 null，不触发导出", () => {
      expect(parseReportExportParam("?session=s_1")).toBeNull();
      expect(parseReportExportParam("?export=")).toBeNull();
      expect(parseReportExportParam("?export=exe")).toBeNull();
      // docx has only the legacy job path (absent from backend parity contract); deliberately exclude it from automatic export
      expect(parseReportExportParam("?export=docx")).toBeNull();
    });

    it("编码出来的 URL 必须能被自己解码回去（两边同源，不许各改一半）", () => {
      for (const fmt of ["pdf", "pptx", "xlsx"] as const) {
        const href = reportExportHrefForSession("s_round", fmt);
        expect(parseReportExportParam(href.slice(href.indexOf("?")))).toBe(fmt);
      }
    });
  });
});

describe("读走 ?export=（顺序不能写反）", () => {
  it("★ 返回格式，并把参数摘掉", () => {
    const url = new URL("https://x/?session=s_1&report=1&export=xlsx");

    expect(consumeReportExportParam(url)).toBe("xlsx");
    expect(url.searchParams.has("export")).toBe(false);
    // Do not remove any other parameters
    expect(url.searchParams.get("session")).toBe("s_1");
    expect(url.searchParams.get("report")).toBe("1");
  });

  it("★★ 这就是那个 bug：先删后读会恒返 null —— 现在读在删之前", () => {
    // Observed on 116: clicking "下载 → XLSX" in the library opens the session, with no export requests and no notice.
    // Source called delete("export") before parseReportExportParam(url.searchParams),
    // so parsing received the cleared object → always null → if (!fmt) return.
    for (const fmt of ["pdf", "pptx", "xlsx"]) {
      const url = new URL(`https://x/?export=${fmt}`);
      expect(consumeReportExportParam(url)).toBe(fmt);
    }
  });

  it("负对照：不认识的格式返回 null，但参数照样摘掉（免得刷新反复重试）", () => {
    const url = new URL("https://x/?export=docx");

    expect(consumeReportExportParam(url)).toBeNull();
    expect(url.searchParams.has("export")).toBe(false);
  });

  it("负对照：压根没有这个参数时无事发生", () => {
    const url = new URL("https://x/?session=s_1");

    expect(consumeReportExportParam(url)).toBeNull();
    expect(url.searchParams.get("session")).toBe("s_1");
  });
});

describe("reportAutoExportReadiness", () => {
  const target = new URL("https://x/?session=s_target&report=1&export=xlsx");

  it("waits until the target session canvas hydration resolves", () => {
    expect(reportAutoExportReadiness(target, null, null)).toBe("waiting");
    expect(reportAutoExportReadiness(target, null, "s_other")).toBe("waiting");
  });

  it("distinguishes a hydrated empty report from a renderable report", () => {
    expect(reportAutoExportReadiness(target, null, "s_target")).toBe("empty");
    expect(reportAutoExportReadiness(target, "cv_1", "s_target")).toBe("ready");
  });

  it("does nothing when the URL has no automatic export intent", () => {
    expect(
      reportAutoExportReadiness(
        new URL("https://x/?session=s_target&report=1"),
        "cv_1",
        "s_target",
      ),
    ).toBe("none");
  });
});
