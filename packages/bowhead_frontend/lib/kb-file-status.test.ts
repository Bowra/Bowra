// Every failure reason has list copy and a drawer title in both Chinese and English (source_missing previously lacked a drawer title, exposing the raw key).
import { describe, expect, it } from "vitest";

import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

import { KB_FAIL_REASONS } from "./kb-file-status";

describe("KB_FAIL_REASONS 文案齐全", () => {
  for (const [locale, messages] of [["zh-CN", messagesZh], ["en-US", messagesEn]] as const) {
    it(locale, () => {
      const kb = messages.knowledge_base as unknown as {
        fail_reason: Record<string, string>;
        drawer: { fail_title: Record<string, string> };
      };
      for (const reason of KB_FAIL_REASONS) {
        expect(kb.fail_reason[reason], `fail_reason.${reason}`).toBeTruthy();
        expect(kb.drawer.fail_title[reason], `drawer.fail_title.${reason}`).toBeTruthy();
      }
    });
  }
});
