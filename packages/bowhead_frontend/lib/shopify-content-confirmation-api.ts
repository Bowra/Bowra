import { apiJson } from "@/lib/api-client";

export interface ShopifyContentConfirmation {
  id: string;
  turn_id: string;
  session_id: string;
  team_id: string;
  shop_domain: string;
  object_id: string;
  resource: "product" | "page";
  title: string;
  handle: string;
  state: string;
  edits: { field: string; before: string | null; after: string }[];
  expires_at: string;
}

export function shopifyContentConfirmationApi<T>(path: string, method = "GET", body?: unknown) {
  return apiJson<T>(`/api/v1/shopify-content-confirmations${path}`, {
    method,
    cache: "no-store",
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
}
