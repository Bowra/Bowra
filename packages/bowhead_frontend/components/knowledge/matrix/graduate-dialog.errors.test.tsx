import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MatrixApiError } from "@/lib/kb-matrix/api-error";
import type { MatrixColumnVM } from "@/lib/kb-matrix/types";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  graduateMatrix: vi.fn(),
}));

vi.mock("@/lib/kb-matrix/graduate-api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/kb-matrix/graduate-api")>();
  return {
    ...actual,
    graduateMatrix: (...args: unknown[]) => mocks.graduateMatrix(...args),
  };
});

import { GraduateDialog } from "./graduate-dialog";

const column: MatrixColumnVM = {
  columnId: "col_test",
  name: "Amount",
  type: "number",
};

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    expected: "存在重复列名：金额, 日期——请先改名再存为模板",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    expected:
      "Duplicate column names: 金额, 日期 — rename before saving",
  },
] as const;

beforeEach(() => {
  vi.clearAllMocks();
  mocks.graduateMatrix.mockRejectedValue(
    new MatrixApiError(
      422,
      "duplicate_column_names",
      "graduateMatrix: 422 RAW_BACKEND_SCHEMA_DIAGNOSTIC",
      { columns: ["金额", "日期"] },
    ),
  );
});

afterEach(cleanup);

describe.each(locales)("graduate errors — $locale", (entry) => {
  it("renders a localized reason without backend prose, code, or status", async () => {
    render(
      <NextIntlClientProvider
        locale={entry.locale}
        messages={entry.messages}
      >
        <GraduateDialog
          matrixId="mx_test"
          defaultName="Contract template"
          columns={[column]}
          filesCount={1}
          emptyCells={0}
          failedCells={0}
          staleCells={0}
          onClose={vi.fn()}
          onDone={vi.fn()}
        />
      </NextIntlClientProvider>,
    );

    fireEvent.click(screen.getByTestId("kb-matrix-graduate-submit"));

    expect(await screen.findByText(entry.expected)).toBeTruthy();
    expect(document.body.textContent).not.toContain(
      "duplicate_column_names",
    );
    expect(document.body.textContent).not.toContain("RAW_BACKEND");
    expect(document.body.textContent).not.toContain("graduateMatrix: 422");
  });
});
