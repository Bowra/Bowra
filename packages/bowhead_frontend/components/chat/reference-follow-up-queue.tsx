"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import {
  cancelQueuedReferenceSubmission,
  fetchQueuedReferenceSubmissions,
  type QueuedReferenceSubmissionDTO,
} from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { QueuedReferenceEvidence } from "./QueuedReferenceEvidence";

interface Props {
  workspaceId: string | null;
  sessionId: string | null;
  actorScopeKey: string | null;
  refreshKey: string;
  canCancel: boolean;
}

export function ReferenceFollowUpQueue({
  workspaceId,
  sessionId,
  actorScopeKey,
  refreshKey,
  canCancel,
}: Props) {
  const t = useTranslations("composer_references");
  const currentScopeKey = actorScopeKey && workspaceId && sessionId
    ? JSON.stringify([actorScopeKey, workspaceId, sessionId])
    : null;
  const currentScopeKeyRef = useRef(currentScopeKey);
  currentScopeKeyRef.current = currentScopeKey;
  const [projection, setProjection] = useState<{
    scopeKey: string;
    items: QueuedReferenceSubmissionDTO[];
  } | null>(null);
  const [loadFailedScope, setLoadFailedScope] = useState<string | null>(null);
  const [cancelFailed, setCancelFailed] = useState<{
    scopeKey: string;
    submissionId: string;
  } | null>(null);
  const [cancelling, setCancelling] = useState<{
    scopeKey: string;
    submissionId: string;
  } | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const items = projection?.scopeKey === currentScopeKey
    ? projection.items
    : [];
  const loadFailed = currentScopeKey !== null && loadFailedScope === currentScopeKey;
  const cancellingId = cancelling?.scopeKey === currentScopeKey
    ? cancelling.submissionId
    : null;

  useEffect(() => {
    if (!workspaceId || !sessionId || !currentScopeKey) return;
    const controller = new AbortController();
    let active = true;
    setLoadFailedScope((failedScope) => (
      failedScope === currentScopeKey ? null : failedScope
    ));
    void fetchQueuedReferenceSubmissions(
      workspaceId,
      sessionId,
      controller.signal,
    ).then((next) => {
      if (!active || currentScopeKeyRef.current !== currentScopeKey) return;
      setProjection({ scopeKey: currentScopeKey, items: next });
      setCancelFailed((failed) => (
        failed?.scopeKey === currentScopeKey ? null : failed
      ));
    }).catch((error: unknown) => {
      if (
        active
        && currentScopeKeyRef.current === currentScopeKey
        && (error as { name?: string })?.name !== "AbortError"
      ) {
        setLoadFailedScope(currentScopeKey);
      }
    });
    return () => {
      active = false;
      controller.abort();
    };
  }, [workspaceId, sessionId, currentScopeKey, refreshKey, reloadToken]);

  const cancel = async (item: QueuedReferenceSubmissionDTO) => {
    const scopeKey = currentScopeKey;
    if (!workspaceId || !sessionId || !scopeKey || cancellingId) return;
    setCancelling({
      scopeKey,
      submissionId: item.client_submission_id,
    });
    setCancelFailed((failed) => (
      failed?.scopeKey === scopeKey ? null : failed
    ));
    try {
      await cancelQueuedReferenceSubmission(
        workspaceId,
        sessionId,
        item.client_submission_id,
      );
      if (currentScopeKeyRef.current !== scopeKey) return;
      setProjection((current) => current?.scopeKey === scopeKey
        ? {
            scopeKey,
            items: current.items.filter(
              (candidate) => candidate.client_submission_id
                !== item.client_submission_id,
            ),
          }
        : current);
      setReloadToken((current) => current + 1);
    } catch (error: unknown) {
      if (
        currentScopeKeyRef.current === scopeKey
        && error instanceof ApiError
        && (error.status === 404 || error.status === 409)
      ) {
        setProjection((current) => current?.scopeKey === scopeKey
          ? {
              scopeKey,
              items: current.items.filter(
                (candidate) => candidate.client_submission_id
                  !== item.client_submission_id,
              ),
            }
          : current);
        setReloadToken((current) => current + 1);
      } else if (currentScopeKeyRef.current === scopeKey) {
        setCancelFailed({
          scopeKey,
          submissionId: item.client_submission_id,
        });
      }
    } finally {
      setCancelling((current) => current?.scopeKey === scopeKey
        ? null
        : current);
    }
  };

  if (items.length === 0 && !loadFailed) return null;

  return (
    <section
      className="reference-follow-up-queue"
      aria-label={t("queue_title", { count: items.length })}
    >
      <div className="reference-follow-up-queue__header">
        <strong>{t("queue_title", { count: items.length })}</strong>
        {loadFailed ? (
          <button
            type="button"
            onClick={() => setReloadToken((current) => current + 1)}
          >
            {t("queue_retry")}
          </button>
        ) : null}
      </div>
      {loadFailed ? (
        <p className="reference-follow-up-queue__error" role="alert">
          {t("queue_load_failed")}
        </p>
      ) : null}
      {items.length > 0 ? (
        <ol className="reference-follow-up-queue__items">
          {items.map((item) => (
            <li
              className="reference-follow-up-queue__item"
              key={`${currentScopeKey}:${item.client_submission_id}`}
            >
              <div className="reference-follow-up-queue__content">
                <p title={item.display_text}>{item.display_text}</p>
                <div className="reference-follow-up-queue__meta">
                  {item.resources.map((resource, index) => (
                    <span key={`${resource.kind}:${resource.label}:${index}`}>
                      {resource.kind === "agent" ? "@" : "/"}{resource.label}
                    </span>
                  ))}
                  {item.attachment_count > 0 ? (
                    <span>{t("queue_attachments", {
                      count: item.attachment_count,
                    })}</span>
                  ) : null}
                </div>
                {workspaceId && sessionId ? <QueuedReferenceEvidence
                  workspaceId={workspaceId}
                  sessionId={sessionId}
                  submissionId={item.client_submission_id}
                /> : null}
                {cancelFailed?.scopeKey === currentScopeKey
                  && cancelFailed.submissionId === item.client_submission_id ? (
                  <span
                    className="reference-follow-up-queue__error"
                    role="alert"
                  >
                    {t("queue_cancel_failed")}
                  </span>
                ) : null}
              </div>
              {canCancel ? (
                <button
                  type="button"
                  disabled={cancellingId !== null}
                  onClick={() => void cancel(item)}
                >
                  {cancellingId === item.client_submission_id
                    ? t("queue_cancelling")
                    : t("queue_cancel")}
                </button>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
