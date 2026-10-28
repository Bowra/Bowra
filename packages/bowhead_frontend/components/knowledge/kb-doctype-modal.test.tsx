import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({ listKnowledgeTables: vi.fn(), bulkBindKbFilesDocType: vi.fn() }));

vi.mock("@/lib/kb-knowledge-tables-api", () => ({
  listKnowledgeTables: (...args: unknown[]) => mocks.listKnowledgeTables(...args),
}));
vi.mock("@/lib/kb-api", () => ({
  bulkBindKbFilesDocType: (...args: unknown[]) => mocks.bulkBindKbFilesDocType(...args),
}));

import { KbDocTypeModal } from "./kb-doctype-modal";

function renderModal(fileIds: string[] = ["kbf_1"]) {
  const onClose = vi.fn();
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <KbDocTypeModal fileIds={fileIds} workspaceId="ws_1" onClose={onClose} onApplied={vi.fn()} toast={vi.fn()} />
    </NextIntlClientProvider>,
  );
  return onClose;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listKnowledgeTables.mockResolvedValue([{ doc_type_id: "dt_1", name: "合同", kind: "manual", cardinality: "one" }]);
});

afterEach(cleanup);

describe("KbDocTypeModal · 类型单选", () => {
  it("方向键与 Home/End 移动并选中，Tab 只停一处", async () => {
    mocks.listKnowledgeTables.mockResolvedValue([
      { doc_type_id: "dt_1", name: "合同", kind: "manual", cardinality: "one" },
      { doc_type_id: "dt_2", name: "发票", kind: "manual", cardinality: "many" },
    ]);
    renderModal();
    const first = await screen.findByTestId("kb-doctype-option-dt_1");
    const second = screen.getByTestId("kb-doctype-option-dt_2");
    const unbind = screen.getByTestId("kb-doctype-option-unbind");
    expect(screen.getByRole("radiogroup")).toBeTruthy();
    expect([first, second, unbind].map((el) => el.tabIndex)).toEqual([0, -1, -1]);

    first.focus();
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(document.activeElement).toBe(second);
    expect(second.getAttribute("aria-checked")).toBe("true");
    expect(second.className).toContain("on");
    expect((screen.getByRole("button", { name: messagesZh.knowledge_base.doctype_modal.apply }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.keyDown(second, { key: "End" });
    expect(unbind.getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(unbind, { key: "Home" });
    expect(document.activeElement).toBe(first);
    expect(first.getAttribute("aria-checked")).toBe("true");
    expect([first, second, unbind].map((el) => el.tabIndex)).toEqual([0, -1, -1]);
  });

  it("应用中用 busy：方向键不改选，焦点留在卡片上", async () => {
    mocks.bulkBindKbFilesDocType.mockReturnValue(new Promise(() => undefined));
    renderModal();
    const first = await screen.findByTestId("kb-doctype-option-dt_1");
    fireEvent.click(first);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: messagesZh.knowledge_base.doctype_modal.apply }));
    });
    expect(mocks.bulkBindKbFilesDocType).toHaveBeenCalledTimes(1);
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(first.getAttribute("aria-checked")).toBe("true");
    expect(screen.getByTestId("kb-doctype-option-unbind").getAttribute("aria-checked")).toBe("false");
    expect(screen.getByRole("radiogroup").getAttribute("aria-busy")).toBe("true");
    expect((first as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("KbDocTypeModal · 标题", () => {
  it("单个文件与批量文件的标题各自成句", async () => {
    renderModal();
    expect(await screen.findByRole("dialog", { name: "把这份文件提取到哪张表？" })).toBeTruthy();
    cleanup();
    renderModal(["kbf_1", "kbf_2", "kbf_3"]);
    expect(await screen.findByRole("dialog", { name: "把选中的 3 个文件提取到哪张表？" })).toBeTruthy();
    expect(screen.getByText("不提取到表")).toBeTruthy();
  });
});

describe("KbDocTypeModal · 关闭", () => {
  it("Esc 关闭弹窗", async () => {
    const onClose = renderModal();
    await screen.findByText("合同");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("应用进行中 Esc 不关闭", async () => {
    mocks.bulkBindKbFilesDocType.mockReturnValue(new Promise(() => undefined));
    const onClose = renderModal();
    fireEvent.click(await screen.findByText("合同"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: messagesZh.knowledge_base.doctype_modal.apply }));
    });
    expect(mocks.bulkBindKbFilesDocType).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
});
