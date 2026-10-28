/** Attachment upload failure: the 32px chip keeps its name and a short state; the full sentence is its title and
 * accessible name, and Copy diagnostics lives in the composer notice slot, never in the chip. */
import "@testing-library/jest-dom/vitest";

import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { HttpStatusError } from "@/lib/api-client";
import { AttachmentChip } from "./attachment-chip";

const REQUEST_ID = "0123456789abcdef0123456789abcdef";

afterEach(() => cleanup());

describe("attachment upload failure", () => {
  it.each([
    ["zh-CN", "失败"],
    ["en-US", "Failed"],
    ["de-DE", "Fehlgeschlagen"],
  ] as const)("%s: a short state on the chip, the sentence in its title, no status code or diagnostics", (locale, short) => {
    const cause = new HttpStatusError("upload", new Response(null, { status: 403, headers: { "x-request-id": REQUEST_ID } }));
    const { container } = renderIntl(
      <AttachmentChip
        item={{ localId: "a1", filename: "data.csv", size: 10, mime: "text/csv", status: "error", errorKind: "other", error: "localized reason", errorCause: cause }}
        onRemove={() => {}}
      />,
      { locale },
    );

    const chip = container.querySelector(".v2-chip")!;
    expect(chip.textContent + String(chip.getAttribute("title"))).not.toMatch(/403|upload:|presign/);
    expect(chip.textContent).not.toContain("localized reason");
    expect(chip.textContent).toContain(short);
    expect(container.querySelector('[title*="localized reason"], [aria-label*="localized reason"]')).not.toBeNull();
    expect(screen.queryByRole("button", { name: /复制诊断|Copy diagnostics|Diagnoseinformationen/ })).toBeNull();
  });

  it("renders no Copy diagnostics when the failure is a local check (no status)", () => {
    renderIntl(
      <AttachmentChip item={{ localId: "a1", filename: "big.csv", size: 10, mime: "text/csv", status: "error", errorKind: "size", error: "too big" }} onRemove={() => {}} />,
    );
    expect(screen.queryByRole("button", { name: "复制诊断" })).toBeNull();
  });
});
