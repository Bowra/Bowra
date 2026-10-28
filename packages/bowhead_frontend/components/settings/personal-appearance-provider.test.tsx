import { useEffect, useState } from "react";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { PersonalAppearanceSnapshot } from "@/lib/personal-appearance";

const state = vi.hoisted(() => ({
  get: vi.fn(),
  patch: vi.fn(),
  compile: vi.fn(),
  reset: vi.fn(),
  undo: vi.fn(),
  refresh: vi.fn(async () => true),
  toast: vi.fn(),
}));

vi.mock("@/lib/personal-appearance", () => ({
  PERSONAL_APPEARANCE_REFRESH_EVENT: "bowhead:personal-appearance-refresh",
  getPersonalAppearance: state.get,
  patchPersonalAppearance: state.patch,
  compilePersonalAppearance: state.compile,
  resetPersonalAppearance: state.reset,
  undoPersonalAppearance: state.undo,
}));
vi.mock("@/lib/team-storage", () => ({
  getActiveTeamId: () => "team_1",
  TEAM_CHANGED_EVENT: "bowhead:team-changed",
}));
vi.mock("@/lib/use-theme-config", () => ({
  refreshEffectiveTheme: state.refresh,
}));
vi.mock("@/lib/use-toast", () => ({ toast: state.toast }));

import {
  PersonalAppearanceProvider,
  usePersonalAppearance,
} from "./personal-appearance-provider";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("PersonalAppearanceProvider", () => {
  it("refreshes a saved Chat result in its original Team without writing settings", async () => {
    state.get
      .mockResolvedValueOnce(snapshot(0))
      .mockResolvedValueOnce(snapshot(1));
    renderIntl(
      <PersonalAppearanceProvider>
        <Harness />
      </PersonalAppearanceProvider>,
    );
    await screen.findByText("saved:0");
    window.dispatchEvent(
      new CustomEvent("bowhead:personal-appearance-refresh", {
        detail: { teamId: "team_other" },
      }),
    );
    expect(state.get).toHaveBeenCalledTimes(1);
    window.dispatchEvent(
      new CustomEvent("bowhead:personal-appearance-refresh", {
        detail: { teamId: "team_1" },
      }),
    );
    await screen.findByText("saved:1");
    expect(state.get).toHaveBeenCalledTimes(2);
    expect(state.patch).not.toHaveBeenCalled();
    expect(state.reset).not.toHaveBeenCalled();
  });

  it("coalesces rapid field patches into one optimistic save", async () => {
    state.get.mockResolvedValue(snapshot(0));
    state.patch.mockResolvedValueOnce(snapshot(1));

    renderIntl(
      <PersonalAppearanceProvider>
        <Harness />
      </PersonalAppearanceProvider>,
    );
    await screen.findByText("saved:0");

    fireEvent.click(screen.getByRole("button", { name: "两次修改" }));

    expect(screen.getByText("saving:0")).toBeTruthy();
    expect(screen.getByText("pending:mode,density")).toBeTruthy();

    await waitFor(() => expect(state.patch).toHaveBeenCalledTimes(1));
    expect(state.patch.mock.calls[0]).toEqual([
      snapshot(0).etag,
      { mode: "dark", density: "compact" },
    ]);
    expect(state.refresh).toHaveBeenCalledTimes(1);
    expect(state.refresh).toHaveBeenLastCalledWith("team_1");
    await screen.findByText("saved:1");
    expect(screen.getByText("pending:none")).toBeTruthy();
  });

  it("keeps only the last rapid value for the same field", async () => {
    state.get.mockResolvedValue(snapshot(0));
    state.patch.mockResolvedValueOnce(snapshot(1));

    renderIntl(
      <PersonalAppearanceProvider>
        <Harness />
      </PersonalAppearanceProvider>,
    );
    await screen.findByText("saved:0");

    fireEvent.click(screen.getByRole("button", { name: "三次模式" }));

    await waitFor(() => expect(state.patch).toHaveBeenCalledTimes(1));
    expect(state.patch).toHaveBeenCalledWith(snapshot(0).etag, {
      mode: "dark",
    });
    await screen.findByText("saved:1");
  });

  it("serializes a later batch against the latest confirmed ETag", async () => {
    let resolveFirst!: (value: PersonalAppearanceSnapshot) => void;
    state.get.mockResolvedValue(snapshot(0));
    state.patch
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockResolvedValueOnce(snapshot(2));

    renderIntl(
      <PersonalAppearanceProvider>
        <Harness />
      </PersonalAppearanceProvider>,
    );
    await screen.findByText("saved:0");

    fireEvent.click(screen.getByRole("button", { name: "修改模式" }));
    await waitFor(() => expect(state.patch).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "修改密度" }));
    resolveFirst(snapshot(1));

    await waitFor(() => expect(state.patch).toHaveBeenCalledTimes(2));
    expect(state.patch.mock.calls[1]).toEqual([
      snapshot(1).etag,
      { density: "compact" },
    ]);
    await screen.findByText("saved:2");
  });

  it("rolls an optimistic field back and marks it when saving fails", async () => {
    state.get.mockResolvedValue(snapshot(0));
    state.patch.mockRejectedValueOnce(new Error("offline"));

    renderIntl(
      <PersonalAppearanceProvider>
        <Harness />
      </PersonalAppearanceProvider>,
    );
    await screen.findByText("mode:system");

    fireEvent.click(screen.getByRole("button", { name: "修改模式" }));
    expect(screen.getByText("mode:dark")).toBeTruthy();

    await screen.findByText("error:0");
    expect(screen.getByText("mode:system")).toBeTruthy();
    expect(screen.getByText("failed:mode")).toBeTruthy();
  });

  it("keeps reset undo alive after the modal content unmounts", async () => {
    state.get.mockResolvedValue(snapshot(0));
    state.reset.mockResolvedValue({
      snapshot: snapshot(1),
      undo: { resetId: "a".repeat(32), expiresAt: "2026-09-05T16:00:00Z" },
    });
    state.undo.mockResolvedValue(snapshot(2));

    renderIntl(
      <PersonalAppearanceProvider>
        <UnmountableHarness />
      </PersonalAppearanceProvider>,
    );
    await screen.findByText("saved:0");
    fireEvent.click(screen.getByRole("button", { name: "重置" }));

    await waitFor(() => expect(state.toast).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "关闭内容" }));
    const toastOptions = state.toast.mock.calls[0][0] as {
      action: { onAction: () => void };
    };
    toastOptions.action.onAction();

    await waitFor(() =>
      expect(state.undo).toHaveBeenCalledWith(snapshot(1).etag, "a".repeat(32)),
    );
  });
});

