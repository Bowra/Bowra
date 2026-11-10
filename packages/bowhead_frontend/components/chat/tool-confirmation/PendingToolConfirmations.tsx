"use client";

/**
 * Pending confirmation cards for the running band's collapsed state. While a turn
 * runs, the band replaces the step trace, so a card attached to its step would
 * stay hidden until the user expands the band. This renders the same card
 * directly below the band instead; once the trace is shown (expanded in place,
 * inline, archived or replayed) the card renders under its step there.
 */
import { agentDisplayName } from "@/components/chat/step-list/agent-meta";
import type { StepView } from "@/components/chat/step-list/types";

import { ToolConfirmationCard } from "./ToolConfirmationCard";

export function PendingToolConfirmations({
  steps,
  peerNames = {},
  peerDisplayNames = {},
}: {
  steps: StepView[];
  peerNames?: Record<string, string>;
  peerDisplayNames?: Record<string, string>;
}) {
  const pending = steps.filter((step) => step.confirmation && !step.confirmation.outcome);
  if (pending.length === 0) return null;
  return (
    <div className="tcf-band-slot" data-testid="band-tool-confirmations">
      {pending.map((step) => {
        const peer = step.subagentId ? peerNames[step.subagentId] : undefined;
        const agentName = peer
          ? agentDisplayName(peer, step.subagentId ? peerDisplayNames[step.subagentId] : undefined)
          : agentDisplayName("lead");
        return <ToolConfirmationCard key={step.confirmation!.id} confirmation={step.confirmation!} agentName={agentName} />;
      })}
    </div>
  );
}
