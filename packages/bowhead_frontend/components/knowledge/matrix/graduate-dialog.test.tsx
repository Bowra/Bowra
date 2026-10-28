import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MatrixColumnVM } from "@/lib/kb-matrix/types";
import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({ graduateMatrix: vi.fn() }));

vi.mock("@/lib/kb-matrix/graduate-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/graduate-api")>();
  return { ...actual, graduateMatrix: (...args: unknown[]) => mocks.graduateMatrix(...args) };
});

import { GraduateDialog } from "./graduate-dialog";

const column: MatrixColumnVM = { columnId: "col_test", name: "Amount", type: "number" };

function renderDialog() {
  const onClose = vi.fn();
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <GraduateDialog
        matrixId="mx_test"
        defaultName="Contract template"
        columns={[column]}
        filesCount={1}
        emptyCells={0}
        failedCells={0}
        staleCells={0}
        onClose={onClose}
        onDone={vi.fn()}
      />
    </NextIntlClientProvider>,
  );
  return onClose;
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("GraduateDialog · 模式单选", () => {
  it("方向键与 Home/End 移动并选中，Tab 只停在选中项", async () => {
    renderDialog();
    const snapshot = await screen.findByTestId("kb-matrix-graduate-mode-snapshot");
    const template = screen.getByTestId("kb-matrix-graduate-mode-template");
    expect(snapshot.getAttribute("aria-checked")).toBe("true");
    expect(snapshot.tabIndex).toBe(0);
    expect(template.tabIndex).toBe(-1);

    snapshot.focus();
    fireEvent.keyDown(snapshot, { key: "ArrowDown" });
    expect(document.activeElement).toBe(template);
    expect(template.getAttribute("aria-checked")).toBe("true");
    expect(template.className).toContain("on");
    expect(screen.getByTestId("kb-matrix-graduate-submit").textContent).toContain("存为模板");

    fireEvent.keyDown(template, { key: "Home" });
    expect(document.activeElement).toBe(snapshot);
    expect(snapshot.getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(snapshot, { key: "End" });
    expect(template.getAttribute("aria-checked")).toBe("true");
  });
});

describe("GraduateDialog · 关闭", () => {
  it("Esc 关闭弹窗", async () => {
    const onClose = renderDialog();
    await screen.findByTestId("kb-matrix-graduate-modal");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("提交进行中 Esc 与点遮罩都不关闭", async () => {
    mocks.graduateMatrix.mockReturnValue(new Promise(() => undefined));
    const onClose = renderDialog();
    const panel = await screen.findByTestId("kb-matrix-graduate-modal");
    await act(async () => {
      fireEvent.click(screen.getByTestId("kb-matrix-graduate-submit"));
    });
    expect(mocks.graduateMatrix).toHaveBeenCalledTimes(1);
    // Mode radios use busy during submission: arrow keys do not change selection, and focus stays on the card (not body)
    const snapshot = screen.getByTestId("kb-matrix-graduate-mode-snapshot");
    snapshot.focus();
    fireEvent.keyDown(snapshot, { key: "ArrowDown" });
    expect(snapshot.getAttribute("aria-checked")).toBe("true");
    expect(screen.getByTestId("kb-matrix-graduate-mode-template")).not.toHaveProperty("disabled", true);
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.click(panel.parentElement as HTMLElement);
    expect(onClose).not.toHaveBeenCalled();
  });
});
