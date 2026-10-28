import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it } from "vitest";

import enUS from "@/messages/en-US.json";
import type { ReportSection } from "@/lib/types";

import { ExternalIframeSection } from "./external-iframe";

type Card = Extract<ReportSection, { kind: "external_iframe" }>;

afterEach(cleanup);

function renderCard(data: Card) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={enUS}>
      <ExternalIframeSection data={data} />
    </NextIntlClientProvider>,
  );
}

const PREVIEW = "https://docs.google.com/document/d/doc-1/preview?authuser=a%40acme.example";

function docCard(overrides: Partial<Card> = {}): Card {
  return {
    kind: "external_iframe",
    url: PREVIEW,
    title: "Q3 report",
    provider: "google_workspace",
    file_kind: "document",
    file_id: "doc-1",
    open_url: "https://docs.google.com/document/d/doc-1",
    snapshot_at: "2026-10-01T08:00:00+00:00",
    snapshot: { format: "markdown", content: "# Summary\nRevenue grew.", truncated: false },
    ...overrides,
  } as Card;
}

describe("Google file card in the canvas", () => {
  it("shows the snapshot and link by default and never loads Google until asked", () => {
    const { container } = renderCard(docCard());

    expect(screen.getByTestId("google-file-snapshot")).toHaveTextContent("Revenue grew.");
    // Markdown snapshots render as Markdown, not plain text.
    expect(screen.getByRole("heading", { name: "Summary" })).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /^Open in Google/ });
    expect(link).toHaveAttribute("href", "https://docs.google.com/document/d/doc-1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveTextContent("Open in Google (opens in a new tab)");
    expect(screen.getByText("Google Doc")).toBeInTheDocument();
    expect(container.querySelector("iframe")).toBeNull();

    fireEvent.click(screen.getByRole("radio", { name: "Live preview" }));
    const frame = container.querySelector("iframe");
    expect(frame).toHaveAttribute("src", PREVIEW);
    expect(screen.queryByTestId("google-file-snapshot")).toBeNull();

    fireEvent.click(screen.getByRole("radio", { name: "Snapshot" }));
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("always explains under the live preview what to do when it stays blank, even after the frame loads", () => {
    const { container } = renderCard(docCard());
    fireEvent.click(screen.getByRole("radio", { name: "Live preview" }));
    // A cross-origin frame fires load for Google's sign-in or refusal page too, so loading is not success.
    fireEvent.load(container.querySelector("iframe")!);
    expect(screen.getByTestId("google-live-hint")).toHaveTextContent(/Google may not allow embedding/);
    fireEvent.click(screen.getByRole("radio", { name: "Snapshot" }));
    expect(screen.queryByTestId("google-live-hint")).toBeNull();
  });

  it("renders a sheet snapshot as a table and notes truncation", () => {
    renderCard(
      docCard({
        file_kind: "spreadsheet",
        url: "https://docs.google.com/spreadsheets/d/s-1/htmlview",
        snapshot: { format: "table", rows: [["Region", "Revenue"], ["East", "1"]], range: "'Sheet1'!A1:B2", truncated: true },
      }),
    );

    expect(screen.getByText("Google Sheet")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "East" })).toBeInTheDocument();
    expect(screen.getByText(/Range written: 'Sheet1'!A1:B2/)).toBeInTheDocument();
    expect(screen.getByText(/only the beginning/)).toBeInTheDocument();
  });

  it("labels an edit snapshot as what was written, not the whole file", () => {
    renderCard(docCard({ snapshot: { format: "markdown", content: "Added paragraph", truncated: false, part: "written" } }));
    expect(screen.getByText("What this write added")).toBeInTheDocument();
  });

  it("falls back to the link and live preview when no snapshot could be read", () => {
    const { container } = renderCard(docCard({ snapshot: undefined, file_kind: undefined }));
    expect(screen.getByText(/could not be read/)).toBeInTheDocument();
    expect(screen.getByText("Google file")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Live preview" }));
    expect(container.querySelector("iframe")).toHaveAttribute("src", PREVIEW);
  });

  it("keeps ordinary external iframes unchanged", () => {
    const { container } = renderCard({ kind: "external_iframe", url: "https://example.com/dash", title: "Dash" } as Card);
    expect(container.querySelector("iframe")).toHaveAttribute("src", "https://example.com/dash");
    expect(screen.queryByTestId("google-file-card")).toBeNull();
  });
});
