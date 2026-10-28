/**
 * Matrix-run confirmation: dialog behavior (component unification P1, docs/design/组件统一-技术设计.md, “Dialog 合同”).
 * Formerly a custom div: no role, Esc could not close, Tab reached the background, and closing left focus on body.
 */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunEstimate, RunScope } from "@/lib/kb-matrix/run-api";
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
    expect(document.getElementById(describedBy!)?.textContent).toContain(messagesZh.kbMatrix.run_modal_pending);
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
