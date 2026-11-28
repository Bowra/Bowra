/**
 * Matrix-run confirmation: dialog behavior (component unification P1, docs/design/组件统一-技术设计.md, “Dialog 合同”).
 * Formerly a custom div: no role, Esc could not close, Tab reached the background, and closing left focus on body.
 */

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunEstimate, RunScope } from "@/lib/kb-matrix/run-api";
import type { MatrixColumnVM } from "@/lib/kb-matrix/types";
import messagesZh from "@/messages/zh-CN.json";

import { RunConfirmModal } from "./run-confirm-modal";

const ESTIMATE: RunEstimate = {
  cells_pending: 12,
  calls: 12,
  credits_estimated: 30,
  eta_s: 20,
  skipped_edited: 0,
  available_credits: 1000,
  team_pool_tighter: false,
  warn: false,
  blocked: false,
};

function Harness({
  starting = false,
  scope = { kind: "all_pending" } as RunScope,
  onClose,
  onConfirm = vi.fn(),
  withTrigger = false,
}: {
  starting?: boolean;
  scope?: RunScope;
  onClose: () => void;
  onConfirm?: () => void;
  withTrigger?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [trigger, setTrigger] = useState<HTMLElement | null>(null);
  const other = useRef<HTMLButtonElement | null>(null);
  return (
    <div className="mx-app">
      <button type="button" onClick={(e) => { setTrigger(withTrigger ? e.currentTarget : null); setOpen(true); }}>运行</button>
      <button type="button" ref={other}>别处</button>
      {open ? (
        <RunConfirmModal
          scope={scope}
          estimate={ESTIMATE}
          starting={starting}
          returnFocusTo={trigger}
          onClose={() => { onClose(); setOpen(false); }}
          onConfirm={onConfirm}
        />
      ) : null}
    </div>
  );
}

function renderHarness(props: Partial<Parameters<typeof Harness>[0]> = {}) {
  const onClose = vi.fn();
  const view = render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <Harness onClose={onClose} {...props} />
    </NextIntlClientProvider>,
  );
  return { onClose, view };
}

async function openFrom(name: string) {
  const trigger = screen.getByRole("button", { name });
  trigger.focus();
  await act(async () => {
    fireEvent.click(trigger);
  });
  return trigger;
}

afterEach(cleanup);