function Harness() {
  const appearance = usePersonalAppearance();
  useEffect(() => {
    void appearance.load();
  }, [appearance.load]);
  return (
    <div>
      <span>{`${appearance.status}:${appearance.snapshot?.resource.rowVersion ?? "none"}`}</span>
      <span>{`pending:${[...appearance.pendingFields].join(",") || "none"}`}</span>
      <span>{`failed:${[...appearance.failedFields].join(",") || "none"}`}</span>
      <span>{`mode:${appearance.snapshot?.resource.effective.mode ?? "none"}`}</span>
      <button
        type="button"
        onClick={() => {
          void appearance.patch({ mode: "dark" });
          void appearance.patch({ density: "compact" });
        }}
      >
        两次修改
      </button>
      <button
        type="button"
        onClick={() => {
          void appearance.patch({ mode: "dark" });
          void appearance.patch({ mode: "light" });
          void appearance.patch({ mode: "dark" });
        }}
      >
        三次模式
      </button>
      <button
        type="button"
        onClick={() => {
          void appearance.patch({ mode: "dark" }).catch(() => undefined);
        }}
      >
        修改模式
      </button>
      <button
        type="button"
        onClick={() => {
          void appearance.patch({ density: "compact" }).catch(() => undefined);
        }}
      >
        修改密度
      </button>
      <button type="button" onClick={() => void appearance.reset()}>
        重置
      </button>
    </div>
  );
}

function UnmountableHarness() {
  const [open, setOpen] = useState(true);
  return (
    <div>
      {open ? <Harness /> : null}
      <button type="button" onClick={() => setOpen(false)}>
        关闭内容
      </button>
    </div>
  );
}

function snapshot(rowVersion: number): PersonalAppearanceSnapshot {
  return {
    etag: `"personal-v3:${String(rowVersion).padStart(64, "a")}"`,
    resource: {
      rowVersion,
      overrides: {},
      effective: {
        mode: "system",
      },
      sources: {
        mode: "product",
        accent: "product",
        background: "product",
        typographyProfile: "product",
        typeScale: "product",
        density: "product",
        radius: "product",
        motion: "product",
      },
      team: { revisionId: null, revisionNumber: 0 },
    } as PersonalAppearanceSnapshot["resource"],
  };
}
