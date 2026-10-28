"use client";

/**
 * Partial-answer footer with Continue (agent run limits, phase 2 §6; mockup "Agent Run Limits Spec
 * Part 2" D2).
 *
 * Shown under the latest answer when a helper stopped at a run limit and saved its work. The footer
 * says which agent stopped at which limit, so the user does not depend on Lead's wording. Continue
 * sends a normal, visible message that carries the saved runs' checkpoint ids, so the server resumes
 * exactly those runs instead of leaving the choice to the model.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { CircleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { PeerTerminal } from "@/lib/turn-store";
import { agentDisplayName } from "./step-list/agent-meta";

const STOP_REASONS = new Set(["step_limit", "time_limit", "tool_call_limit", "no_progress", "budget"]);

export interface ContinueTarget {
  checkpointId: string;
  agent: string;
  stopReason: string;
}

/** Helpers that stopped partway in this turn and saved their work, in settle order (at most four). */
export function continueTargets(
  peers: Record<string, PeerTerminal> | undefined,
  peerNames: Record<string, string> | undefined = {},
  peerDisplayNames: Record<string, string> | undefined = {},
): ContinueTarget[] {
  return Object.entries(peers ?? {})
    .filter(([, peer]) => peer.status === "partial" && typeof peer.resumeCheckpointId === "string")
    .sort(([, a], [, b]) => (a.evtSeq ?? 0) - (b.evtSeq ?? 0))
    .slice(0, 4)
    .map(([peerId, peer]) => ({
      checkpointId: peer.resumeCheckpointId as string,
      agent: agentDisplayName(peerNames?.[peerId], peerDisplayNames?.[peerId]),
      stopReason: peer.stopReason ?? "",
    }));
}

export function ContinueAction({
  targets,
  onContinue,
}: {
  targets: ContinueTarget[];
  onContinue: (checkpointIds: string[]) => void | Promise<boolean>;
}) {
  const t = useTranslations("chat");
  const [sending, setSending] = useState(false);
  if (targets.length === 0) return null;
  const [first] = targets;
  const reason = t(`partial_footer.reason.${STOP_REASONS.has(first.stopReason) ? first.stopReason : "other"}`);
  const body =
    targets.length === 1
      ? t("partial_footer.body", { agent: first.agent, reason })
      : t("partial_footer.body_many", { count: targets.length });
  return (
    <div className="bh-partial-footer" role="status">
      <CircleAlert size={14} aria-hidden className="bh-partial-footer__icon" />
      <div className="bh-partial-footer__text">
        <b>{t("partial_footer.title")}</b>
        <span>{body}</span>
      </div>
      <Button
        variant="primary"
        size="sm"
        disabled={sending}
        loading={sending}
        onClick={async () => {
          setSending(true);
          try {
            // A refused send (busy session, blocked composer) lets the user try again.
            const sent = await onContinue(targets.map((target) => target.checkpointId));
            if (sent === false) setSending(false);
          } catch {
            setSending(false);
          }
        }}
      >
        {t("partial_footer.continue")}
      </Button>
    </div>
  );
}
