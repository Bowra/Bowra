"use client";

import { useEffect, useSyncExternalStore } from "react";

import { useBrand } from "@/components/brand-provider";
import {
  applyOwnedDocumentMetadata,
  resolveBrandMetadata,
} from "@/lib/brand-metadata";
import {
  getPageTitleSnapshot,
  getServerPageTitleSnapshot,
  getServerTitleAttentionSnapshot,
  getTitleAttentionSnapshot,
  subscribePageTitle,
  subscribeTitleAttention,
  withTitleAttention,
} from "@/lib/page-title-store";

/**
 * `whiteLabel`: the public layout passes true, so a custom brand's tab title is just the application name (matching the gateway first-paint injection).
 * The authenticated layout's title comes from `/theme/effective` and the local brand cache, not the public injection, and is unchanged.
 * The attention count ("(n) " prefix for confirmations waiting for the user) is composed here, by the title's only owner.
 */
export function BrandMetadataController({ whiteLabel = false }: { whiteLabel?: boolean } = {}): null {
  const { bundle } = useBrand();
  const pageTitle = useSyncExternalStore(
    subscribePageTitle,
    getPageTitleSnapshot,
    getServerPageTitleSnapshot,
  );
  const attention = useSyncExternalStore(
    subscribeTitleAttention,
    getTitleAttentionSnapshot,
    getServerTitleAttentionSnapshot,
  );

  useEffect(() => {
    const state = resolveBrandMetadata(bundle, pageTitle, "live", { whiteLabel });
    applyOwnedDocumentMetadata({
      ...state,
      documentTitle: withTitleAttention(state.documentTitle, attention),
    });
  }, [bundle, pageTitle, whiteLabel, attention]);

  return null;
}
