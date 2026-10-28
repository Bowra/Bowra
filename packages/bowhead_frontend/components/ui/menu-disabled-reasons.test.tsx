/**
 * Behavior batch A: disabled reasons (decision 9) at call sites—disabled items omit native disabled (remaining keyboard
 * highlightable), keep reasons visible on a second line and read them via aria-describedby, and do not execute when selected. Call sites: report status (saving), export menu (exporting), result-object Give Feedback on This Format
 * (recorded), chart/table “⋯”, row-end “⋯” (an action in the list is underway).
 */
import { useState } from "react";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ExportMenu } from "@/components/canvas/export-ui";
import { ReportStatusControl } from "@/components/canvas/report-status";
import { ResultObjectActions } from "@/components/canvas/result-object-actions";
import { TableActions } from "@/components/canvas/sections/table-actions";
import { MENU_DESCRIPTION_ATTR, useMenu } from "@/components/ui/menu";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchPinnedAttachments: vi.fn(() => Promise.resolve([])),
}));

afterEach(() => {
  cleanup();
});

function reasonOf(item: HTMLElement): string | null | undefined {
  const id = item.getAttribute("aria-describedby");
  return id ? document.getElementById(id)?.textContent : null;
}

let setStatusBusy: ((busy: boolean) => void) | null = null;

function StatusHost({ onChange }: { onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  setStatusBusy = setBusy;
  return <ReportStatusControl storedStatus="active" effectiveStatus="active" onChange={onChange} busy={busy} />;
}

function ExportHost({ busy, onPick }: { busy: boolean; onPick: (fmt: string) => void }) {
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen });
  return (
    <>
      <button type="button" ref={menu.refs.setReference} {...menu.getReferenceProps()}>
        Export
      </button>
      <ExportMenu menu={menu} busy={busy} onPick={onPick} onOpenHistory={() => {}} />
    </>
  );
}

describe("disabled menu items keep a visible reason", () => {
  it("report status: items while saving", () => {
    const onChange = vi.fn();
    renderIntl(<StatusHost onChange={onChange} />);
    fireEvent.click(document.querySelector<HTMLButtonElement>(".rc-status-trigger")!);
    // Enter saving while the menu is open (the trigger becomes disabled, but the menu stays open).
    act(() => setStatusBusy?.(true));
    const item = screen.getByRole("menuitem", { name: "归档" });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    expect(item.hasAttribute("disabled")).toBe(false);
    expect(reasonOf(item)).toBe("正在保存状态，保存完成后可以再更改");
    fireEvent.click(item);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("export menu: format rows while exporting carry the description marker", async () => {
    const onPick = vi.fn();
    renderIntl(<ExportHost busy onPick={onPick} />);
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    const pdf = await screen.findByRole("menuitem", { name: /PDF 文档/ });
    expect(pdf.getAttribute("aria-disabled")).toBe("true");
    expect(pdf.hasAttribute("disabled")).toBe(false);
    expect(reasonOf(pdf)).toBe("正在导出，完成后可以再导出");
    expect(Boolean(pdf.querySelector(`[${MENU_DESCRIPTION_ATTR}]`))).toBe(true);
    fireEvent.click(pdf);
    expect(onPick).not.toHaveBeenCalled();
  });

  it("result object: format feedback already recorded", () => {
    const onFeedback = vi.fn();
    renderIntl(
      <ResultObjectActions
        artifactId="art_1"
        capabilities={new Set(["feedback_unsupported"])}
        role="unsupported"
        mode="preview"
        downloadHref="/x"
        feedbackRecorded
        onFeedbackUnsupported={onFeedback}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    // The item keeps the action name with a second-line reason.
    const item = screen.getByRole("menuitem", { name: "反馈此格式" });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    expect(item.hasAttribute("disabled")).toBe(false);
    expect(reasonOf(item)).toBe("已记录你对这个格式的需求，不用重复提交");
    fireEvent.click(item);
    expect(onFeedback).not.toHaveBeenCalled();
  });

  it("chart and table more menu: the disabled reason (title) shows on a second line", () => {
    const onClick = vi.fn();
    renderIntl(
      <TableActions
        variant="quiet"
        moreLabel="更多操作"
        actions={[{ key: "csv", label: "下载 CSV", disabled: true, title: "数据还在加载，加载完成后可以下载", onClick }]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    const item = screen.getByRole("menuitem", { name: "下载 CSV" });
    expect(item.hasAttribute("disabled")).toBe(false);
    expect(reasonOf(item)).toBe("数据还在加载，加载完成后可以下载");
    fireEvent.click(item);
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe("row actions menu", () => {
  it("while a list action runs, disabled items without a reason show the generic one and others keep their own", () => {
    renderIntl(
      <RowActionsMenu
        pending
        actions={[
          { id: "edit", label: "编辑", disabled: true, onSelect: vi.fn() },
          { id: "copy", label: "复制", disabled: true, disabledReason: "已达到上限", onSelect: vi.fn() },
          { id: "delete", label: "删除", danger: true, disabled: true, onSelect: vi.fn() },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    const menu = screen.getByRole("menu");
    expect(reasonOf(within(menu).getByRole("menuitem", { name: "编辑" }))).toBe("正在处理上一个操作，完成后再试");
    expect(reasonOf(within(menu).getByRole("menuitem", { name: "复制" }))).toBe("已达到上限");
    expect(reasonOf(within(menu).getByRole("menuitem", { name: "删除…" }))).toBe("正在处理上一个操作，完成后再试");
  });

  it("invents no reason when nothing runs; danger items that do not confirm carry no ellipsis", async () => {
    renderIntl(
      <RowActionsMenu
        actions={[
          { id: "edit", label: "编辑", disabled: true, onSelect: vi.fn() },
          { id: "stop", label: "停止", danger: true, confirms: false, onSelect: vi.fn() },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeTruthy());
    expect(screen.getByRole("menuitem", { name: "编辑" }).getAttribute("aria-describedby")).toBeNull();
    expect(screen.getByRole("menuitem", { name: "停止" })).toBeTruthy();
  });
});
