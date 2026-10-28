import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CodeBlockSection } from "./code-block";

const focusSpy = vi.fn();

vi.mock("@/lib/canvas-workspace-focus", () => ({
  requestCanvasDataFocus: (...args: unknown[]) => focusSpy(...args),
}));

afterEach(() => {
  cleanup();
  focusSpy.mockClear();
});

const messages = {
  code_block: {
    summary_format: "{n_lines} lines · {statement_summary} · {dur_ms}ms",
    stdout_label: "stdout",
    lang_label_python: "Python",
    lang_label_sql: "SQL",
    lang_label_shell: "Shell",
  },
};

function renderWithIntl(node: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      {node}
    </NextIntlClientProvider>,
  );
}

describe("CodeBlockSection", () => {
  it("renders artifact chips with filenames and focuses by artifact id", () => {
    renderWithIntl(
      <CodeBlockSection
        data={{
          id: "sec_code",
          kind: "code_block",
          call_id: "call_1",
          lang: "python",
          code: "print('ok')",
          input_artifact_ids: ["art_in"],
          output_artifact_ids: ["art_out"],
          input_artifacts: [{ artifact_id: "art_in", filename: "source.csv", mime: "text/csv" }],
          output_artifacts: [{ artifact_id: "art_out", filename: "summary.csv", mime: "text/csv" }],
        }}
      />,
    );

    const input = screen.getByRole("button", { name: "← source.csv" });
    const output = screen.getByRole("button", { name: "→ summary.csv" });
    expect(input).toHaveAttribute("title", "source.csv · art_in");
    expect(output).toHaveAttribute("title", "summary.csv · art_out");

    fireEvent.click(output);
    expect(focusSpy).toHaveBeenCalledWith("art_out", {
      entryPoint: "report",
      reportContext: {
        label: "summary.csv",
        sectionId: "sec_code",
      },
    });
  });
});
