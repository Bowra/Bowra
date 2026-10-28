import { describe, expect, it } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { SettingsExport } from "./export";

describe("report export guidance", () => {
  it("explains template availability and distinguishes report snapshots from document PDFs", () => {
    const view = renderIntl(<SettingsExport />);
    expect(view.getByRole("heading", { name: "数据导出" })).toBeTruthy();
    expect(view.getByText(/按模板生成文档尚未开放/)).toBeTruthy();
    expect(view.getByText(/此前在设置中上传的文件会保留/)).toBeTruthy();
    expect(view.getByText(/报告 PDF 是报告快照/)).toBeTruthy();
  });
});
