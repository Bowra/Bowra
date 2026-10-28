"use client";

import { useTranslations } from "next-intl";
import { ArrowRight } from "lucide-react";
import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
import { collaborationTopicState, useCollaborationTrace } from "./collaboration-trace-state";
import "./CollaborationTrace.css";

export function CollaborationTrace() {
  const trace = useCollaborationTrace();
  const t = useTranslations("step_list.collaboration");
  if (!trace?.enabled) return null;
  return <section className="bh-collaboration-trace" aria-label={t("title")} aria-busy={trace.loading}>
    <div className="bh-collaboration-heading">
      <strong>{t("title")}</strong>
      <button type="button" onClick={trace.refresh} disabled={trace.loading}>{t("refresh")}</button>
    </div>
    {trace.error ? <p>{t("load_error")}</p> : trace.loading ? <p>{t("loading")}</p> : null}
    {trace.data?.topics.map((topic) => {
      const first = topic.messages[0];
      if (!first) return null;
      return <details key={topic.topic_id} className="bh-collaboration-topic">
        <summary>
          <span>{trace.name(topic.sender_id ?? first.sender_id)} <ArrowRight aria-hidden="true" size={12} /> {trace.name(topic.recipient_id ?? first.recipient_id)}</span>
          <span className="bh-collaboration-state">{t(collaborationTopicState(topic, trace.terminal))}</span>
        </summary>
        <ol className="bh-collaboration-messages">
          {topic.messages.map((message) => <li key={message.operation_id}>
            <div className="bh-collaboration-messageLabel">{trace.name(message.sender_id)} · {t(`action_${message.action}`)}</div>
            <p>{trace.loading ? t("loading") : message.unavailable ? t("hidden") : message.body}</p>
            {!message.unavailable && message.references?.length ? <div className="bh-collaboration-evidence">
              {message.references.map((reference, index) => <button key={`${reference.identity}:${reference.version}`} type="button"
                onClick={() => requestCanvasDataFocus(reference.identity, { entryPoint: "chat" })}>
                {t("evidence", { number: index + 1 })}
              </button>)}
            </div> : null}
          </li>)}
        </ol>
      </details>;
    })}
  </section>;
}
