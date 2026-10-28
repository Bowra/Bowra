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
  subscribePageTitle,
} from "@/lib/page-title-store";

/**
 * `whiteLabel`: the public layout passes true, so a custom brand's tab title is just the application name (matching the gateway first-paint injection).
 * The authenticated layout's title comes from `/theme/effective` and the local brand cache, not the public injection, and is unchanged.
 */
export function BrandMetadataController({ whiteLabel = false }: { whiteLabel?: boolean } = {}): null {
  const { bundle } = useBrand();
  const pageTitle = useSyncExternalStore(
    subscribePageTitle,
    getPageTitleSnapshot,
    getServerPageTitleSnapshot,
  );

  useEffect(() => {
    applyOwnedDocumentMetadata(
      resolveBrandMetadata(bundle, pageTitle, "live", { whiteLabel }),
    );
  }, [bundle, pageTitle, whiteLabel]);

  return null;
}
