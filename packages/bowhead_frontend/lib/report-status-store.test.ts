import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cachedReportStatus,
  invalidateReportStatus,
  REPORT_STATUS_REUSE_MS,
  requestReportStatus,
  resetReportStatusStoreForTests,
  subscribeReportStatus,
  type ReportStatusSnapshot,
} from "./report-status-store";

const ACTIVE: ReportStatusSnapshot = { status: "active", effectiveStatus: "active", quarantinedCount: 0 };
const ARCHIVED: ReportStatusSnapshot = { status: "archived", effectiveStatus: "archived", quarantinedCount: 0 };

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  let reject: (error: unknown) => void = () => {};
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  resetReportStatusStoreForTests();
  vi.useRealTimers();
});

describe("report status store", () => {
  it("a forced read after a canvas change never joins a GET sent before the change", async () => {
    const beforeChange = deferred<ReportStatusSnapshot>();
    const load = vi.fn().mockImplementationOnce(() => beforeChange.promise).mockResolvedValue(ARCHIVED);
    const listener = vi.fn();
    subscribeReportStatus("cv_1", listener);
    void requestReportStatus("cv_1", load);
    // A turn quarantines content while the first read is in flight; two readers react to the same dispatch.
    const event = new Event("bowhead.canvas.changed");
    window.dispatchEvent(event);
    const first = requestReportStatus("cv_1", load, { force: true, cause: event });
    const second = requestReportStatus("cv_1", load, { force: true, cause: event });
    expect(load).toHaveBeenCalledTimes(2);
    expect(await first).toEqual(ARCHIVED);
    expect(await second).toEqual(ARCHIVED);
    // The pre-change answer lands late and must not overwrite the newer status.
    beforeChange.resolve(ACTIVE);
    await beforeChange.promise;
    expect(cachedReportStatus("cv_1")).toEqual(ARCHIVED);
    expect(listener.mock.calls).toEqual([[ARCHIVED]]);
  });

  it("forgets a cached status changed while no reader is mounted", async () => {
    const load = vi.fn().mockResolvedValueOnce(ACTIVE).mockResolvedValueOnce(ARCHIVED);
    await requestReportStatus("cv_1", load);
    expect(cachedReportStatus("cv_1")).toEqual(ACTIVE);
    // E.g. the reports list archives it and broadcasts the change.
    window.dispatchEvent(new Event("bowhead.canvas.changed"));
    expect(cachedReportStatus("cv_1")).toBeNull();
    expect(await requestReportStatus("cv_1", load)).toEqual(ARCHIVED);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("does not offer a result older than the reuse window for first paint", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    await requestReportStatus("cv_1", async () => ACTIVE);
    expect(cachedReportStatus("cv_1")).toEqual(ACTIVE);
    vi.setSystemTime(Date.now() + REPORT_STATUS_REUSE_MS + 1);
    expect(cachedReportStatus("cv_1")).toBeNull();
  });

  it("shares one in-flight request within one change generation, including forced reads", async () => {
    const pending = deferred<ReportStatusSnapshot>();
    const load = vi.fn(() => pending.promise);
    const first = requestReportStatus("cv_1", load);
    const second = requestReportStatus("cv_1", load);
    const forced = requestReportStatus("cv_1", load, { force: true });
    pending.resolve(ACTIVE);
    expect(await Promise.all([first, second, forced])).toEqual([ACTIVE, ACTIVE, ACTIVE]);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("reuses a recent result, refetches after the reuse window or when forced", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const load = vi.fn(async () => ACTIVE);
    await requestReportStatus("cv_1", load);
    await requestReportStatus("cv_1", load);
    expect(load).toHaveBeenCalledTimes(1);
    await requestReportStatus("cv_1", load, { force: true });
    expect(load).toHaveBeenCalledTimes(2);
    vi.setSystemTime(Date.now() + REPORT_STATUS_REUSE_MS + 1);
    await requestReportStatus("cv_1", load);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("drops a read that started before an invalidation", async () => {
    const stale = deferred<ReportStatusSnapshot>();
    const listener = vi.fn();
    subscribeReportStatus("cv_1", listener);
    void requestReportStatus("cv_1", () => stale.promise);
    invalidateReportStatus("cv_1");
    const fresh = requestReportStatus("cv_1", async () => ARCHIVED);
    expect(await fresh).toEqual(ARCHIVED);
    stale.resolve(ACTIVE);
    await stale.promise;
    expect(cachedReportStatus("cv_1")).toEqual(ARCHIVED);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith(ARCHIVED);
  });

  it("lets the next read retry after a failure", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(ACTIVE);
    await expect(requestReportStatus("cv_1", load)).rejects.toThrow("offline");
    expect(await requestReportStatus("cv_1", load)).toEqual(ACTIVE);
    expect(load).toHaveBeenCalledTimes(2);
  });
});
