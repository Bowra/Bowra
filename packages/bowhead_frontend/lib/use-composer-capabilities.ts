"use client";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import type { MeUser } from "@/lib/auth-api";

export interface ComposerCapabilities {
  planning: boolean;
}

const BASE_CAPABILITIES: ComposerCapabilities = Object.freeze({
  planning: false,
});

export function composerCapabilitiesForUser(
  me: MeUser | null | undefined,
): ComposerCapabilities {
  if (!me) return BASE_CAPABILITIES;
  return { planning: me.capabilities?.planning === true };
}

export function useComposerCapabilities(): ComposerCapabilities {
  const session = useOptionalAuthSession();
  return composerCapabilitiesForUser(session?.me);
}
