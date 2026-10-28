// Shared upgrade barrier: keep the name, keys, stores and unresolved fences.
// Released v2/v3/v4 tabs must stop before an exact document revision is stored.
const COMPOSER_LOCAL_DB_NAME = "bowhead-composer-recovery-v1";
const COMPOSER_LOCAL_DB_VERSION = 5;

export const COMPOSER_SUBMISSION_STORES = [
  "identities",
  "principals",
  "submission_fence_ledgers",
  "submission_fences",
  "manifests",
  "draft_blobs",
  "ingress_blobs",
  "content_chunk_meta",
  "content_chunk_payload",
] as const;
export const COMPOSER_ORDINARY_DRAFT_STORE = "ordinary_drafts";
const COMPOSER_LOCAL_SCHEMA_STORES = [
  ...COMPOSER_SUBMISSION_STORES,
  COMPOSER_ORDINARY_DRAFT_STORE,
] as const;

export class ComposerRecoveryUnavailableError extends Error {
  constructor(message = "composer recovery storage unavailable") {
    super(message);
    this.name = "ComposerRecoveryUnavailableError";
  }
}

/** An incompatible/blocked schema is never a storage-unavailable send bypass. */
export class ComposerPersistenceVersionError extends Error {
  constructor(message = "composer persistence requires a compatible client") {
    super(message);
    this.name = "ComposerPersistenceVersionError";
  }
}

export function composerPersistenceNeedsUpgrade(error: unknown): boolean {
  return error instanceof ComposerPersistenceVersionError
    || (error instanceof DOMException && error.name === "VersionError");
}

const COMPOSER_PERSISTENCE_UNAVAILABLE_DOM_ERRORS = new Set([
  "AbortError",
  "InvalidStateError",
  "NotAllowedError",
  "NotReadableError",
  "QuotaExceededError",
  "SecurityError",
  "TimeoutError",
  "UnknownError",
]);

export function composerRecoveryUnavailable(error: unknown): Error {
  if (error instanceof ComposerRecoveryUnavailableError) return error;
  if (
    error instanceof DOMException
    && COMPOSER_PERSISTENCE_UNAVAILABLE_DOM_ERRORS.has(error.name)
  ) {
    return new ComposerRecoveryUnavailableError(error.message);
  }
  // VersionError means a newer schema may contain an unresolved action;
  // DataError and unknown failures can likewise indicate invalid state.  They
  // remain untyped so callers fail closed instead of sending around it.
  return error instanceof Error || error instanceof DOMException
    ? error
    : new Error("composer persistence failed");
}

export function requestValue<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error
      ? composerRecoveryUnavailable(request.error)
      : new ComposerRecoveryUnavailableError("indexedDB request failed"));
  });
}

export function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error
      ? composerRecoveryUnavailable(transaction.error)
      : new ComposerRecoveryUnavailableError("indexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error
      ? composerRecoveryUnavailable(transaction.error)
      : new ComposerRecoveryUnavailableError("indexedDB transaction aborted"));
  });
}

// Wait for the previous open request, not its connection: each caller still
// owns/closes its own database. Keep a blocked request here until it settles so
// another reader cannot wait forever behind the same abandoned upgrade.
let pendingOpen: Promise<IDBDatabase> | null = null;

export async function openComposerLocalPersistenceDB(): Promise<IDBDatabase> {
  if (pendingOpen) await pendingOpen;
  if (typeof indexedDB === "undefined") {
    throw new ComposerRecoveryUnavailableError();
  }
  let request: IDBOpenDBRequest;
  try {
    request = indexedDB.open(COMPOSER_LOCAL_DB_NAME, COMPOSER_LOCAL_DB_VERSION);
  } catch (error) {
    throw composerRecoveryUnavailable(error);
  }
  const opening = new Promise<IDBDatabase>((resolve, reject) => {
    let abandoned = false;
    request.onupgradeneeded = () => {
      // A blocked open cannot be cancelled. Do not perform a later upgrade
      // after its caller already received a rejection and retained the draft.
      if (abandoned) {
        request.transaction?.abort();
        return;
      }
      const db = request.result;
      for (const store of COMPOSER_LOCAL_SCHEMA_STORES) {
        if (!db.objectStoreNames.contains(store)) db.createObjectStore(store);
      }
    };
    request.onsuccess = () => {
      if (pendingOpen === opening) pendingOpen = null;
      const db = request.result;
      if (abandoned) {
        db.close();
        return;
      }
      db.onversionchange = () => db.close();
      resolve(db);
    };
    request.onerror = () => {
      if (pendingOpen === opening) pendingOpen = null;
      reject(request.error
        ? composerRecoveryUnavailable(request.error)
        : new ComposerRecoveryUnavailableError("composer recovery open failed"));
    };
    request.onblocked = () => {
      abandoned = true;
      reject(new ComposerPersistenceVersionError("composer recovery upgrade blocked"));
    };
  });
  pendingOpen = opening;
  return opening;
}
