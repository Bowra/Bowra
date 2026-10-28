import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import type { Editor } from "@tiptap/react";
import { NextIntlClientProvider } from "next-intl";

import zhMessages from "@/messages/zh-CN.json";

import { renderIntl } from "@/__tests__/intl-render";
import { resetAiRewriteDrafts } from "@/lib/ai-rewrite-drafts";
import { BlockAIBar } from "./block-ai-bar";

vi.mock("./use-report-center", () => ({ useReportCenterStyle: () => ({}) }));

/** Only the minimum needed by findSectionTextRange: one section node. */
function fakeEditor(): Editor {
  return {
    state: {
      doc: {
        descendants(cb: (node: unknown, pos: number) => boolean | void) {
          for (const sid of ["sec_a", "sec_b"]) {
            cb({ attrs: { sectionId: sid }, type: { name: "section_narrative" }, nodeSize: 12 }, sid === "sec_a" ? 0 : 20);
          }
        },
      },
    },
  } as unknown as Editor;
}

afterEach(() => {
  cleanup();
  resetAiRewriteDrafts();
});

function renderBar(sectionId: string | null, draftScope = "s_1", onSubmit = vi.fn()) {
  const editor = fakeEditor();
  const onClose = vi.fn();
  const view = renderIntl(
    <BlockAIBar editor={editor} sectionId={sectionId} onClose={onClose} onSubmit={onSubmit} draftScope={draftScope} />,
  );
  const rerender = (next: string | null, scope = draftScope) => view.rerender(
    <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
      <BlockAIBar editor={editor} sectionId={next} onClose={onClose} onSubmit={onSubmit} draftScope={scope} />
    </NextIntlClientProvider>,
  );
  return { rerender, onSubmit };
}

const input = () => screen.getByRole("textbox") as HTMLInputElement;

describe("整块 AI 改写输入的草稿（第 4a 批）", () => {
  it("keeps the draft when the same section is closed and reopened; sections and sessions keep separate drafts; sending clears it", () => {
    const { rerender, onSubmit } = renderBar("sec_a");
    fireEvent.change(input(), { target: { value: "压到三句话" } });
    rerender(null);
    rerender("sec_a");
    expect(input().value).toBe("压到三句话");
    rerender("sec_b");
    expect(input().value).toBe("");
    rerender("sec_a", "s_2");
    expect(input().value).toBe("");
    rerender("sec_a", "s_1");
    expect(input().value).toBe("压到三句话");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("压到三句话", { from: 1, to: 11 });
    rerender(null);
    rerender("sec_a");
    expect(input().value).toBe("");
  });
});
