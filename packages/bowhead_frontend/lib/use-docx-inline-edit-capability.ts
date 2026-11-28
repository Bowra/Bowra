"use client";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";

// FEATURE_DOCX_INLINE_EDIT, from the `/me` capabilities like the other deployment switches the browser
// needs (rollout register: Word inline edit). Fails closed: without an answer Word files keep the
// reading view.
let forcedForTest: boolean | null = null;

/** Test hook: force the capability (null returns to the `/me` answer). */
export function setDocxInlineEditCapabilityForTest(value: boolean | null): void {
  forcedForTest = value;
}

export function useDocxInlineEditCapability(): boolean {
  const enabled = useOptionalAuthSession()?.me?.capabilities?.docx_inline_edit === true;
  return forcedForTest ?? enabled;
}
