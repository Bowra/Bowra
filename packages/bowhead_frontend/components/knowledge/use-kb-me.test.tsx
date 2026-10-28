// useKbMe: reuse /auth/me already fetched by the app shell instead of fetching on every mount (such as every upload-dialog opening).
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MeUser } from "@/lib/auth-api";

const mocks = vi.hoisted(() => ({
  sessionMe: null as Partial<MeUser> | null,
  hasProvider: true,
  fetchMe: vi.fn(),
}));

vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => (mocks.hasProvider ? { status: "authenticated", me: mocks.sessionMe } : null),
}));
vi.mock("@/lib/use-space", () => ({ useSpaceSelection: () => ({ activeId: "ws_1" }) }));
vi.mock("./kb-me", () => ({ fetchKbMe: (...args: unknown[]) => mocks.fetchMe(...args) }));

import { useKbMe, useKbMeState } from "./use-kb-me";

beforeEach(() => {
  mocks.sessionMe = null;
  mocks.hasProvider = true;
  mocks.fetchMe.mockReset();
});

describe("useKbMe", () => {
  it("会话里已有当前用户：首帧即返回身份，不再请求 /auth/me", () => {
    mocks.sessionMe = { id: "u_1", team_id: "team_1", is_team_admin: true };

    const { result } = renderHook(() => useKbMe());

    expect(result.current).toEqual({ user_id: "u_1", team_id: "team_1", is_team_admin: true, workspace_id: "ws_1" });
    expect(mocks.fetchMe).not.toHaveBeenCalled();
  });

  it("没有会话（外壳之外）：回退为自行请求", async () => {
    mocks.hasProvider = false;
    mocks.fetchMe.mockResolvedValue({ user_id: "u_2", team_id: "team_2", is_team_admin: false });

    const { result } = renderHook(() => useKbMe());

    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current?.user_id).toBe("u_2"));
    expect(mocks.fetchMe).toHaveBeenCalledTimes(1);
  });
});


it("distinguishes failed identity loading and supports an explicit read retry", async () => {
  mocks.hasProvider = false;
  mocks.fetchMe.mockResolvedValueOnce(null).mockResolvedValueOnce({ user_id: "u_2", team_id: "team_2", is_team_admin: false });
  const { result } = renderHook(() => useKbMeState());
  expect(result.current.status).toBe("loading");
  await waitFor(() => expect(result.current.status).toBe("failed"));
  expect(result.current.me).toBeNull();
  act(() => result.current.retry());
  await waitFor(() => expect(result.current.status).toBe("ready"));
  expect(result.current.me?.user_id).toBe("u_2");
  expect(mocks.fetchMe).toHaveBeenCalledTimes(2);
});
