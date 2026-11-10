"use client";

/**
 * Kind-specific previews of a call waiting for confirmation. The email and Shopify
 * previews are shared by the inline card and the original dialogs (kept until
 * Release), so both show the same exact content. The MCP argument list is shared
 * by personal and Team MCP cards.
 */
import { useTranslations } from "next-intl";

import type { EmailSendConfirmation } from "@/lib/email-send-confirmation-api";
import { formatByteSize } from "@/lib/format";
import type { ShopifyContentConfirmation } from "@/lib/shopify-content-confirmation-api";

/** Recipient, subject, body and attachments of an outgoing email. */
export function EmailSendPreview({ item }: { item: EmailSendConfirmation }) {
  const t = useTranslations("emailSendConfirmation");
  return (
    <>
      <p>{t("to", { to: item.to })}</p>
      <p>{t("subject", { subject: item.subject })}</p>
      <pre>{item.body}</pre>
      {item.attachments?.length ? (
        <section aria-label={t("attachments")}>
          <p>{t("attachments")}</p>
          <ul>
            {item.attachments.map((attachment) => (
              <li key={attachment.artifact_id}>
                {attachment.filename} ({formatByteSize(attachment.size) || "0 B"})
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  );
}

/** Shop, object and the per-field before/after of a Shopify content update. */
export function ShopifyContentPreview({ item }: { item: ShopifyContentConfirmation }) {
  const t = useTranslations("shopifyContentConfirmation");
  return (
    <>
      <p>{item.shop_domain}</p>
      <p>
        {t(item.resource)}: {item.title} · {item.handle}
      </p>
      <p>{item.object_id}</p>
      {item.edits.map((edit) => (
        <section key={edit.field}>
          <h3>{t(`fields.${edit.field}`)}</h3>
          <p>{t("before")}</p>
          <pre>{edit.before === null ? t("defaultValue") : edit.before || t("emptyValue")}</pre>
          <p>{t("after")}</p>
          <pre>{edit.after || t("emptyValue")}</pre>
        </section>
      ))}
    </>
  );
}

/** Values longer than this, or spanning lines, are folded behind a short preview. */
const LONG_VALUE_CHARS = 160;
const FOLDED_PREVIEW_CHARS = 120;

function argumentText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/**
 * MCP call arguments as a key/value list: long values are folded behind a short
 * preview, and the exact JSON sent to the service can be expanded below.
 */
export function McpArgumentList({ args }: { args: Record<string, unknown> | null | undefined }) {
  const t = useTranslations("toolConfirmation");
  const entries = args && typeof args === "object" && !Array.isArray(args) ? Object.entries(args) : [];
  return (
    <div className="tcf-args">
      <p className="tcf-args-title">{t("arguments")}</p>
      {entries.length === 0 ? (
        <p className="tcf-args-empty">{t("argumentsNone")}</p>
      ) : (
        <>
          <dl className="tcf-args-list">
            {entries.map(([name, value]) => {
              const text = argumentText(value);
              const long = text.length > LONG_VALUE_CHARS || text.includes("\n");
              return (
                <div className="tcf-arg" key={name}>
                  <dt>{name}</dt>
                  <dd>
                    {long ? (
                      <details className="tcf-arg-fold">
                        <summary>
                          <span className="tcf-arg-preview">
                            {text.replace(/\s+/g, " ").slice(0, FOLDED_PREVIEW_CHARS)}…
                          </span>{" "}
                          <span className="tcf-arg-more">{t("argumentShowAll")}</span>
                        </summary>
                        <pre>{text}</pre>
                      </details>
                    ) : (
                      text
                    )}
                  </dd>
                </div>
              );
            })}
          </dl>
          <details className="tcf-args-raw">
            <summary>{t("argumentsRaw")}</summary>
            <pre>{JSON.stringify(args, null, 2)}</pre>
          </details>
        </>
      )}
    </div>
  );
}
