/**
 * §C (real-auth) — i18n key parity for the verify notice (Task #8 FE).
 *
 * The component test mocks useTranslations to echo the key, so it cannot catch a missing/typo'd
 * locale key. This loads the real message bundles and asserts auth.verify.* exists in BOTH locales
 * with identical key sets — the actual rendered copy depends on it.
 */

import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

type Bundle = { auth: { verify?: Record<string, string> } };

const EXPECTED = [
  "pending_message",
  "pending_title",
  "resend",
  "resending",
  "resent",
  "resend_failed",
  "sent_title",
].sort();

describe("auth.verify i18n parity", () => {
  it("zh-CN defines exactly the expected auth.verify keys", () => {
    const keys = Object.keys((zh as Bundle).auth.verify ?? {}).sort();
    expect(keys).toEqual(EXPECTED);
  });

  it("en-US defines the same auth.verify keys as zh-CN", () => {
    const zhKeys = Object.keys((zh as Bundle).auth.verify ?? {}).sort();
    const enKeys = Object.keys((en as Bundle).auth.verify ?? {}).sort();
    expect(enKeys).toEqual(zhKeys);
  });
});
