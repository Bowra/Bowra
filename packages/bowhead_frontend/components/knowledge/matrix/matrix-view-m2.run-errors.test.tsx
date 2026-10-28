import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastContainer } from "@/components/chat/toast-container";
import { MatrixApiError } from "@/lib/kb-matrix/api-error";
import { setMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixRunVM, MatrixSnapshot } from "@/lib/kb-matrix/types";
import messagesEn from "@/messages/en-US.json";
import { _clearAllToasts } from "@/lib/use-toast";
import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  estimateRun: vi.fn(),
  cancelRun: vi.fn(),
  addColumn: vi.fn(),
  reload: vi.fn(),
  unavailable: null as (() => void) | null,
}));

vi.mock("@/lib/kb-matrix/run-api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/kb-matrix/run-api")>();
  return {
    ...actual,
    estimateRun: (...args: unknown[]) => mocks.estimateRun(...args),
    cancelRun: (...args: unknown[]) => mocks.cancelRun(...args),
    addColumn: (...args: unknown[]) => mocks.addColumn(...args),
  };
});

vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({
  useIsMobile: () => false,
}));

vi.mock("@/lib/kb-matrix/use-matrix-poll", () => ({
  useMatrixPolling: (_id: string, _snapshot: unknown, onUnavailable: () => void) => {
    mocks.unavailable = onUnavailable;
    return { reload: mocks.reload };
  },
}));

vi.mock("./matrix-grid", () => ({
  MatrixGrid: () => <div data-testid="matrix-grid" />,
}));

vi.mock("./add-column-panel", () => ({
  AddColumnPanel: ({
    onCreate,
  }: {
    onCreate: (
      column: { name: string; type: string; question: string },
      run: boolean,
    ) => void;
  }) => (
    <button
      type="button"
      data-testid="submit-column"
      onClick={() =>
        onCreate(
          { name: "Status", type: "select", question: "Status?" },
          false,
        )
      }
    >
      submit column
    </button>
  ),
}));

vi.mock("./run-banner", () => ({
  RunBanners: ({
    runs,
    onCancel,
  }: {
    runs: MatrixRunVM[];
    onCancel: (runId: string) => void;
  }) =>
    runs[0] ? (
      <button
        type="button"
        data-testid="cancel-run"
        onClick={() => onCancel(runs[0].runId)}
      >
        cancel
      </button>
    ) : null,
}));

import { MatrixViewM2 } from "./matrix-view-m2";

const baseSnapshot: MatrixSnapshot = {
  matrixId: "mx_test",
  title: "Contract matrix",
  readOnly: false,
  columns: [],
  rows: [],
  seq: 1,
  canEdit: true,
  runs: [],
};

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    serviceUnavailable:
      "矩阵运行服务暂不可用，请稍后重试；如问题持续，请联系系统管理员。",
    cancelForbidden:
      "只有运行发起者、空间管理员或团队管理员可以取消该运行。",
    optionsRequired: "单选或多选列至少需要一个选项。",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    serviceUnavailable:
      "The matrix run service is temporarily unavailable. Try again later, or contact your system administrator if the problem continues.",
    cancelForbidden:
      "Only the person who started the run, a space administrator, or a team administrator can cancel it.",
    optionsRequired: "Select and multi-select columns need at least one option.",
  },
] as const;

function renderLocalized(
  entry: (typeof locales)[number],
  snapshot: MatrixSnapshot,
) {
  mocks.reload.mockImplementation(async () => {
    setMatrixSnapshot(snapshot);
  });
  return render(
    <NextIntlClientProvider
      locale={entry.locale}
      messages={entry.messages}
    >
      <MatrixViewM2 matrixId="mx_test" onBack={vi.fn()} />
      <ToastContainer />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  _clearAllToasts();
  setMatrixSnapshot(null);
});

describe.each(locales)("matrix run errors — $locale", (entry) => {
  it("localizes estimate failures without backend prose, code, or status", async () => {
    mocks.estimateRun.mockRejectedValue(
      new MatrixApiError(
        503,
        "matrix_service_unavailable",
        "estimateRun: 503 RAW_BACKEND_ENGINE_DIAGNOSTIC",
        {},
      ),
    );

    renderLocalized(entry, baseSnapshot);
    fireEvent.click(await screen.findByTestId("kb-matrix-run"));

    expect(await screen.findByText(entry.serviceUnavailable)).toBeTruthy();
    expect(document.body.textContent).not.toContain(
      "matrix_service_unavailable",
    );
    expect(document.body.textContent).not.toContain("RAW_BACKEND");
    expect(document.body.textContent).not.toContain("estimateRun: 503");
  });

  it("localizes cancel authorization failures without backend prose", async () => {
    const running: MatrixRunVM = {
      runId: "mxrun_test",
      status: "running",
      cellsTotal: 1,
      cellsDone: 0,
      cellsFailed: 0,
      skippedEdited: 0,
      etaS: 30,
      initiatedBy: "u_other",
      canCancel: true,
    };
    mocks.cancelRun.mockRejectedValue(
      new MatrixApiError(
        403,
        "owner_or_admin_required",
        "cancelRun: 403 RAW_BACKEND_OWNER_DIAGNOSTIC",
        {},
      ),
    );

    renderLocalized(entry, { ...baseSnapshot, runs: [running] });
    fireEvent.click(await screen.findByTestId("cancel-run"));

    expect(await screen.findByText(entry.cancelForbidden)).toBeTruthy();
    expect(document.body.textContent).not.toContain(
      "owner_or_admin_required",
    );
    expect(document.body.textContent).not.toContain("RAW_BACKEND");
    expect(document.body.textContent).not.toContain("cancelRun: 403");
  });

  it("localizes column editing failures without backend prose", async () => {
    mocks.addColumn.mockRejectedValue(
      new MatrixApiError(
        422,
        "options_required",
        "addColumn: 422 RAW_BACKEND_COLUMN_DIAGNOSTIC",
        {},
      ),
    );

    renderLocalized(entry, baseSnapshot);
    fireEvent.click(await screen.findByTestId("kb-matrix-addcol-btn"));
    fireEvent.click(await screen.findByTestId("submit-column"));

    expect(await screen.findByText(entry.optionsRequired)).toBeTruthy();
    expect(document.body.textContent).not.toContain("options_required");
    expect(document.body.textContent).not.toContain("RAW_BACKEND");
    expect(document.body.textContent).not.toContain("addColumn: 422");
  });
});

// 116 dogfood: a 404 after changing space must not change hook order.
it("renders an unavailable matrix after a loaded view loses access and can recover", async () => {
  renderLocalized(locales[0], baseSnapshot);
  await screen.findByTestId("kb-matrix-run");
  act(() => { setMatrixSnapshot(null); mocks.unavailable?.(); });
  expect(screen.getByTestId("kb-matrix-unavailable")).toBeTruthy();
  fireEvent.click(screen.getByTestId("kb-matrix-unavailable-retry"));
  expect(await screen.findByTestId("kb-matrix-run")).toBeTruthy();
});


it("names the run options toggle and exposes its expanded state", async () => {
  renderLocalized(locales[0], baseSnapshot);
  const toggle = await screen.findByRole("button", { name: "运行选项" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByTestId("kb-matrix-runmenu")).toBeTruthy();
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
});
