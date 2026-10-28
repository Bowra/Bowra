import { afterEach, describe, expect, it, vi } from "vitest";

import { changePassword } from "./security-api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("changePassword", () => {
  it("returns a wrong-current-password error without treating the session as expired", async () => {
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({
        detail: {
          code: "wrong_old_password",
          message: "当前密码错误",
        },
      }),
      {
        status: 401,
        headers: { "content-type": "application/json", "x-request-id": "0123456789abcdef0123456789abcdef" },
      },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = changePassword({
      old_password: "wrong",
      new_password: "NewPassword456",
    });

    await expect(result).rejects.toMatchObject({
      status: 401,
      code: "wrong_old_password",
      message: "当前密码错误",
      // Carry the request ID with SecurityError so Copy Diagnostics includes it on failure.
      requestId: "0123456789abcdef0123456789abcdef",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps a wrong-current-password error local after refreshing an expired access token", async () => {
    const paths: string[] = [];
    let passwordAttempts = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      paths.push(path);
      if (path === "/api/v1/auth/refresh") {
        return new Response(JSON.stringify({
          ok: true,
          token: "fresh-token",
          user: {},
        }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (path === "/api/v1/auth/change-password") {
        passwordAttempts += 1;
        const detail = passwordAttempts === 1
          ? { code: "invalid_token" }
          : {
            code: "wrong_old_password",
            message: "当前密码错误",
          };
        return new Response(JSON.stringify({ detail }), {
          status: 401,
          headers: { "content-type": "application/json" },
        });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(changePassword({
      old_password: "wrong",
      new_password: "NewPassword456",
    })).rejects.toMatchObject({
      status: 401,
      code: "wrong_old_password",
      message: "当前密码错误",
    });
    expect(paths).toEqual([
      "/api/v1/auth/change-password",
      "/api/v1/auth/refresh",
      "/api/v1/auth/change-password",
    ]);
  });

  it("retries the password change after refreshing an expired access token", async () => {
    const paths: string[] = [];
    let passwordAttempts = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      paths.push(path);
      if (path === "/api/v1/auth/refresh") {
        return new Response(JSON.stringify({
          ok: true,
          token: "fresh-token",
          user: {},
        }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (path === "/api/v1/auth/change-password") {
        passwordAttempts += 1;
        if (passwordAttempts === 1) {
          return new Response(JSON.stringify({
            detail: { code: "invalid_token" },
          }), {
            status: 401,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify({
          ok: true,
          access_token: "changed-token",
          other_devices_kicked: 2,
        }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(changePassword({
      old_password: "OldPassword123",
      new_password: "NewPassword456",
    })).resolves.toMatchObject({
      ok: true,
      access_token: "changed-token",
      other_devices_kicked: 2,
    });
    expect(paths).toEqual([
      "/api/v1/auth/change-password",
      "/api/v1/auth/refresh",
      "/api/v1/auth/change-password",
    ]);
  });
});
