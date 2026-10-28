const PREFIX = "bowhead:composer-cancel:v4";
const CANCEL_NAMESPACE = "0de3db36-d292-5fb8-80e2-eac393019ff4";

export const EXPLICIT_STOP_SOURCE = "explicit_stop" as const;

export type PendingCancelState =
  | "posting"
  | "reconciling"
  | "accepted_pending_terminal";

export type PendingCancelRequest = {
  teamId: string;
  userId: string;
  turnId: string;
  executionGeneration: number;
  cancelRequestId: string;
  source: typeof EXPLICIT_STOP_SOURCE;
  state: PendingCancelState;
  revision: number;
};

const volatileRecords = new Map<string, PendingCancelRequest>();

function rotateLeft(value: number, shift: number): number {
  return ((value << shift) | (value >>> (32 - shift))) >>> 0;
}

function sha1(input: Uint8Array): Uint8Array {
  const bitLength = input.length * 8;
  const paddedLength = Math.ceil((input.length + 9) / 64) * 64;
  const bytes = new Uint8Array(paddedLength);
  bytes.set(input);
  bytes[input.length] = 0x80;
  const view = new DataView(bytes.buffer);
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 0x1_0000_0000));
  view.setUint32(paddedLength - 4, bitLength >>> 0);

  let h0 = 0x67452301;
  let h1 = 0xefcdab89;
  let h2 = 0x98badcfe;
  let h3 = 0x10325476;
  let h4 = 0xc3d2e1f0;
  const words = new Uint32Array(80);
  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      words[index] = view.getUint32(offset + index * 4);
    }
    for (let index = 16; index < 80; index += 1) {
      words[index] = rotateLeft(
        words[index - 3] ^ words[index - 8] ^ words[index - 14] ^ words[index - 16],
        1,
      );
    }
    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    for (let index = 0; index < 80; index += 1) {
      let f: number;
      let k: number;
      if (index < 20) {
        f = (b & c) | (~b & d);
        k = 0x5a827999;
      } else if (index < 40) {
        f = b ^ c ^ d;
        k = 0x6ed9eba1;
      } else if (index < 60) {
        f = (b & c) | (b & d) | (c & d);
        k = 0x8f1bbcdc;
      } else {
        f = b ^ c ^ d;
        k = 0xca62c1d6;
      }
      const next = (rotateLeft(a, 5) + f + e + k + words[index]) >>> 0;
      e = d;
      d = c;
      c = rotateLeft(b, 30);
      b = a;
      a = next;
    }
    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
  }
  const digest = new Uint8Array(20);
  const digestView = new DataView(digest.buffer);
  [h0, h1, h2, h3, h4].forEach((word, index) => {
    digestView.setUint32(index * 4, word);
  });
  return digest;
}

function namespaceBytes(): Uint8Array {
  const hex = CANCEL_NAMESPACE.replaceAll("-", "");
  return Uint8Array.from(
    Array.from({ length: 16 }, (_, index) =>
      Number.parseInt(hex.slice(index * 2, index * 2 + 2), 16),
    ),
  );
}

function lengthPrefix(value: string): Uint8Array {
  const encoded = new TextEncoder().encode(value);
  if (encoded.length > 0xffff) throw new Error("cancel identity field is too long");
  return Uint8Array.of(encoded.length >>> 8, encoded.length & 0xff, ...encoded);
}

