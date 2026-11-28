"use client";

/**
 * 测试用例 › 试用记录 (trial-tech-design §6–§7, canvas ⑫): the Owner's latest trial chats of this Agent, which
 * stay out of the sidebar. Each row is titled by its first question and says when the last message was sent,
 * how many turns it has and how many drafts it used; 新的试用 is the release bar's 试用草稿.
 */

import { useId } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import type { AgentRecentTrial } from "@/lib/agents-api";
import { appPath } from "@/lib/base-path";
import { parseTrialTime } from "@/lib/agent-trial";
import { useCiFormat } from "./agent-ci-shared";

export function trialSessionHref(sessionId: string): string {
  return appPath(`/?${new URLSearchParams({ session: sessionId })}`);
}

export function AgentTrialList({ trials, busy = false, onNewTrial }: {
  trials: readonly AgentRecentTrial[];
  busy?: boolean;
  onNewTrial: () => void;
}) {
  const t = useTranslations("agent_trial");
  const format = useCiFormat();
  const titleId = useId();
  return (
    <section className="am-trial-list" aria-labelledby={titleId}>
      <header className="am-trial-list-head">
        <h3 id={titleId}>{t("list_title")}<span className="am-trial-list-meta">{t("list_meta")}</span></h3>
        <Button size="sm" variant="secondary" loading={busy} disabled={busy} onClick={onNewTrial}>{t("list_new")}</Button>
      </header>
      {trials.length === 0 ? <p className="am-ci-note">{t("list_empty")}</p> : (
        <ul className="am-trial-list-rows">
          {trials.map((trial) => {
            const question = trial.first_question.trim() || t("list_untitled");
            const at = parseTrialTime(trial.last_message_at);
            const meta = [
              at ? format.dateTime(at.toISOString()) : null,
              t("list_turns", { count: trial.turn_count }),
              trial.released_as
                ? t("list_released", { version: `v${trial.released_as.release_sequence}` })
                : trial.drafts_used.length > 1 ? t("list_drafts", { count: trial.drafts_used.length }) : null,
            ].filter(Boolean).join(t("list_separator"));
            return (
              <li key={trial.session_id} className="am-trial-list-row">
                <span className="am-trial-list-text">
                  <span className="am-trial-list-question">{question}</span>
                  <span className="am-trial-list-row-meta">{meta}</span>
                </span>
                <a className="am-trial-list-open" href={trialSessionHref(trial.session_id)} aria-label={t("list_open_label", { question })}>{t("list_open")}</a>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
