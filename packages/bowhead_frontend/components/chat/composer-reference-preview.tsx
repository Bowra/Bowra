"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { copyTextWithFallback } from "@/lib/clipboard";
import {
  inputDocumentDisplayText,
  type InputDocument,
} from "@/lib/composer-input-document";
import type { AttachmentRef } from "@/lib/types";

/** Local saved input only. Labels/parameters are escaped text, never instructions. */
export function ComposerReferencePreview({
  document,
  attachments,
  onRemove,
  title,
  collapsed = false,
}: {
  document: InputDocument;
  attachments: AttachmentRef[];
  onRemove?: () => Promise<boolean>;
  title?: string;
  collapsed?: boolean;
}) {
  const t = useTranslations("composer");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const copy = async (full: boolean) => {
    const value = full
      ? JSON.stringify(
          {
            schemaVersion: 2,
            message: inputDocumentDisplayText(document),
            input_document: document,
            attachments,
          },
          null,
          2,
        )
      : inputDocumentDisplayText(document);
    const copied = await copyTextWithFallback(
      value,
      t("reference_copy_fallback"),
    );
    setStatus(t(copied ? "reference_copied" : "reference_copy_fallback"));
  };
  const remove = async () => {
    if (busy || !onRemove) return;
    setBusy(true);
    try {
      if (!(await onRemove())) setStatus(t("reference_remove_conflict"));
    } catch {
      setStatus(t("reference_remove_conflict"));
    } finally {
      setBusy(false);
    }
  };
  const contents = (
    <>
      {title ? <p>{title}</p> : null}
      <p className="composer-reference-preview__note">
        {t("reference_saved_readonly")}
      </p>
      <div className="composer-reference-preview__document">
        {document.nodes.map((node, index) =>
          node.type === "text" ? (
            <span key={`text-${index}`}>{node.text}</span>
          ) : (
            <span
              className="composer-reference-preview__reference"
              key={node.node_id}
            >
              {node.resource.kind === "agent" ? "@" : "/"}
              {node.label}
            </span>
          ),
        )}
      </div>
      {attachments.length ? (
        <ul>
          {attachments.map((attachment) => (
            <li key={attachment.fileId}>{attachment.filename}</li>
          ))}
        </ul>
      ) : null}
      <details>
        <summary>{t("reference_saved_details")}</summary>
        {document.nodes
          .filter((node) => node.type === "reference")
          .map((node) => (
            <div key={node.node_id}>
              <strong>{node.label}</strong>
              <pre>{JSON.stringify(node.parameters, null, 2)}</pre>
            </div>
          ))}
      </details>
      <div className="composer-v2__draft-warning-actions">
        <button type="button" onClick={() => void copy(false)}>
          {t("reference_copy_text")}
        </button>
        <button type="button" onClick={() => void copy(true)}>
          {t("reference_copy_full")}
        </button>
        {onRemove ? (
          <button type="button" disabled={busy} onClick={() => void remove()}>
            {t("reference_remove_all")}
          </button>
        ) : null}
      </div>
      {status ? <p role="status">{status}</p> : null}
    </>
  );
  return (
    <section
      className="composer-reference-preview"
      aria-label={title ?? t("reference_saved_title")}
    >
      {collapsed ? (
        <details className="composer-reference-preview__disclosure">
          <summary>{t("reference_saved_title")}</summary>
          {contents}
        </details>
      ) : contents}
    </section>
  );
}
