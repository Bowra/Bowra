import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

import { ConfirmDialog, useConfirmDialog } from "./confirm-dialog";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ConfirmDialog", () => {
  it("focuses the localized cancel button first, even when the body has controls", () => {
    renderIntl(
      <ConfirmDialog
        title="删除报告？"
        description="删除后无法恢复。"
        confirmLabel="删除报告"
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
      >
        <input type="checkbox" aria-label="同时删除相关对话" />
      </ConfirmDialog>,
    );

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "取消" }));
  });
});

describe("useConfirmDialog", () => {
  function Harness({ onResult }: { onResult: (ok: boolean) => void }) {
    const { confirm, dialog } = useConfirmDialog();
    return (
      <>
        <button
          type="button"
          onClick={() => void confirm({ title: "Remove it?", confirmLabel: "Remove" }).then(onResult)}
        >
          Open
        </button>
        {dialog}
      </>
    );
  }

  it("resolves false on cancel and true on confirm", async () => {
    const onResult = vi.fn();
    renderIntl(<Harness onResult={onResult} />, { locale: "en-US" });

    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    });
    expect(onResult).toHaveBeenLastCalledWith(false);
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    });
    expect(onResult).toHaveBeenLastCalledWith(true);
  });
});
