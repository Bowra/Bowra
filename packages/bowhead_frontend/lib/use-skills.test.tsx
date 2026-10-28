/**
 * chat-page-polish B4 — useSkills shared hook tests.
 *
 * Core assertion: two mounted components (simulating /profile + /settings overview) receive
 * identical skill counts with only one HTTP request (dedupe).
 */

import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SkillListDTO } from "./api";
import {
  _resetSkillsCacheForTest,
  computeSkillCounts,
  dispatchSkillsChanged,
  useSkills,
} from "./use-skills";

function makeSkill(name: string, enabled: boolean) {
  return {
    name, description: "", version: "1.0",
    source_type: "builtin" as const,
    enabled,
    size_bytes: 100,
    created_at: null,
  };
}

function makeListDTO(enabled: number, disabled: number): SkillListDTO {
  return {
    builtin: [
      ...Array.from({ length: enabled }, (_, i) => makeSkill(`b_e_${i}`, true)),
      ...Array.from({ length: disabled }, (_, i) => makeSkill(`b_d_${i}`, false)),
    ],
    user_upload: [],
    user_generated: [],
  };
}

describe("computeSkillCounts (pure)", () => {
  it("returns 0/0 for null", () => {
    expect(computeSkillCounts(null)).toEqual({ enabled: 0, total: 0 });
  });

  it("counts enabled + total across all 3 buckets", () => {
    const dto: SkillListDTO = {
      builtin: [makeSkill("a", true), makeSkill("b", false)],
      user_upload: [makeSkill("c", true)],
      user_generated: [makeSkill("d", true), makeSkill("e", false)],
    };
    expect(computeSkillCounts(dto)).toEqual({ enabled: 3, total: 5 });
  });

  it("returns total=count when all enabled (Settings page 25/25 scenario)", () => {
    const dto = makeListDTO(25, 0);
    expect(computeSkillCounts(dto)).toEqual({ enabled: 25, total: 25 });
  });
});

describe("useSkills", () => {
  beforeEach(() => {
    _resetSkillsCacheForTest();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    _resetSkillsCacheForTest();
  });

  /** Render a small probe component that exposes counts. */
  function Probe({ onCount }: { onCount: (c: { enabled: number; total: number }) => void }) {
    const { counts, loading } = useSkills();
    if (!loading) onCount(counts);
    return null;
  }

  it("two probe components mount and see identical counts (B4 fix)", async () => {
    const dto = makeListDTO(25, 0);
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify(dto), { status: 200 }),
    );

    let countA: { enabled: number; total: number } | null = null;
    let countB: { enabled: number; total: number } | null = null;
    render(<Probe onCount={(c) => { countA = c; }} />);
    render(<Probe onCount={(c) => { countB = c; }} />);

    await waitFor(() => {
      expect(countA).not.toBeNull();
      expect(countB).not.toBeNull();
    });
    expect(countA).toEqual({ enabled: 25, total: 25 });
    expect(countB).toEqual({ enabled: 25, total: 25 });
    // Two-mounter dedupe: only 1 fetch
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it("uses module cache for second mount (no extra fetch)", async () => {
    const dto = makeListDTO(10, 5);
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify(dto), { status: 200 }),
    );

    // First mount
    let countA: { enabled: number; total: number } | null = null;
    const { unmount: unmountA } = render(<Probe onCount={(c) => { countA = c; }} />);
    await waitFor(() => expect(countA).not.toBeNull());
    expect(fetchMock.mock.calls.length).toBe(1);

    // Unmount
    unmountA();

    // Second mount - should hit cache, no extra HTTP call
    let countB: { enabled: number; total: number } | null = null;
    render(<Probe onCount={(c) => { countB = c; }} />);
    await waitFor(() => expect(countB).not.toBeNull());
    expect(countB).toEqual({ enabled: 10, total: 15 });
    // Still only 1 fetch (cache hit on second mount)
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it("does not crash when fetch fails (returns enabled=0/total=0)", async () => {
    vi.spyOn(global, "fetch").mockRejectedValue(new Error("network"));
    let count: { enabled: number; total: number } | null = null;
    render(<Probe onCount={(c) => { count = c; }} />);
    await waitFor(() => expect(count).not.toBeNull(), { timeout: 1000 });
    expect(count).toEqual({ enabled: 0, total: 0 });
  });

  it("dispatchSkillsChanged invalidates cache and triggers refetch", async () => {
    const dto1 = makeListDTO(10, 0);
    const dto2 = makeListDTO(11, 0);
    const fetchMock = vi.spyOn(global, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(dto1), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(dto2), { status: 200 }));

    let count: { enabled: number; total: number } | null = null;
    render(<Probe onCount={(c) => { count = c; }} />);
    await waitFor(() => expect(count).toEqual({ enabled: 10, total: 10 }));

    act(() => { dispatchSkillsChanged(); });
    await waitFor(() => expect(count).toEqual({ enabled: 11, total: 11 }));
    expect(fetchMock.mock.calls.length).toBe(2);
  });

  it("patchSkill auto-dispatches refresh event (api-level integration)", async () => {
    // Verify api.ts patchSkill fires the same window event that useSkills listens for
    // so /settings/skills page toggle propagates to /profile + /settings overview
    // without those pages needing to opt in.
    const dto1 = makeListDTO(10, 0);
    const dto2 = makeListDTO(11, 0);
    const fetchMock = vi.spyOn(global, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(dto1), { status: 200 }))
      // mock for patchSkill request
      .mockResolvedValueOnce(new Response(JSON.stringify({ name: "x", enabled: true }), { status: 200 }))
      // mock for refresh fetchSkills
      .mockResolvedValueOnce(new Response(JSON.stringify(dto2), { status: 200 }));

    let count: { enabled: number; total: number } | null = null;
    render(<Probe onCount={(c) => { count = c; }} />);
    await waitFor(() => expect(count).toEqual({ enabled: 10, total: 10 }));

    // dynamically import patchSkill to avoid hoisting concerns
    const { patchSkill } = await import("./api");
    await act(async () => { await patchSkill("x", { enabled: true }); });
    await waitFor(() => expect(count).toEqual({ enabled: 11, total: 11 }));
    expect(fetchMock.mock.calls.length).toBe(3);
  });
});
