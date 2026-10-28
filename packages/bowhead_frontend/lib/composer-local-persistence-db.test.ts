import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ComposerRecoveryUnavailableError,
  ComposerPersistenceVersionError,
  composerPersistenceNeedsUpgrade,
  openComposerLocalPersistenceDB,
} from "./composer-local-persistence-db";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Composer local persistence error boundary", () => {
  it("classifies a synchronous browser access denial as unavailable", async () => {
    vi.stubGlobal("indexedDB", {
      open: () => {
        throw new DOMException("storage denied", "SecurityError");
      },
    });

    await expect(openComposerLocalPersistenceDB()).rejects.toBeInstanceOf(
      ComposerRecoveryUnavailableError,
    );
  });

  it("keeps a future database version fail-closed", async () => {
    const versionError = new DOMException("future schema", "VersionError");
    const request = { error: versionError } as IDBOpenDBRequest;
    vi.stubGlobal("indexedDB", { open: () => request });

    const opening = openComposerLocalPersistenceDB();
    request.onerror?.(new Event("error"));

    await expect(opening).rejects.toBe(versionError);
    await expect(opening).rejects.not.toBeInstanceOf(ComposerRecoveryUnavailableError);
  });

  it("fences a blocked upgrade and abandons its delayed upgrade/connection", async () => {
    const close = vi.fn();
    const abort = vi.fn();
    const request = {
      result: { close },
      transaction: { abort },
    } as unknown as IDBOpenDBRequest;
    vi.stubGlobal("indexedDB", { open: () => request });
    const opening = openComposerLocalPersistenceDB();
    request.onblocked?.(new Event("blocked") as IDBVersionChangeEvent);
    await expect(opening).rejects.toBeInstanceOf(ComposerPersistenceVersionError);
    await expect(opening).rejects.not.toBeInstanceOf(ComposerRecoveryUnavailableError);

    request.onupgradeneeded?.(new Event("upgradeneeded") as IDBVersionChangeEvent);
    expect(abort).toHaveBeenCalledOnce();
    request.onsuccess?.(new Event("success"));
    expect(close).toHaveBeenCalledOnce();
  });

  it("closes a successful connection when another tab upgrades", async () => {
    const db = { close: vi.fn() } as unknown as IDBDatabase;
    const request = { result: db } as IDBOpenDBRequest;
    vi.stubGlobal("indexedDB", { open: () => request });
    const opening = openComposerLocalPersistenceDB();
    request.onsuccess?.(new Event("success"));
    expect(await opening).toBe(db);
    db.onversionchange?.(new Event("versionchange") as IDBVersionChangeEvent);
    expect(db.close).toHaveBeenCalledOnce();
  });

  it("distinguishes incompatible versions from ordinary storage failures", () => {
    expect(composerPersistenceNeedsUpgrade(new ComposerPersistenceVersionError())).toBe(true);
    expect(composerPersistenceNeedsUpgrade(new DOMException("future", "VersionError"))).toBe(true);
    expect(composerPersistenceNeedsUpgrade(new ComposerRecoveryUnavailableError())).toBe(false);
  });

  it("shares a blocked open failure until the browser finishes aborting it", async () => {
    const blocked = {} as IDBOpenDBRequest;
    const retry = { result: { close: vi.fn() } } as unknown as IDBOpenDBRequest;
    const open = vi.fn().mockReturnValueOnce(blocked).mockReturnValueOnce(retry);
    vi.stubGlobal("indexedDB", { open });
    const results = Promise.allSettled([
      openComposerLocalPersistenceDB(),
      openComposerLocalPersistenceDB(),
    ]);
    expect(open).toHaveBeenCalledTimes(1);
    blocked.onblocked?.(new Event("blocked") as IDBVersionChangeEvent);
    for (const result of await results) {
      expect(result.status).toBe("rejected");
      if (result.status === "rejected")
        expect(result.reason).toBeInstanceOf(ComposerPersistenceVersionError);
    }
    await expect(openComposerLocalPersistenceDB()).rejects.toBeInstanceOf(
      ComposerPersistenceVersionError,
    );
    expect(open).toHaveBeenCalledTimes(1);
    blocked.onerror?.(new Event("error"));
    const reopening = openComposerLocalPersistenceDB();
    expect(open).toHaveBeenCalledTimes(2);
    retry.onsuccess?.(new Event("success"));
    expect(await reopening).toBe(retry.result);
  });
});
