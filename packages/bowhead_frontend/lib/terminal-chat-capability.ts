import type { ChatExecutionBootstrap } from "./auth-api";
import type { AttachmentRef } from "./types";

export const TERMINAL_CHAT_CAPABILITY_HEADERS = {
  "X-Bowhead-Terminal-Protocol-Capability": "1",
} as const;

export function maySkipChatExecutionPreflight(
  requestedSessionId: string | null,
  hasExactSessionHint: boolean,
  bootstrapHint: ChatExecutionBootstrap | null,
): boolean {
  return (!requestedSessionId || hasExactSessionHint)
    && (!bootstrapHint || bootstrapHint.write_protocol === "legacy_v0");
}

export function shouldRouteSecondaryChatEntryThroughComposer(
  requestedSessionId: string | null,
  hasExactSessionHint: boolean,
  bootstrapHint: ChatExecutionBootstrap | null,
): boolean {
  return !maySkipChatExecutionPreflight(
    requestedSessionId,
    hasExactSessionHint,
    bootstrapHint,
  );
}

export function chatReplayAdmission(
  bootstrap: ChatExecutionBootstrap | null,
): "legacy_v0" | "refresh_required" | "unavailable" {
  if (bootstrap?.write_protocol === "legacy_v0") return "legacy_v0";
  if (bootstrap?.write_protocol === "refresh_required") return "refresh_required";
  return "unavailable";
}

export function terminalChatActionAttachmentsSupported(
  actionKind: "start" | "steer",
  attachments: readonly AttachmentRef[],
  bootstrap: ChatExecutionBootstrap | null,
): boolean {
  if (attachments.length === 0) return true;
  return actionKind === "start"
    && bootstrap?.attachment_refs_wire_version === 1;
}

export function legacyChatActionAttachmentsSupported(
  actionKind: "start" | "steer",
  attachments: readonly AttachmentRef[],
  submissionProtocolEnabled: boolean,
): boolean {
  return attachments.length === 0
    || actionKind === "start"
    || submissionProtocolEnabled;
}
