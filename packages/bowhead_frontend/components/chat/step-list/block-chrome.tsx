"use client";

/**
 * block-chrome.tsx — the net-new chat chrome the agent-block redesign adds
 * on top of the existing FE surface (§10 of the block-model-spec):
 *
 *   ReadableProv → a neutral list of agents whose work contributed to the
 *                  answer. It does not infer ordering, concurrency, or a judge.
 *
 * Deliberately NOT re-built here (the FE already owns these, D-register-protected):
 * typed citations + hover popover, ChatInline{Chart,Kpi,Table,File,Image},
 * the byline (BotByline) and the answer bubble (ChatBubbleNew). ReadableProv is
 * rendered by AgentTrace and reads its labels from the production message
 * catalog.
 */
import { Fragment } from "react";
import { useTranslations } from "next-intl";
import { AgentBadge } from "./AgentBadge";
import { agentDisplayName } from "./agent-meta";

/* ── readable provenance participants ─────────────────────────────────── */
export interface ProvenanceParticipant {
  agent: string;
  displayName?: string;
}

export function ReadableProv({
  participants,
}: {
  participants: ProvenanceParticipant[];
}) {
  const t = useTranslations("step_list");
  return (
    <div className="provflow">
      <span className="lead-in">{t("prov_source")}</span>
      {participants.map((item, i) => (
        <Fragment key={`${item.agent}-${i}`}>
          {i > 0 && <span className="provconn">·</span>}
          {item.agent === "lead" ? (
            <span className="provseg">
              <AgentBadge agent="lead" size={18} />
              {t("prov_lead")}
            </span>
          ) : (
            <span className="provseg">
              <AgentBadge agent={item.agent} size={18} />
              <b>{agentDisplayName(item.agent, item.displayName)}</b>
            </span>
          )}
        </Fragment>
      ))}
    </div>
  );
}
