import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { DialogShell } from "@/components/ui/dialog";
import type { KbFileDTO } from "@/lib/kb-api";
import messagesZh from "@/messages/zh-CN.json";

import { KbPreview } from "./kb-preview";

afterEach(cleanup);

const file = {
  file_id: "kbf_preview",
  filename: "slides.pptx",
  mime_type: "application/vnd.ms-powerpoint",
} as KbFileDTO;

describe("KbPreview", () => {
  it("is a nested dialog: Esc closes only the preview, not the drawer it sits in", () => {
    const onDrawerClose = vi.fn();
    const onPreviewClose = vi.fn();
    renderIntl(
      <DialogShell onClose={onDrawerClose} label="drawer">
        <button type="button">Drawer action</button>
        <KbPreview file={file} onClose={onPreviewClose} />
      </DialogShell>,
    );
    const close = screen.getByRole("button", { name: messagesZh.knowledge_base.preview.close_aria });
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(close, { key: "Escape" });
    expect(onPreviewClose).toHaveBeenCalledTimes(1);
    expect(onDrawerClose).not.toHaveBeenCalled();
  });
});
