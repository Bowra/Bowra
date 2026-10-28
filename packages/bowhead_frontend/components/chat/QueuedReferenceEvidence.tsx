"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { fetchQueuedReferenceEvidence, type QueuedReferenceEvidence as Evidence } from "@/lib/reference-evidence-api";
import "./reference-evidence.css";

/** Acceptance facts stay separate from the child's actual execution record. */
export function QueuedReferenceEvidence({ workspaceId, sessionId, submissionId }: {
  workspaceId: string; sessionId: string; submissionId: string;
}) {
  const t = useTranslations("reference_evidence");
  const panelId = useId();
  const toggle = useRef<HTMLButtonElement>(null);
  const request = useRef<AbortController | null>(null);
  const identity = JSON.stringify([workspaceId, sessionId, submissionId]);
  const [snapshot, setView] = useState<{ identity: string; loading: boolean; error: boolean; data: Evidence | null } | null>(null);
  const view = snapshot?.identity === identity ? snapshot : null;
  useEffect(() => { setView(null); return () => { request.current?.abort(); }; }, [identity]);
  const load = async (includeContent = false) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setView({ identity, loading: true, error: false, data: null });
    try {
      const data = await fetchQueuedReferenceEvidence(workspaceId, sessionId, submissionId, includeContent, controller.signal);
      if (!controller.signal.aborted && request.current === controller) setView({ identity, loading: false, error: false, data });
    } catch {
      if (!controller.signal.aborted && request.current === controller) setView({ identity, loading: false, error: true, data: null });
    }
  };
  const close = () => { request.current?.abort(); setView(null); toggle.current?.focus(); };
  return (
    <section className="reference-evidence-root">
      <button ref={toggle} type="button" className="reference-evidence-action" aria-expanded={!!view} aria-controls={panelId} onClick={() => view ? close() : void load()}>
        {t("queue_source")}
      </button>
      {view ? <div id={panelId} className="reference-evidence-panel" aria-busy={view.loading}>
        {view.loading ? <p role="status">{t("loading")}</p> : view.error ? <p role="alert">{t("error")}</p> : !view.data ? <p>{t("empty")}</p> : <>
          <p>{t(`queue_${view.data.state}`)}</p>
          <p>{t("queue_explanation")}</p>
          {view.data.partial ? <p>{t("partial")}</p> : null}
          <ul className="reference-evidence-list">
            {view.data.references.map((item, index) => <li key={index}>
              <strong>{item.hidden ? t("hidden") : item.name || t(`kind_${item.kind}`)}</strong>
              {item.parameters !== null ? <><p>{t("queue_parameters")}</p><pre tabIndex={0} className="reference-evidence-excerpt">{item.parameters}</pre></> : null}
            </li>)}
          </ul>
          {view.data.text ? <><p>{t("queue_text")}</p><pre tabIndex={0} className="reference-evidence-excerpt">{view.data.text}</pre></> : null}
          {view.data.truncated ? <p>{t("truncated")}</p> : null}
        </>}
        <div className="reference-evidence-row">
          {!view.loading && view.data ? <button type="button" className="reference-evidence-action" onClick={() => void load(true)}>{t("queue_content")}</button> : null}
          <button type="button" className="reference-evidence-action" disabled={view.loading} onClick={() => void load()}>{t("refresh")}</button>
          <button type="button" className="reference-evidence-action" onClick={close}>{t("close")}</button>
        </div>
      </div> : null}
    </section>
  );
}
