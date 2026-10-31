"use client";

import { createContext } from "react";

/** Authenticated presentation scope; receipt references never grant resource access. */
export const CoachReceiptScopeContext = createContext<{
  userId: string; teamId: string; workspaceId: string; sessionId: string;
} | null>(null);
