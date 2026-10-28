import { fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider, useTranslations } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/messages/en-US.json";
import fixture from "@/__tests__/fixtures/document-draft-canvas.json";
import type { DocumentJob } from "@/lib/document-drafts";
import { CanvasOpenMenu } from "@/components/canvas/object";
import { DocumentCanvasList } from "./document-canvas-list";
import { documentOpenItems } from "./document-open-items";

const job: DocumentJob = {
  id: "djob_original", origin_turn_id: "t_original", request_text: "Use the supplied quarterly data.",
  state: "failed", error_code: "document_information_incomplete", can_confirm: false,
};

function Opener({ jobs, onOpen }: { jobs: DocumentJob[]; onOpen: (id: string) => void }) {
  const documents = useTranslations("documents");
  const settingsCanvas = useTranslations("settings_canvas");
  return <CanvasOpenMenu model={{ documents: {
    items: documentOpenItems(jobs, { documents, settingsCanvas }),
    loading: false, error: false, hasPrevious: false, hasNext: false,
    onOpen, onRefresh: vi.fn(), onPrevious: vi.fn(), onNext: vi.fn(),
  } }} />;
}

describe("document discovery lifecycle", () => {
  it.each([
    ["failed", "document_information_incomplete", "Not completed"],
    ["failed", "planning_quote_mismatch", "Generation failed"],
    ["failed", "unknown_provider_error", "Generation failed"],
    ["question", null, "Needs information"],
    ["published", "planning_quote_mismatch", "Published"],
  ] as const)("keeps list and Open menu aligned for %s / %s", async (state, error_code, status) => {
    const onOpen = vi.fn();
    const jobs = [{
      ...job, state, error_code,
      ...(state === "published" ? { draft_digest: fixture.published.draft_digest, draft: { ...fixture.published.draft, output_stem: "" } } : {}),
    }];
    render(<NextIntlClientProvider locale="en-US" messages={messages}>
      <DocumentCanvasList jobs={jobs} loading={false} error={false} hasPrevious={false} hasNext={false}
        onOpen={onOpen} onRefresh={vi.fn()} onPrevious={vi.fn()} onNext={vi.fn()} />
      <Opener jobs={jobs} onOpen={onOpen} />
    </NextIntlClientProvider>);
    const list = screen.getByRole("region", { name: "Document drafts" });
    // No task ID is appended to the generic title.
    expect(within(list).getByText("Template presentation")).toBeTruthy();
    expect(within(list).getByText(job.request_text)).toBeTruthy();
    expect(within(list).getByText(status)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open…" }));
    const menu = await screen.findByRole("menu");
    const entry = within(menu).getByRole("menuitem", { name: /Template presentation/ });
    expect(entry.textContent).toContain(status);
    fireEvent.click(entry);
    expect(onOpen).toHaveBeenCalledExactlyOnceWith(job.id);
  });
});
