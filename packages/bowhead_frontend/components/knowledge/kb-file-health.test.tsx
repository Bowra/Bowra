import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import messages from "@/messages/zh-CN.json";
import { renderIntl } from "@/__tests__/intl-render";
import { KbFileHealth } from "./kb-file-health";

const auth = vi.hoisted(() => ({ enabled: true, guarded: false }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => ({ me: { capabilities: { kb_health_observation: auth.enabled, kb_use_guard: auth.guarded } } }) }));
const result = { file_id: "kbf_1", file_version: 1, checked_at: "2026-09-28T00:00:00Z", source_health: "present", source_reason: "head_present", index: { state: "unknown", reason: "legacy_chunks_readable", observed_chunks: 1, count_limited: false }, binding_state: "unverified", evidence_state: "unknown", eligibility_enforced: false };
beforeEach(() => { auth.enabled = true; auth.guarded = false; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("checks only on request and distinguishes readable legacy chunks from verified binding", async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(result)));
  vi.stubGlobal("fetch", fetcher);
  renderIntl(<KbFileHealth fileId="kbf_1" version={1} />);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "检查来源" }));
  await screen.findByText("尚未验证索引是否对应当前原件。");
  expect(screen.getByRole("status")).toHaveTextContent("抽样片段可读取");
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("discards an in-flight result when another file is opened", async () => {
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>((done) => { resolve = done; })));
  const view = renderIntl(<KbFileHealth fileId="kbf_1" version={1} />);
  fireEvent.click(screen.getByRole("button", { name: "检查来源" }));
  view.rerender(<NextIntlClientProvider locale="zh-CN" messages={messages}><KbFileHealth fileId="kbf_2" version={1} /></NextIntlClientProvider>);
  await act(async () => { resolve(new Response(JSON.stringify(result))); });
  expect(screen.getByRole("status")).toBeEmptyDOMElement();
  expect(screen.getByRole("button", { name: "检查来源" })).toBeEnabled();
});

it("shows neutral feedback when access is revoked and offers no stale observation", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 403 })));
  renderIntl(<KbFileHealth fileId="kbf_1" version={1} />);
  fireEvent.click(screen.getByRole("button", { name: "检查来源" }));
  await screen.findByText(/暂时无法完成检查/);
  expect(screen.getByRole("status")).not.toHaveTextContent("存储中可找到");
});

it("keeps the check hidden without the capability", () => {
  auth.enabled = false;
  renderIntl(<KbFileHealth fileId="kbf_1" version={1} />);
  expect(screen.queryByTestId("kb-file-health")).not.toBeInTheDocument();
});


it("restores through the offered action and explains a different file without replacing it", async () => {
  auth.guarded = true;
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...result, eligibility_enforced: true, use_state: "paused", primary_action: "restore" })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ outcome: "different_content_requires_new_file" })));
  vi.stubGlobal("fetch", fetcher);
  renderIntl(<KbFileHealth fileId="kbf_1" version={1} allowActions />);
  fireEvent.click(screen.getByRole("button", { name: "检查来源" }));
  await screen.findByRole("button", { name: "恢复原件" });
  expect(fetcher.mock.calls[0][0]).toContain("/verify");
  fireEvent.change(screen.getByLabelText("恢复原件"), { target: { files: [new File(["changed"], "new.md")] } });
  await screen.findByText(/所选文件内容与原件不同/);
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(fetcher.mock.calls[1][0]).toContain("/restore-source");
  expect(screen.queryByRole("button", { name: "重新解析并核对" })).not.toBeInTheDocument();
});