function cancelName(
  teamId: string,
  userId: string,
  turnId: string,
  executionGeneration: number,
): string {
  const fields = [
    teamId,
    userId,
    turnId,
    String(executionGeneration),
    EXPLICIT_STOP_SOURCE,
  ];
  const raw = Uint8Array.from(fields.flatMap((field) => [...lengthPrefix(field)]));
  let binary = "";
  for (const byte of raw) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

export function deterministicCancelRequestId(
  teamId: string,
  userId: string,
  turnId: string,
  executionGeneration: number,
): string {
  if (!Number.isSafeInteger(executionGeneration) || executionGeneration < 1) {
    throw new Error("execution generation must be a positive safe integer");
  }
  const name = new TextEncoder().encode(
    cancelName(teamId, userId, turnId, executionGeneration),
  );
  const material = new Uint8Array(16 + name.length);
  material.set(namespaceBytes());
  material.set(name, 16);
  const bytes = sha1(material).slice(0, 16);
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function storageKey(cancelRequestId: string): string {
  return `${PREFIX}:${cancelRequestId}`;
}

function validRecord(value: unknown): value is PendingCancelRequest {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<PendingCancelRequest>;
  if (
    !item.teamId
    || !item.userId
    || !item.turnId
    || item.source !== EXPLICIT_STOP_SOURCE
    || !item.cancelRequestId
    || !Number.isSafeInteger(item.executionGeneration)
    || Number(item.executionGeneration) < 1
    || !Number.isSafeInteger(item.revision)
    || Number(item.revision) < 0
    || !["posting", "reconciling", "accepted_pending_terminal"].includes(
      String(item.state),
    )
  ) return false;
  return item.cancelRequestId === deterministicCancelRequestId(
    item.teamId,
    item.userId,
    item.turnId,
    Number(item.executionGeneration),
  );
}

export function getOrCreateCancelRequest(
  teamId: string,
  userId: string,
  turnId: string,
  executionGeneration: number,
): PendingCancelRequest {
  const cancelRequestId = deterministicCancelRequestId(
    teamId,
    userId,
    turnId,
    executionGeneration,
  );
  const key = storageKey(cancelRequestId);
  try {
    const existing = JSON.parse(window.localStorage.getItem(key) ?? "null") as unknown;
    if (validRecord(existing)) {
      volatileRecords.delete(cancelRequestId);
      return existing;
    }
  } catch {
    // Replace corrupt state with the deterministic journal below.
  }
  const record: PendingCancelRequest = {
    teamId,
    userId,
    turnId,
    executionGeneration,
    cancelRequestId,
    source: EXPLICIT_STOP_SOURCE,
    state: "posting",
    revision: 0,
  };
  try {
    window.localStorage.setItem(key, JSON.stringify(record));
    volatileRecords.delete(cancelRequestId);
  } catch {
    // The deterministic identity still makes this page's request idempotent.
    volatileRecords.set(cancelRequestId, record);
  }
  return record;
}

/**
 * Advance one exact journal revision. Missing/stale durable state is terminal:
 * never recreate it after an SSE terminal handler has removed the record.
 */
export function compareAndSetCancelRequestState(
  record: PendingCancelRequest,
  state: PendingCancelState,
): PendingCancelRequest | null {
  const next = { ...record, state, revision: record.revision + 1 };
  try {
    const stored = JSON.parse(
      window.localStorage.getItem(storageKey(record.cancelRequestId)) ?? "null",
    ) as unknown;
    if (!validRecord(stored)) {
      const volatile = volatileRecords.get(record.cancelRequestId);
      if (!volatile || volatile.revision !== record.revision) return null;
      volatileRecords.set(record.cancelRequestId, next);
      return next;
    }
    if (
      stored.cancelRequestId !== record.cancelRequestId
      || stored.revision !== record.revision
    ) return null;
    window.localStorage.setItem(storageKey(record.cancelRequestId), JSON.stringify(next));
    volatileRecords.delete(record.cancelRequestId);
  } catch {
    // Server receipt remains authoritative when journal persistence is blocked.
    const volatile = volatileRecords.get(record.cancelRequestId);
    if (!volatile || volatile.revision !== record.revision) return null;
    volatileRecords.set(record.cancelRequestId, next);
  }
  return next;
}

export function listPendingCancelRequests(
  teamId: string,
  userId: string,
): PendingCancelRequest[] {
  const pending = new Map<string, PendingCancelRequest>();
  for (const value of volatileRecords.values()) {
    if (validRecord(value) && value.teamId === teamId && value.userId === userId) {
      pending.set(value.cancelRequestId, value);
    }
  }
  try {
    const prefix = `${PREFIX}:`;
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (!key?.startsWith(prefix)) continue;
      try {
        const value = JSON.parse(window.localStorage.getItem(key) ?? "null") as unknown;
        if (validRecord(value) && value.teamId === teamId && value.userId === userId) {
          pending.set(value.cancelRequestId, value);
        }
      } catch {
        // Ignore corrupt records; never invent a cancel identity from them.
      }
    }
  } catch {
    // A browser may expose localStorage while denying every operation. The
    // page-local journal must remain visible so terminal facts can clear it.
  }
  return [...pending.values()];
}

export function clearCancelRequest(record: PendingCancelRequest): void {
  volatileRecords.delete(record.cancelRequestId);
  try {
    window.localStorage.removeItem(storageKey(record.cancelRequestId));
  } catch {
    // A terminal server fact is authoritative even if local cleanup fails.
  }
}
