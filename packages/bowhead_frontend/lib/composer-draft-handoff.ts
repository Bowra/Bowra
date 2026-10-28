/** Same-document ordering only. IndexedDB CAS remains the cross-tab authority. */
type Handoff = {
  attempt: Promise<void>;
  failed: boolean;
  finish: (retry: boolean) => Promise<void>;
};

const handoffs = new Map<string, Handoff>();
const HANDOFF_WAIT_MS = 5_000;

export class ComposerDraftHandoffError extends Error {
  constructor() {
    super("composer predecessor draft has not settled");
    this.name = "ComposerDraftHandoffError";
  }
}

function start(signature: string, handoff: Handoff, retry: boolean): void {
  handoff.failed = false;
  handoff.attempt = Promise.resolve().then(() => handoff.finish(retry)).then(
    () => { if (handoffs.get(signature) === handoff) handoffs.delete(signature); },
    () => { handoff.failed = true; throw new ComposerDraftHandoffError(); },
  );
  // Unmount cannot await; the next reader owns presentation and explicit retry.
  void handoff.attempt.catch(() => undefined);
}

export function registerComposerDraftHandoff(signature: string, finish: (retry: boolean) => Promise<void>): void {
  const previous = handoffs.get(signature);
  const handoff: Handoff = {
    attempt: Promise.resolve(),
    failed: false,
    finish: async (retry) => {
      if (previous) {
        if (retry && previous.failed) start(signature, previous, true);
        await previous.attempt;
      }
      await finish(retry);
    },
  };
  handoffs.set(signature, handoff);
  start(signature, handoff, false);
}

export function retryComposerDraftHandoff(signature: string): void {
  const handoff = handoffs.get(signature);
  if (handoff?.failed) start(signature, handoff, true);
}

export async function waitForComposerDraftHandoff(signature: string): Promise<void> {
  if (!handoffs.has(signature)) return;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      (async () => {
        // A second unmount may append another tail while this reader waits.
        while (handoffs.has(signature)) await handoffs.get(signature)!.attempt;
      })(),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new ComposerDraftHandoffError()), HANDOFF_WAIT_MS);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
