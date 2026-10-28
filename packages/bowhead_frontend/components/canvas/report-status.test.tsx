/**
 * WP5 (R2): report status chip/controls — badge colors + available transitions + applied onChange.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

vi.mock("@/lib/api", () => ({
  fetchCanvasStatus: vi.fn(async () => ({
    status: "active", effectiveStatus: "active", quarantinedCount: 0,
  })),
  setCanvasStatus: vi.fn(async () => undefined),
}));

import { fetchCanvasStatus, setCanvasStatus } from "@/lib/api";
import { ActiveReportStatusMenuItems, ReportStatusBadge, ReportStatusControl, ReportStatusChip } from "./report-status";

const messages = {
  report_status: {
    active: "Report status: active",
    verified: "Report status: marked reviewed by owner",
    stale: "Report status: may be out of date",
    quarantined_stale: "Report status: may be out of date due to quarantined content",
    archived: "Report status: archived",
    change_status: "Change report status",
    action_verify: "Mark Reviewed", action_unverify: "Clear Reviewed Mark",
    action_archive: "Archive", action_restore: "Restore",
  },
};

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function wrap(node: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages}>{node}</NextIntlClientProvider>,
  );
}

describe("ReportStatusBadge", () => {
  it("keeps owner review neutral and gives lifecycle an explicit subject", () => {
    const reviewed = wrap(<ReportStatusBadge status="verified" />);
    expect(reviewed.container.querySelector(".rc-badge.neutral")?.textContent)
      .toContain("Report status: marked reviewed by owner");
    expect(reviewed.container.querySelector(".rc-badge.conf-high")).toBeNull();
    expect(wrap(<ReportStatusBadge status="stale" />).container.querySelector(".rc-badge.amber")?.textContent)
      .toContain("Report status: may be out of date");
    expect(wrap(<ReportStatusBadge status="archived" />).container.querySelector(".rc-badge.neutral")?.textContent)
      .toContain("Report status: archived");
  });

  it("distinguishes quarantine-derived staleness", () => {
    expect(wrap(<ReportStatusBadge status="stale" quarantinedCount={2} />).container.textContent)
      .toContain("Report status: may be out of date due to quarantined content");
  });
});

describe("ReportStatusControl", () => {
  it("active report → menu offers Mark Reviewed + Archive", () => {
    const onChange = vi.fn();
    const { getByTitle, getByRole, queryByRole } = wrap(
      <ReportStatusControl storedStatus="active" effectiveStatus="active" onChange={onChange} />,
    );
    fireEvent.click(getByTitle("Change report status"));
    expect(getByRole("menuitem", { name: "Mark Reviewed" })).toBeTruthy();
    expect(getByRole("menuitem", { name: "Archive" })).toBeTruthy();
    expect(queryByRole("menuitem", { name: "Restore" })).toBeNull();
  });

  it("owner-reviewed report → offers Clear Reviewed Mark", () => {
    const { getByTitle, getByRole, queryByRole } = wrap(
      <ReportStatusControl storedStatus="verified" effectiveStatus="verified" onChange={vi.fn()} />,
    );
    fireEvent.click(getByTitle("Change report status"));
    expect(getByRole("menuitem", { name: "Clear Reviewed Mark" })).toBeTruthy();
    expect(queryByRole("menuitem", { name: "Mark Reviewed" })).toBeNull();
  });

  it("archived report → only Restore", () => {
    const { getByTitle, getByRole, queryByRole } = wrap(
      <ReportStatusControl storedStatus="archived" effectiveStatus="archived" onChange={vi.fn()} />,
    );
    fireEvent.click(getByTitle("Change report status"));
    expect(getByRole("menuitem", { name: "Restore" })).toBeTruthy();
    expect(queryByRole("menuitem", { name: "Archive" })).toBeNull();
  });

  it("clicking a transition fires onChange with the target status", () => {
    const onChange = vi.fn();
    const { getByTitle, getByRole } = wrap(
      <ReportStatusControl storedStatus="active" effectiveStatus="active" onChange={onChange} />,
    );
    fireEvent.click(getByTitle("Change report status"));
    fireEvent.click(getByRole("menuitem", { name: "Archive" }));
    expect(onChange).toHaveBeenCalledWith("archived");
  });

  it("shows the EFFECTIVE status on the badge (stale) even if stored is active", () => {
    const { container } = wrap(
      <ReportStatusControl storedStatus="active" effectiveStatus="stale" onChange={vi.fn()} />,
    );
    expect(container.querySelector(".rc-badge.amber")?.textContent)
      .toContain("Report status: may be out of date");
  });
});

describe("ReportStatusControl menu behaviour", () => {
  it("opens into the first transition on the keyboard, moves with arrows and returns focus on Escape", async () => {
    const { getByTitle, getByRole, queryByRole } = wrap(
      <ReportStatusControl storedStatus="active" effectiveStatus="active" onChange={vi.fn()} />,
    );
    const trigger = getByTitle("Change report status");
    fireEvent.click(trigger, { detail: 0 });
    await waitFor(() => expect(document.activeElement).toBe(getByRole("menuitem", { name: "Mark Reviewed" })));
    fireEvent.keyDown(getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(getByRole("menuitem", { name: "Archive" }));

    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });

    await waitFor(() => expect(queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("closes on a press outside", async () => {
    const { getByTitle, getByRole, queryByRole } = wrap(
      <ReportStatusControl storedStatus="active" effectiveStatus="active" onChange={vi.fn()} />,
    );
    fireEvent.click(getByTitle("Change report status"));
    expect(getByRole("menu")).toBeTruthy();

    fireEvent.pointerDown(document.body);

    await waitFor(() => expect(queryByRole("menu")).toBeNull());
  });
});

describe("ReportStatusChip", () => {
  it("fetches status on mount and applies a transition via setCanvasStatus", async () => {
    const { getByTitle, getByRole } = wrap(<ReportStatusChip canvasId="cv_1" />);
    await waitFor(() => expect(fetchCanvasStatus).toHaveBeenCalledWith("cv_1"));
    fireEvent.click(getByTitle("Change report status"));
    fireEvent.click(getByRole("menuitem", { name: "Archive" }));
    await waitFor(() => expect(setCanvasStatus).toHaveBeenCalledWith("cv_1", "archived"));
  });

  it("hides the default active status in the toolbar placement (Report Canvas Chrome Spec)", async () => {
    const { container, queryByText } = wrap(<ReportStatusChip canvasId="cv_1" hideDefault />);
    await waitFor(() => expect(fetchCanvasStatus).toHaveBeenCalledWith("cv_1"));
    await waitFor(() => expect(container.innerHTML).toBe(""));
    expect(queryByText("Report status: active")).toBeNull();
  });

  it("keeps a non-default status visible in the toolbar placement", async () => {
    vi.mocked(fetchCanvasStatus).mockResolvedValueOnce({ status: "verified", effectiveStatus: "verified", quarantinedCount: 0 } as never);
    const { findByText } = wrap(<ReportStatusChip canvasId="cv_1" hideDefault />);
    expect(await findByText("Report status: marked reviewed by owner")).toBeTruthy();
  });
});

describe("ActiveReportStatusMenuItems", () => {
  it("offers the active report's transitions for the report ⋯ menu", async () => {
    const onSelect = vi.fn();
    const { findByRole } = wrap(
      <ActiveReportStatusMenuItems
        canvasId="cv_1"
        renderItem={(item) => (
          <button key={item.key} type="button" onClick={() => { onSelect(item.key); item.onSelect(); }}>
            {item.label}
          </button>
        )}
      />,
    );
    expect(await findByRole("button", { name: "Mark Reviewed" })).toBeTruthy();
    fireEvent.click(await findByRole("button", { name: "Archive" }));
    await waitFor(() => expect(setCanvasStatus).toHaveBeenCalledWith("cv_1", "archived"));
  });

  it("renders nothing for a non-default status (its chip carries the menu)", async () => {
    vi.mocked(fetchCanvasStatus).mockResolvedValueOnce({ status: "archived", effectiveStatus: "archived", quarantinedCount: 0 } as never);
    const { container } = wrap(<ActiveReportStatusMenuItems canvasId="cv_1" renderItem={() => <button type="button">x</button>} />);
    await waitFor(() => expect(fetchCanvasStatus).toHaveBeenCalled());
    expect(container.querySelector("button")).toBeNull();
  });
});

describe("one status request per canvas (116 dogfood: opening a report sent the status GET twice)", () => {
  const renderItem = (item: { key: string; label: string; onSelect: () => void }) => (
    <button key={item.key} type="button" onClick={item.onSelect}>{item.label}</button>
  );

  it("shares one GET between the chip and the report ⋯ items", async () => {
    const { findByRole } = wrap(
      <>
        <ReportStatusChip canvasId="cv_1" hideDefault />
        <ActiveReportStatusMenuItems canvasId="cv_1" renderItem={renderItem} />
      </>,
    );
    expect(await findByRole("button", { name: "Archive" })).toBeTruthy();
    expect(fetchCanvasStatus).toHaveBeenCalledTimes(1);
  });

  it("reuses the read when the chip moves from the header to the toolbar placement", async () => {
    vi.mocked(fetchCanvasStatus).mockResolvedValueOnce({ status: "verified", effectiveStatus: "verified", quarantinedCount: 0 } as never);
    const { findByText, rerender } = wrap(<div key="header"><ReportStatusChip canvasId="cv_1" hideDefault /></div>);
    expect(await findByText("Report status: marked reviewed by owner")).toBeTruthy();
    // The results workspace capability arrives: the header chip unmounts and the toolbar chip mounts.
    rerender(
      <NextIntlClientProvider locale="en-US" messages={messages}>
        <div key="toolbar"><ReportStatusChip canvasId="cv_1" readOnly hideDefault /></div>
      </NextIntlClientProvider>,
    );
    expect(await findByText("Report status: marked reviewed by owner")).toBeTruthy();
    expect(fetchCanvasStatus).toHaveBeenCalledTimes(1);
  });

  it("refetches once per canvas change, however many readers are mounted", async () => {
    const { findByRole } = wrap(
      <>
        <ReportStatusChip canvasId="cv_1" />
        <ActiveReportStatusMenuItems canvasId="cv_1" renderItem={renderItem} />
      </>,
    );
    await findByRole("button", { name: "Archive" });
    window.dispatchEvent(new Event("bowhead.canvas.changed"));
    await waitFor(() => expect(fetchCanvasStatus).toHaveBeenCalledTimes(2));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(fetchCanvasStatus).toHaveBeenCalledTimes(2);
  });

  it("reads the new status once after a change (previously twice: its own reload plus the change event)", async () => {
    const { findByRole } = wrap(<ActiveReportStatusMenuItems canvasId="cv_1" renderItem={renderItem} />);
    vi.mocked(fetchCanvasStatus).mockResolvedValueOnce({ status: "archived", effectiveStatus: "archived", quarantinedCount: 0 } as never);
    fireEvent.click(await findByRole("button", { name: "Archive" }));
    await waitFor(() => expect(setCanvasStatus).toHaveBeenCalledWith("cv_1", "archived"));
    await waitFor(() => expect(fetchCanvasStatus).toHaveBeenCalledTimes(2));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(fetchCanvasStatus).toHaveBeenCalledTimes(2);
  });
});
