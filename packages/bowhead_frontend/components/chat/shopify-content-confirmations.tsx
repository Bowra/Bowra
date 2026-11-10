"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { DialogShell } from "@/components/ui/dialog";
import { formatTime } from "@/lib/locale";
import { ShopifyContentPreview } from "@/components/chat/tool-confirmation/previews";
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
  const locale = useLocale();
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
      <ShopifyContentPreview item={current} />
      <p>
        {t("expires", {
          time: formatTime(current.expires_at, locale, { seconds: true }),
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
