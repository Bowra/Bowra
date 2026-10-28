import { describe, expect, it } from "vitest";
import { effectiveEndpoint } from "@/components/settings/models";

describe("effectiveEndpoint", () => {
  it("appends the openai-compat chat path, vendor prefix preserved", () => {
    expect(effectiveEndpoint("openai_compatible", "https://host/openai/v1", ""))
      .toBe("https://host/openai/v1/chat/completions");
  });

  it("tolerates a trailing slash", () => {
    expect(effectiveEndpoint("openai_compatible", "https://host/v1/", ""))
      .toBe("https://host/v1/chat/completions");
  });

  it("uses the anthropic default when messages_path is blank", () => {
    expect(effectiveEndpoint("anthropic_compatible", "https://host", ""))
      .toBe("https://host/v1/messages");
  });

  it("honours a custom anthropic messages_path", () => {
    expect(effectiveEndpoint("anthropic_compatible", "https://host", "/messages"))
      .toBe("https://host/messages");
  });
});
