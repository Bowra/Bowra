import { describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";

import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

import { useMemoryScopeLabel, withoutMemoryIds } from "./memory-display";

function wrapper(locale: "en-US" | "zh-CN") {
  const messages = locale === "en-US" ? en : zh;
  return function Wrapper({ children }: { children: ReactNode }) {
    return <NextIntlClientProvider locale={locale} messages={messages}>{children}</NextIntlClientProvider>;
  };
}

describe("useMemoryScopeLabel", () => {
  it("composes the scope chip in the UI language instead of the gateway's Chinese label (116 en-US)", () => {
    const { result } = renderHook(() => useMemoryScopeLabel(), { wrapper: wrapper("en-US") });
    expect(result.current({ type: "global" })).toBe("Only you · All Spaces");
    expect(result.current({ type: "space", space_name: "Retail", availability: "available" })).toBe("Only you · Retail");
    expect(result.current({ type: "space", space_name: "Retail", availability: "unavailable" })).toBe("Only you · Unavailable Space");
    expect(result.current(null)).toBeNull();
    // Older payloads without a structured type keep the server label rather than guessing.
    expect(result.current({ label: "所有空间" })).toBe("所有空间");
  });

  it("keeps the Chinese wording in zh-CN", () => {
    const { result } = renderHook(() => useMemoryScopeLabel(), { wrapper: wrapper("zh-CN") });
    expect(result.current({ type: "global" })).toBe("仅供你使用 · 所有空间");
  });
});

describe("withoutMemoryIds", () => {
  it("never renders a raw memory id", () => {
    expect(withoutMemoryIds("与已有记忆 mem_7c729bd92a5c（默认用亿元）矛盾")).toBe("与已有记忆（默认用亿元）矛盾");
    expect(withoutMemoryIds("Conflicts with mem_7c729bd92a5c.")).toBe("Conflicts with.");
    expect(withoutMemoryIds("与已有记忆mem_7c729bd92a5c矛盾")).toBe("与已有记忆矛盾");
    expect(withoutMemoryIds("xmem_7c729bd92a5c stays")).toBe("xmem_7c729bd92a5c stays");
    expect(withoutMemoryIds("plain reason")).toBe("plain reason");
    expect(withoutMemoryIds(null)).toBeNull();
  });
});
