import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import { useCoachOperationViews } from "./use-coach-operation-views";
import type { CoachOperationOpen } from "./coach-operations";

const pointer = (id: string, requestId = id): CoachOperationOpen => ({
  requestId,
  turnId: "t_original",
  userId: "u_original",
  teamId: "team_original",
  workspaceId: "ws_original",
  sessionId: "s_original",
  reference: { kind: "draft_record", draft_id: id },
});

it("keeps another selected object when an original result arrives and does not reopen closed replay", () => {
  const { result } = renderHook(() =>
    useCoachOperationViews("owner/team/space/chat"),
  );
  act(() => result.current.open(pointer("sad_a"), true));
  act(() => result.current.open(pointer("sad_b"), true));
  const [a, b] = result.current.objects;
  act(() => result.current.open(pointer("sad_a", "later"), false));
  expect(result.current.selectedKey).toBe(b.key);
  expect(result.current.objects).toHaveLength(2);
  act(() => result.current.close(a.key));
  act(() => result.current.open(pointer("sad_a", "later"), false));
  expect(result.current.objects).toHaveLength(1);
  act(() => result.current.open(pointer("sad_a", "explicit-reopen"), false));
  expect(result.current.objects).toHaveLength(2);
  expect(result.current.selectedKey).toBe(b.key);
});

it("clears pointers on Chat or account changes and ignores old close callbacks", () => {
  const { result, rerender } = renderHook(
    ({ scope }) => useCoachOperationViews(scope),
    { initialProps: { scope: "original" } },
  );
  act(() => result.current.open(pointer("sad_a"), true));
  const original = result.current.objects[0];
  rerender({ scope: "different" });
  expect(result.current.objects).toHaveLength(0);
  act(() => result.current.open(pointer("sad_b"), true));
  act(() => result.current.close(original.key));
  expect(result.current.objects).toHaveLength(1);
  rerender({ scope: "original" });
  expect(result.current.objects).toHaveLength(0);
});
