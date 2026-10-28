import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MatrixApiError } from "@/lib/kb-matrix/api-error";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  createMatrix: vi.fn(),
}));

vi.mock("@/lib/kb-matrix/run-api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/kb-matrix/run-api")>();
  return {
    ...actual,
    createMatrix: (...args: unknown[]) => mocks.createMatrix(...args),
  };
});

import { NewMatrixFlow } from "./new-matrix-flow";

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    expected: "个人文件不能加入空间矩阵。请先将文件移动到当前空间后重试。",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    expected:
      "Personal files cannot be added to a space matrix. Move them into the current space and try again.",
  },
] as const;

function Harness({ entry }: { entry: (typeof locales)[number] }) {
  const [error, setError] = useState("");
  return (
    <NextIntlClientProvider locale={entry.locale} messages={entry.messages}>
      <NewMatrixFlow
        workspaceId="ws_test"
        preselectedFileIds={["file_test"]}
        onCancel={vi.fn()}
        onCreated={vi.fn()}
        onError={setError}
      />
      <output>{error}</output>
    </NextIntlClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.createMatrix.mockRejectedValue(
    new MatrixApiError(
      422,
      "personal_files_not_allowed",
      "createMatrix: 422 RAW_BACKEND_FILE_DIAGNOSTIC",
      {},
    ),
  );
});

afterEach(cleanup);

describe.each(locales)("new matrix errors — $locale", (entry) => {
  it("renders a localized reason without backend prose, code, or status", async () => {
    render(<Harness entry={entry} />);
    fireEvent.click(screen.getByRole("button", { name: /创建|Create/ }));

    expect(await screen.findByText(entry.expected)).toBeTruthy();
    expect(document.body.textContent).not.toContain(
      "personal_files_not_allowed",
    );
    expect(document.body.textContent).not.toContain("RAW_BACKEND");
    expect(document.body.textContent).not.toContain("createMatrix: 422");
  });
});
