import type { EffectiveThemeResponse } from "@/lib/theme-config";

export interface EffectiveThemeRequestToken {
  epoch: number;
  expectedTeamId: string | null;
}

export interface EffectiveThemeRequest {
  token: EffectiveThemeRequestToken;
  signal: AbortSignal;
  complete: () => void;
}

let currentEpoch = 0;
let verifiedTeamId: string | null = null;
let transitionTeamId: string | null = null;
const activeControllers = new Set<AbortController>();

export function beginBrandTransition(teamId: string): number {
  const normalizedTeamId = normalizeTeamId(teamId);
  currentEpoch += 1;
  transitionTeamId = normalizedTeamId;
  abortActiveRequests();
  return currentEpoch;
}

export function beginEffectiveThemeRequest(
  expectedTeamId?: string | null,
): EffectiveThemeRequest {
  const controller = new AbortController();
  activeControllers.add(controller);
  const explicitExpectedTeamId = normalizeTeamId(expectedTeamId);
  const token: EffectiveThemeRequestToken = {
    epoch: currentEpoch,
    expectedTeamId:
      explicitExpectedTeamId ?? transitionTeamId ?? verifiedTeamId,
  };
  let active = true;
  return {
    token,
    signal: controller.signal,
    complete: () => {
      if (!active) return;
      active = false;
      activeControllers.delete(controller);
    },
  };
}

export function isEffectiveThemeRequestCurrent(
  token: EffectiveThemeRequestToken,
): boolean {
  return token.epoch === currentEpoch;
}

export function acceptEffectiveThemeResponse(
  token: EffectiveThemeRequestToken,
  response: EffectiveThemeResponse,
): boolean {
  if (!isEffectiveThemeRequestCurrent(token)) return false;
  const responseTeamId = normalizeTeamId(response.team?.id);
  if (
    token.expectedTeamId &&
    responseTeamId !== token.expectedTeamId
  ) {
    return false;
  }
  if (transitionTeamId && responseTeamId !== transitionTeamId) {
    return false;
  }
  if (responseTeamId) {
    verifiedTeamId = responseTeamId;
    if (transitionTeamId === responseTeamId) transitionTeamId = null;
  }
  return true;
}

export function getVerifiedEffectiveThemeTeamId(): string | null {
  return verifiedTeamId;
}

export function resetEffectiveThemeRequestGuardForTests(): void {
  currentEpoch = 0;
  verifiedTeamId = null;
  transitionTeamId = null;
  abortActiveRequests();
}

function abortActiveRequests(): void {
  for (const controller of activeControllers) controller.abort();
  activeControllers.clear();
}

function normalizeTeamId(
  teamId: string | null | undefined,
): string | null {
  const normalized = teamId?.trim();
  return normalized || null;
}
