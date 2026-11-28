/** A deck reference's chip names its slides: one page, up to three listed, more counted (pptx-inline-edit design §13.6). */
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { AttachmentChip } from "./attachment-chip";

afterEach(() => cleanup());

const deck = (slides: number[]) => ({
  localId: "a1", fileId: "art_deck", filename: "季度复盘.pptx", size: 10, status: "done" as const, referenceKind: "result" as const,
  mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  presentationSelection: { version: 2 as const, artifactId: "art_deck", slides: slides.map((slideIndex) => ({ slideIndex })) },
});

describe("deck page references on a chip", () => {
  it.each([
    ["zh-CN", [3], "第 3 页"],
    ["zh-CN", [2, 5, 7], "第 2、5、7 页"],
    ["zh-CN", [1, 2, 5, 7, 9, 10], "第 1、2、5 等 6 页"],
    ["en-US", [2, 5], "Slides 2, 5"],
    ["en-US", [1, 2, 5, 7], "Slides 1, 2, 5 and 1 more"],
  ] as const)("%s %j reads %s", (locale, slides, label) => {
    const { container } = renderIntl(<AttachmentChip item={deck([...slides])} onRemove={() => {}} />, { locale });
    expect(container.querySelector(".v2-chip")).toHaveTextContent(label);
  });
});

describe("a page set on a chip", () => {
  it("lists its pages, each removable on its own (design §13.7)", async () => {
    const onRemoveSlide = vi.fn();
    renderIntl(<AttachmentChip item={deck([2, 5])} onRemove={() => {}} onRemoveSlide={onRemoveSlide} />, { locale: "zh-CN" });
    fireEvent.click(screen.getByRole("button", { name: "查看引用的页" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "取消引用第 5 页" }));
    expect(onRemoveSlide).toHaveBeenCalledWith("a1", 5);
  });

  it("has no list for a single page", () => {
    renderIntl(<AttachmentChip item={deck([2])} onRemove={() => {}} onRemoveSlide={() => {}} />, { locale: "zh-CN" });
    expect(screen.queryByRole("button", { name: "查看引用的页" })).not.toBeInTheDocument();
  });
});


describe("Word references on a chip (docx inline edit design §7.4)", () => {
  const word = (documentSelection: object) => ({
    localId: "w1", fileId: "art_doc", filename: "复盘.docx", size: 10, status: "done" as const, referenceKind: "result" as const,
    mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    documentSelection: documentSelection as never,
  });

  it.each([
    ["zh-CN", { version: 2, artifactId: "art_doc", pages: [{ page: 4 }] }, "第 4 页"],
    ["zh-CN", { version: 2, artifactId: "art_doc", pages: [{ page: 1 }, { page: 2 }, { page: 5 }, { page: 9 }] }, "第 1、2、5 等 4 页"],
    ["en-US", { version: 2, artifactId: "art_doc", pages: [{ page: 2 }, { page: 5 }] }, "Pages 2, 5"],
    ["zh-CN", { version: 1, artifactId: "art_doc", paragraphKey: "document:b3", paragraphName: "一、总体情况 · 第 2 段" }, "一、总体情况 · 第 2 段"],
  ] as const)("%s reads %s", (locale, selection, label) => {
    const { container } = renderIntl(<AttachmentChip item={word(selection)} onRemove={() => {}} />, { locale });
    expect(container.querySelector(".v2-chip")).toHaveTextContent(label);
  });

  it("lists a Word page set like a deck's, each page removable", async () => {
    const onRemoveSlide = vi.fn();
    renderIntl(<AttachmentChip item={word({ version: 2, artifactId: "art_doc", pages: [{ page: 2 }, { page: 5 }] })} onRemove={() => {}} onRemoveSlide={onRemoveSlide} />, { locale: "zh-CN" });
    fireEvent.click(screen.getByRole("button", { name: "查看引用的页" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "取消引用第 5 页" }));
    expect(onRemoveSlide).toHaveBeenCalledWith("w1", 5);
  });
});