describe("RunConfirmModal · 弹窗行为", () => {
  it("是带标题名称与说明的模态弹窗，就地渲染在矩阵容器里，焦点进弹窗", async () => {
    renderHarness();
    await openFrom("运行");
    const dialog = await screen.findByRole("dialog", { name: messagesZh.kbMatrix.run_modal_title_all });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog).toBe(screen.getByTestId("kb-matrix-run-modal"));
    expect(dialog.className).toBe("kb-modal");
    // The panel sits in the shared dialog sheet host inside the overlay.
    expect(dialog.parentElement?.className).toBe("ui-dialog-sheet-host");
    expect(dialog.parentElement?.parentElement?.className).toBe("kb-overlay");
    // Inline mounting: preserve ancestor scopes required by appearance
    expect(dialog.closest(".mx-app")).not.toBeNull();
    const describedBy = dialog.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)?.textContent).toContain(messagesZh.kbMatrix.run_modal_total);
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("Esc 关闭并把焦点还给触发按钮", async () => {
    const { onClose } = renderHarness();
    const trigger = await openFrom("运行");
    await screen.findByRole("dialog");
    await act(async () => {
      fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(document.activeElement).toBe(trigger);
  });

  it("returnFocusTo 指定时回到它（点击不给焦点的浏览器、从菜单打开）", async () => {
    renderHarness({ withTrigger: true });
    const trigger = screen.getByRole("button", { name: "运行" });
    screen.getByRole("button", { name: "别处" }).focus();
    await act(async () => {
      fireEvent.click(trigger);
    });
    await screen.findByRole("dialog");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: messagesZh.kbMatrix.cancel }));
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(document.activeElement).toBe(trigger);
  });

  it("点遮罩关闭，点面板不关", async () => {
    const { onClose } = renderHarness();
    await openFrom("运行");
    const dialog = await screen.findByRole("dialog");
    fireEvent.pointerDown(dialog);
    fireEvent.click(dialog);
    expect(onClose).not.toHaveBeenCalled();
    const overlay = dialog.closest(".kb-overlay") as HTMLElement;
    fireEvent.pointerDown(overlay);
    fireEvent.click(overlay);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("启动中 Esc、点遮罩、取消都不关闭，确认不能重复提交", async () => {
    const onConfirm = vi.fn();
    const { onClose } = renderHarness({ starting: true, onConfirm });
    await openFrom("运行");
    const dialog = await screen.findByRole("dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });
    const overlay = dialog.closest(".kb-overlay") as HTMLElement;
    fireEvent.pointerDown(overlay);
    fireEvent.click(overlay);
    const cancel = screen.getByRole("button", { name: messagesZh.kbMatrix.cancel }) as HTMLButtonElement;
    expect(cancel.disabled).toBe(true);
    fireEvent.click(cancel);
    expect(onClose).not.toHaveBeenCalled();
    const confirm = screen.getByTestId("kb-matrix-run-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("强制重跑的确认动作在底栏、是 kb-btn-danger-solid", async () => {
    renderHarness({ scope: { kind: "force_all" } as RunScope });
    await openFrom("运行");
    const confirm = screen.getByTestId("kb-matrix-run-confirm");
    expect(confirm.className).toContain("kb-btn-danger-solid");
    expect(confirm.closest(".kb-modal-foot")?.closest("[role=dialog]")).not.toBeNull();
  });
});

// ── Document matrix step 1 (S3): cost breakdown and trial option ──

const COLUMNS: MatrixColumnVM[] = [
  { columnId: "c_amount", name: "合同金额", type: "currency" },
  { columnId: "c_term", name: "到期日", type: "date" },
  { columnId: "c_noncompete", name: "竞业条款", type: "boolean", crossVerify: true },
];

const BREAKDOWN: RunEstimate = {
  ...ESTIMATE,
  cells_pending: 6,
  credits_estimated: 115,
  eta_s: 130,
  skipped_edited: 1,
  skipped_verified: 12,
  available_credits: 944553,
  files: 2,
  reading_credits: 32,
  columns: [
    { column_id: "c_amount", cells: 2, credits: 5, cross_verify: false },
    { column_id: "c_term", cells: 2, credits: 5, cross_verify: false },
    { column_id: "c_noncompete", cells: 2, credits: 73, cross_verify: true },
  ],
  longest_file: { file_id: "f_long", chars: 30000, credits: 104 },
  trial_file_ids: ["f_a", "f_long"],
};

function renderModal(props: Partial<Parameters<typeof RunConfirmModal>[0]> = {}) {
  const onTrialChange = vi.fn();
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <div className="mx-app">
        <RunConfirmModal
          scope={{ kind: "force_all" }}
          columns={COLUMNS}
          fileName={(id) => (id === "f_long" ? "北方物流运输合同.pdf" : undefined)}
          estimate={BREAKDOWN}
          starting={false}
          onClose={vi.fn()}
          onConfirm={vi.fn()}
          onTrialChange={onTrialChange}
          {...props}
        />
      </div>
    </NextIntlClientProvider>,
  );
  return { onTrialChange };
}

describe("RunConfirmModal · 花费明细", () => {
  it("每列一行：格子与额度；读取文档单列；合计、余额、用时与最长文件", () => {
    renderModal();
    const table = screen.getByTestId("kb-matrix-run-costs");
    const rows = within(table).getAllByRole("row").map((r) => Array.from(r.children).map((c) => c.textContent));
    expect(rows).toEqual([
      ["列", "格子", "预计额度"],
      ["读取文档（各列共用）", "—", "32"],
      ["合同金额", "2", "5"],
      ["到期日", "2", "5"],
      ["竞业条款 · 交叉校验", "2", "73"],
      ["合计", "6", "115"],
    ]);
    expect(screen.getByTestId("kb-matrix-run-summary").textContent).toBe("2 份文件 × 3 列，共 6 格。 保留 12 个已核和 1 个人工修改的格子，不会覆盖。");
    expect(screen.getByTestId("kb-matrix-run-cv-note").textContent).toContain("「竞业条款」开了交叉校验");
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("944,553 点额度");
    expect(dialog.textContent).toContain("约 3 分钟，可以离开页面");
    expect(screen.getByTestId("kb-matrix-run-longest").textContent).toBe("最长的一份是「北方物流运输合同.pdf」，约 30,000 字，占预计花费的 90%。");
  });

  it("交叉校验不是最大的一行时不提示；单个文件不显示最长文件；没有保留的格子不写保留", () => {
    renderModal({
      estimate: {
        ...BREAKDOWN,
        files: 1,
        skipped_edited: 0,
        skipped_verified: 0,
        reading_credits: 80,
        columns: [{ column_id: "c_noncompete", cells: 1, credits: 35, cross_verify: true }],
      },
    });
    expect(screen.queryByTestId("kb-matrix-run-cv-note")).toBeNull();
    expect(screen.queryByTestId("kb-matrix-run-longest")).toBeNull();
    expect(screen.getByTestId("kb-matrix-run-summary").textContent).toBe("1 份文件 × 1 列，共 6 格。");
  });

  it("试跑选项：勾选回调；勾选后确认按钮写明试跑几份文件且不再是危险样式", () => {
    const { onTrialChange } = renderModal({ trialFiles: 2 });
    const option = within(screen.getByTestId("kb-matrix-run-trial")).getByRole("checkbox");
    expect(screen.getByTestId("kb-matrix-run-trial").textContent).toBe("先在 2 份文件上试跑，看结果再决定");
    fireEvent.click(option);
    expect(onTrialChange).toHaveBeenCalledWith(true);
    cleanup();
    renderModal({ trialFiles: 2, trial: true, estimate: { ...BREAKDOWN, files: 2, cells_pending: 6 } });
    const confirm = screen.getByTestId("kb-matrix-run-confirm");
    expect(confirm.textContent).toBe("试跑 2 份文件");
    expect(confirm.className).toContain("kb-btn-primary");
  });

  it("没有试跑行时不显示试跑选项；修改列后的确认也不显示", () => {
    renderModal({ trialFiles: 0 });
    expect(screen.queryByTestId("kb-matrix-run-trial")).toBeNull();
    cleanup();
    renderModal({ trialFiles: 2, postEdit: true, scope: { kind: "stale_column", column_id: "c_term" } });
    expect(screen.queryByTestId("kb-matrix-run-trial")).toBeNull();
  });
});
