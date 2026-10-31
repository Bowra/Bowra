"use client";

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Lock } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { ChatCard, ChatCardHint } from "@/components/ui/card";

/**
 * Created-Agent receipt (Canvas Objects Spec part 2 §6): a success ChatCard with the name, saved resources and visibility, a secondary “Manage agent” and the primary “Start using”. Creation is acknowledged even
 * when the saved configuration cannot be read back; the facts then give way to a retry.
 *
 * Creating releases v1 straight away, so the card says so and who can use it, then gives two next steps
 * (docs/design/agent-release-beginner-experience.md, Item 2). The status line shows even when the saved
 * configuration can't be read, because creation itself succeeded.
 */
export function AgentCreationReceipt({ name, visibility, created = true, summary, readFailed, onRetry, onManage, onUse, onOpenEvaluate, onOpenWho }: {
  name: string;
  /** The audience the Agent was created with: `creator_only`, `team_wide`, or anything else for selected people. */
  visibility: string;
  /** False after editing an existing Agent: nothing was released, so no release line or first steps. */
  created?: boolean;
  summary: {
    /** "1 Plugin · 3 Tools", or null while the created Agent's resources are unknown. */
    resources: string | null;
    visibility: string;
  } | null;
  readFailed: boolean;
  onRetry: () => void;
  onManage: () => void;
  onUse?: () => void;
  /** Open the Agent's Evaluate tab. */
  onOpenEvaluate?: () => void;
  /** Open Configure › Who can use it. */
  onOpenWho?: () => void;
}) {
  const t = useTranslations("create_subagent");
  const usable = Boolean(summary && onUse);
  const privateAgent = visibility === "creator_only";
  const released = t(privateAgent ? "canvas_receipt_released_private" : visibility === "team_wide" ? "canvas_receipt_released_team" : "canvas_receipt_released_custom");
  const link = (onClick: (() => void) | undefined) => function ReceiptLink(chunks: ReactNode) {
    return onClick ? <button type="button" className="ui-card__inline-link" onClick={onClick}>{chunks}</button> : <>{chunks}</>;
  };
  const rich = { b: (chunks: ReactNode) => <strong>{chunks}</strong>, evaluate: link(onOpenEvaluate), who: link(onOpenWho) };
  return (
    <ChatCard
      intent="success"
      eyebrow={t("canvas_receipt_title")}
      title={name || t("canvas_created_agent")}
      data-testid="agent-creation-receipt"
      secondary={[
        ...(readFailed ? [{ label: t("canvas_receipt_retry"), onSelect: onRetry, variant: "secondary" as const }] : []),
        { label: t("manage_agent"), onSelect: onManage, variant: "secondary" as const },
      ]}
      primary={usable && onUse ? { label: t("canvas_start_use"), onSelect: onUse } : undefined}
    >
      {created ? <p className="ui-card__status-line">
        <Badge tone="positive">{privateAgent ? <Lock size={12} aria-hidden /> : null}{released}</Badge>
      </p> : null}
      {summary ? (
        <dl className="ui-card-facts">
          {summary.resources ? <>
            <dt>{t("receipt_resources_label")}</dt>
            <dd>{summary.resources}</dd>
          </> : null}
          <dt>{t("canvas_receipt_visibility")}</dt>
          <dd>{t(summary.visibility === "team_wide" ? "chip_vis_team" : summary.visibility === "creator_only" ? "chip_vis_me" : "canvas_receipt_custom_visibility")}</dd>
        </dl>
      ) : (
        <span role="status">
          <ChatCardHint pending={!readFailed}>{t(readFailed ? "canvas_receipt_unavailable" : "canvas_receipt_loading")}</ChatCardHint>
        </span>
      )}
      {created ? <ol className="ui-card__steps" aria-label={t("canvas_receipt_steps_label")}>
        {/* Name only the action this receipt renders: "Start using" exists only with direct use enabled. */}
        <li>{usable ? t.rich("canvas_receipt_step_try", rich) : t.rich("canvas_receipt_step_try_mention", { ...rich, name: name || t("canvas_created_agent") })}</li>
        <li>{t.rich(privateAgent ? "canvas_receipt_step_confidence_share" : "canvas_receipt_step_confidence", rich)}</li>
      </ol> : null}
    </ChatCard>
  );
}
