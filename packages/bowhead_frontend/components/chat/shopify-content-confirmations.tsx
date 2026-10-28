"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { DialogShell } from "@/components/ui/dialog";
import { shopifyContentConfirmationApi, type ShopifyContentConfirmation } from "@/lib/shopify-content-confirmation-api";
import { useAuthSession } from "@/lib/auth/auth-session-context";
import { useHasActiveToolStep } from "@/lib/turn-store";
import { useConfirmationQueuePoll } from "@/lib/use-confirmation-queue-poll";

export function ShopifyContentConfirmations() {
  const { me } = useAuthSession();
  return me?.capabilities?.shopify_content_confirmations === true ? <ShopifyContentConfirmationQueue /> : null;
}

function ShopifyContentConfirmationQueue() {
  const t = useTranslations("shopifyContentConfirmation");
  const [pending, setPending] = useState<ShopifyContentConfirmation[]>([]);
  const [mountCheck, setMountCheck] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const running = useHasActiveToolStep(".shopify_apply_content_update", "suffix");
  useConfirmationQueuePoll({
    enabled: mountCheck || running || pending.length > 0,
    load: async () => {
      try {
        const data = await shopifyContentConfirmationApi<{
          confirmations: ShopifyContentConfirmation[];
        }>("");
        return data.confirmations;
      } catch (failure) {
        setMountCheck(false);
        throw failure;
      }
    },
    onItems: (items) => {
      setPending(items);
      setMountCheck(false);
    },
  });
  const current = pending[0];
  if (!current) return null;
  async function decide(decision: "approve" | "reject") {
    if (!current || busy) return;
    setBusy(true);
    setError(false);
    try {
      await shopifyContentConfirmationApi(`/${encodeURIComponent(current.id)}/decision`, "POST", { decision });
      setPending((items) => items.filter((item) => item.id !== current.id));
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <DialogShell
      key={current.id}
      onClose={() => undefined}
      busy={busy}
      closeOnBackdrop={false}
      layer="top"
      labelledBy="shopify-content-confirmation-title"
      backdropClassName="personal-mcp-confirmation-backdrop"
      panelClassName="personal-mcp-confirmation"
    >
      <h2 id="shopify-content-confirmation-title">{t("title")}</h2>
      <p>{t("notice")}</p>
      <p>{current.shop_domain}</p>
      <p>
        {t(current.resource)}: {current.title} · {current.handle}
      </p>
      <p>{current.object_id}</p>
      {current.edits.map((edit) => (
        <section key={edit.field}>
          <h3>{t(`fields.${edit.field}`)}</h3>
          <p>{t("before")}</p>
          <pre>{edit.before === null ? t("defaultValue") : edit.before || t("emptyValue")}</pre>
          <p>{t("after")}</p>
          <pre>{edit.after || t("emptyValue")}</pre>
        </section>
      ))}
      <p>
        {t("expires", {
          time: new Date(current.expires_at).toLocaleTimeString(),
        })}
      </p>
      {error ? <p role="alert">{t("decisionError")}</p> : null}
      <div className="personal-mcp-confirmation-actions">
        <button data-dialog-initial-focus="" type="button" disabled={busy} onClick={() => void decide("reject")}>
          {t("reject")}
        </button>
        <button className="personal-mcp-confirmation-allow-once" type="button" disabled={busy} onClick={() => void decide("approve")}>
          {t("approve")}
        </button>
      </div>
    </DialogShell>
  );
}
