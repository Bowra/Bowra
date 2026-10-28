import { expect, it } from "vitest";
import { requestConfirm, settleConfirm, subscribeConfirm, type PendingConfirm } from "./confirm-dialog-store";

it("cancels a superseded confirmation without approving the next queued action", async () => {
  let current: PendingConfirm | null = null;
  const unsubscribe = subscribeConfirm((value) => {
    current = value;
  });
  try {
    const original = new AbortController();
    const first = requestConfirm({ title: "Leave draft", confirmLabel: "Leave" }, original.signal);
    const firstId = current!.id;
    const second = requestConfirm({ title: "Close settings", confirmLabel: "Close" });
    original.abort();
    await expect(first).resolves.toBe(false);
    expect(current!.title).toBe("Close settings");
    settleConfirm(firstId, true);
    expect(current!.title).toBe("Close settings");
    settleConfirm(current!.id, false);
    await expect(second).resolves.toBe(false);
  } finally {
    unsubscribe();
  }
});

it("cancels pending decisions when their dialog host disappears", async () => {
  const unsubscribe = subscribeConfirm(() => {});
  const controller = new AbortController();
  const decision = requestConfirm({ title: "Discard local input", confirmLabel: "Discard" }, controller.signal);
  unsubscribe();
  controller.abort();
  await expect(decision).resolves.toBe(false);
});
