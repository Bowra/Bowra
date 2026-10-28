import { apiJson } from "@/lib/api-client";

export interface EmailSendConfirmation {
  id: string;
  turn_id: string;
  session_id: string;
  to: string;
  subject: string;
  body: string;
  attachments?: {
    artifact_id: string;
    filename: string;
    mime: string;
    size: number;
  }[];
  expires_at: string;
}

export function emailSendConfirmationApi<T>(
  path: string,
  method = "GET",
  body?: unknown,
) {
  return apiJson<T>(`/api/v1/email-send-confirmations${path}`, {
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
