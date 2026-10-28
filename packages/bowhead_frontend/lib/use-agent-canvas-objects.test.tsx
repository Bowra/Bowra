import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useAgentCanvasObjects } from "./use-agent-canvas-objects";

const pointer = (id: string, version = 1) => ({
  draft_id: id,
  draft_version: version,
  draft_title: id,
  draft_status: "editing",
  purpose_hint: null,
});

describe("retained Agent object pointers", () => {
  it("deduplicates a reopen and routes a background version to its original object", () => {
    const { result } = renderHook(() => useAgentCanvasObjects("user/team/space", true));
    act(() => result.current.open(pointer("sad_a")));
    const original = result.current.objects[0];
    act(() => result.current.bind(original.key, original.incarnation, { draftId: "sad_a", version: 1, dirty: true }));
    act(() => result.current.open(pointer("sad_b")));
    const selected = result.current.selectedKey;
    act(() => result.current.open(pointer("sad_a", 3), false));
    expect(result.current.objects).toHaveLength(2);
    expect(result.current.selectedKey).toBe(selected);
    expect(result.current.objects[0]).toMatchObject({
      incarnation: original.incarnation,
      binding: { dirty: true },
      payload: { draft_version: 3 },
    });
    act(() => result.current.open(pointer("sad_a", 2)));
    expect(result.current.objects[0].payload.draft_version).toBe(3);
    expect(result.current.selectedKey).toBe(original.key);
  });

  it("keeps a closed object dismissed and fences callbacks from its earlier incarnation", () => {
    const { result } = renderHook(() => useAgentCanvasObjects("scope", true));
    act(() => result.current.open(pointer("sad_a")));
    const old = result.current.objects[0];
    act(() => result.current.close(old.key, old.incarnation));
    act(() => result.current.open(pointer("sad_a", 2), false));
    expect(result.current.objects).toHaveLength(0);
    act(() => result.current.open(pointer("sad_a")));
    const reopened = result.current.objects[0];
    expect(reopened.incarnation).not.toBe(old.incarnation);
    act(() => result.current.bind(old.key, old.incarnation, { draftId: "sad_a", version: 8, dirty: false }));
    act(() => result.current.close(old.key, old.incarnation));
    expect(result.current.objects[0]).toBe(reopened);
    expect(result.current.objects[0].binding).toBeNull();
  });

  it("clears private pointers immediately when the caller or scope changes", () => {
    const { result, rerender } = renderHook(({ scope }) => useAgentCanvasObjects(scope, true), {
      initialProps: { scope: "owner/team/space" },
    });
    act(() => result.current.open(pointer("sad_a")));
    const old = result.current.objects[0];
    rerender({ scope: "different-user/team/space" });
    expect(result.current.objects).toHaveLength(0);
    act(() => result.current.bind(old.key, old.incarnation, { draftId: "sad_a", version: 2, dirty: true }));
    expect(result.current.objects).toHaveLength(0);
  });

  it("closes the original object using selection at completion instead of the earlier intent", () => {
    const { result } = renderHook(() => useAgentCanvasObjects("scope", true));
    act(() => result.current.open(pointer("sad_a")));
    const original = result.current.objects[0];
    const close = result.current.close;
    act(() => result.current.open(pointer("sad_b")));
    act(() => result.current.open(pointer("sad_c")));
    let receipt: ReturnType<typeof close>;
    act(() => { receipt = close(original.key, original.incarnation); });
    expect(receipt!).toEqual({ selectionChanged: false, selectedDraftId: "sad_c" });
    expect(result.current.selectedKey).toBe(result.current.objects[1].key);
  });

  it("returns to the last selected surviving object rather than its adjacent Tab", () => {
    const { result } = renderHook(() => useAgentCanvasObjects("scope", true));
    act(() => result.current.open(pointer("sad_a")));
    act(() => result.current.open(pointer("sad_b")));
    act(() => result.current.open(pointer("sad_c")));
    const [a, , c] = result.current.objects;
    act(() => result.current.select(a.key));
    act(() => result.current.select(c.key));
    act(() => result.current.close(c.key, c.incarnation));
    expect(result.current.selectedKey).toBe(a.key);
  });
});
