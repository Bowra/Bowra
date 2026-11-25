/** A deck reference's chip names its slides: one page, up to three listed, more counted (pptx-inline-edit design §13.6). */
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

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
