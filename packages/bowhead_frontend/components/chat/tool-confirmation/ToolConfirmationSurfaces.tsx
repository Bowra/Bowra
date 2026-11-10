"use client";

import { EmailSendConfirmations } from "@/components/chat/email-send-confirmations";
import { PersonalMcpConfirmations } from "@/components/chat/personal-mcp-confirmations";
import { ShopifyContentConfirmations } from "@/components/chat/shopify-content-confirmations";
import { TeamToolConfirmations } from "@/components/chat/team-tool-confirmations";
import { useAuthSession } from "@/lib/auth/auth-session-context";

import { ToolConfirmationNotice } from "./ToolConfirmationNotice";

/**
 * Global confirmation surfaces. With `inline_tool_confirmations` on, confirmations are
 * cards in the conversation and only the non-blocking notice is mounted here; otherwise
 * the four original dialogs stay exactly as before (removed at Release).
 */
export function ToolConfirmationSurfaces() {
  const { me } = useAuthSession();
  if (me?.capabilities?.inline_tool_confirmations === true) return <ToolConfirmationNotice />;
  return (
    <>
      <PersonalMcpConfirmations />
      <EmailSendConfirmations />
      <ShopifyContentConfirmations />
      <TeamToolConfirmations />
    </>
  );
}
