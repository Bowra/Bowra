import { afterEach, describe, expect, it, vi } from "vitest";

import {
  composerDraftScopeSignature,
  composerOrdinaryDraftStorageKey,
  composerPersistenceKeys,
  type ComposerPersistenceScope,
} from "./composer-persistence-scope";

const ASCII_SCOPE = {
  userId: "user_1",
  teamId: "team_1",
  workspaceId: "ws_1",
  requestedSessionId: "session_1",
  authSessionGeneration: "auth-gen-1",
} satisfies ComposerPersistenceScope;

const ASCII_KEYS = {
  identityKey: "AAAAFHJlY292ZXJ5LWlkZW50aXR5LnYxAAAAFWh0dHA6Ly9sb2NhbGhvc3Q6MzAwMAAAAAZ1c2VyXzE",
  principalKey: "AAAAFXJlY292ZXJ5LXByaW5jaXBhbC52MQAAABVodHRwOi8vbG9jYWxob3N0OjMwMDAAAAAGdXNlcl8xAAAABnRlYW1fMQ",
  manifestKey: "AAAAFWNvbnZlcnNhdGlvbi1kcmFmdC52MQAAAF5BQUFBRlhKbFkyOTJaWEo1TFhCeWFXNWphWEJoYkM1Mk1RQUFBQlZvZEhSd09pOHZiRzlqWVd4b2IzTjBPak13TURBQUFBQUdkWE5sY2w4eEFBQUFCblJsWVcxZk1RAAAABHdzXzEAAAAJc2Vzc2lvbl8x",
  authSessionGenerationHash: "8d1e6666d75dda6e6b60f8650c8141ed3801795212656faca374735cf9ea1900",
} as const;

const UNICODE_NULL_SCOPE = {
  userId: "用户😀",
  teamId: "团队/α",
  workspaceId: "空间\nws",
  requestedSessionId: null,
  authSessionGeneration: "代际🔐\0v1",
} satisfies ComposerPersistenceScope;

const UNICODE_NULL_KEYS = {
  identityKey: "AAAAFHJlY292ZXJ5LWlkZW50aXR5LnYxAAAAFWh0dHA6Ly9sb2NhbGhvc3Q6MzAwMAAAAArnlKjmiLfwn5iA",
  principalKey: "AAAAFXJlY292ZXJ5LXByaW5jaXBhbC52MQAAABVodHRwOi8vbG9jYWxob3N0OjMwMDAAAAAK55So5oi38J-YgAAAAAnlm6LpmJ8vzrE",
  manifestKey: "AAAAFWNvbnZlcnNhdGlvbi1kcmFmdC52MQAAAGdBQUFBRlhKbFkyOTJaWEo1TFhCeWFXNWphWEJoYkM1Mk1RQUFBQlZvZEhSd09pOHZiRzlqWVd4b2IzTjBPak13TURBQUFBQUs1NVNvNW9pMzhKLVlnQUFBQUFubG02THBtSjh2enJFAAAACeepuumXtAp3cwAAAAA",
  authSessionGenerationHash: "1c01be17e5d4b7286ddd31e808b80a88c5ea266af142ef89ceec99829c1e2d44",
} as const;

describe("composer persistence scope keys", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps the existing partitioned key bytes stable", async () => {
    expect(window.location.origin).toBe("http://localhost:3000");
    const keys = await composerPersistenceKeys(ASCII_SCOPE);
    expect(keys).toEqual(ASCII_KEYS);
    expect(
      composerOrdinaryDraftStorageKey(
        keys.manifestKey,
        keys.authSessionGenerationHash,
      ),
    ).toBe(
      "AAAAEW9yZGluYXJ5LWRyYWZ0LnYyAAAAwEFBQUFGV052Ym5abGNuTmhkR2x2Ymkxa2NtRm1kQzUyTVFBQUFGNUJRVUZCUmxoS2JGa3lPVEphV0VvMVRGaENlV0ZYTldwaFdFSm9Za00xTWsxUlFVRkJRbFp2WkVoU2QwOXBPSFppUnpscVdWZDRiMkl6VGpCUGFrMTNUVVJCUVVGQlFVZGtXRTVzWTJ3NGVFRkJRVUZDYmxKc1dWY3haazFSQUFBQUJIZHpYekVBQUFBSmMyVnpjMmx2Ymw4eAAAAEA4ZDFlNjY2NmQ3NWRkYTZlNmI2MGY4NjUwYzgxNDFlZDM4MDE3OTUyMTI2NTZmYWNhMzc0NzM1Y2Y5ZWExOTAw",
    );
    expect(composerDraftScopeSignature(ASCII_SCOPE)).toBe(
      '["user_1","team_1","ws_1","session_1","auth-gen-1"]',
    );
  });

  it("keeps Unicode bytes and the null-session partition stable", async () => {
    await expect(composerPersistenceKeys(UNICODE_NULL_SCOPE)).resolves.toEqual(
      UNICODE_NULL_KEYS,
    );
    expect(composerDraftScopeSignature(UNICODE_NULL_SCOPE)).toBe(
      '["用户😀","团队/α","空间\\nws","","代际🔐\\u0000v1"]',
    );
    expect(composerDraftScopeSignature(null)).toBe("");
  });

  it("uses the same frozen hash when crypto.subtle is absent", async () => {
    vi.stubGlobal("crypto", {});
    await expect(composerPersistenceKeys(UNICODE_NULL_SCOPE)).resolves.toEqual(
      UNICODE_NULL_KEYS,
    );
  });
});
