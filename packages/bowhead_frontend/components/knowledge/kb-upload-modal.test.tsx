// Knowledge-base experience cleanup P1 · upload modal (FR5.1 / FR5.2 / FR5.4; design 04 Upload Modal).
// Disconnected-transfer case exercises real uploadKbFile → xhrPostFormJson → kbFileError; stub only XMLHttpRequest.
import "@testing-library/jest-dom/vitest";

import type { ReactElement } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import zhMessages from "@/messages/zh-CN.json";
import type { KbMeContext } from "./kb-me";

const mocks = vi.hoisted(() => ({
  me: null as KbMeContext | null,
  spaces: [] as Array<{ id: string; name: string; desc: string }>,
  spacesLoading: false,
  readiness: "ready" as "ready" | "missing" | "unavailable" | "checking",
}));

vi.mock("./use-kb-me", () => ({ useKbMeState: () => ({ me: mocks.me, status: mocks.me ? "ready" : "loading", retry: vi.fn() }) }));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({
    items: mocks.spaces,
    activeId: mocks.me?.workspace_id || null,
    loading: mocks.spacesLoading,
    error: null,
    setActiveId: () => {},
    refresh: () => {},
  }),
}));
vi.mock("@/lib/kb-capabilities-api", () => ({ fetchKbCapabilities: () => Promise.resolve(null) }));
vi.mock("@/lib/kb-ingestion-readiness", () => ({
  useKbIngestionReadiness: () => ({ state: mocks.readiness, retry: vi.fn() }),
}));
vi.mock("@/lib/kb-knowledge-tables-api", () => ({ listKnowledgeTables: () => Promise.resolve([]) }));

import { KbUploadModal } from "./kb-upload-modal";

class MockXhr {
  static instances: MockXhr[] = [];
  upload: {
    onprogress?: (e: ProgressEvent) => void;
    onload?: () => void;
  } = {};
  onload?: () => void;
  onerror?: () => void;
  onabort?: () => void;
  ontimeout?: () => void;
  status = 0;
  responseText = "";
  body: unknown = null;
  aborted = false;
  constructor() {
    MockXhr.instances.push(this);
  }
  open() {}
  setRequestHeader() {}
  send(body: unknown) {
    this.body = body;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  progress(loaded: number, total: number) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total } as ProgressEvent);
  }
}

const ME_MEMBER: KbMeContext = { user_id: "u_1", team_id: "team_1", is_team_admin: false, workspace_id: "ws_1" };
const ME_ADMIN: KbMeContext = { ...ME_MEMBER, is_team_admin: true };

function wrap(ui: ReactElement) {
  return (
    <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
      {ui}
    </NextIntlClientProvider>
  );
}

function renderModal(props: Partial<Parameters<typeof KbUploadModal>[0]> = {}) {
  const onClose = vi.fn();
  const onUploaded = vi.fn();
  const ui = () => wrap(
    <KbUploadModal defaultScope="personal" onClose={onClose} onUploaded={onUploaded} {...props} />,
  );
  const r = render(ui());
  return { ...r, onClose, onUploaded, rerenderSame: () => r.rerender(ui()) };
}

function pick(name: string, size = 1000) {
  const file = new File([new Uint8Array(size)], name);
  fireEvent.change(screen.getByTestId("kb-file-input"), { target: { files: [file] } });
  return file;
}

async function startUpload() {
  await act(async () => {
    fireEvent.click(screen.getByTestId("kb-upload-submit"));
  });
  expect(MockXhr.instances.length).toBeGreaterThan(0);
  return MockXhr.instances[MockXhr.instances.length - 1];
}

beforeEach(() => {
  mocks.me = ME_MEMBER;
  mocks.spaces = [{ id: "ws_1", name: "商超零售场景", desc: "" }];
  mocks.spacesLoading = false;
  mocks.readiness = "ready";
  MockXhr.instances = [];
  vi.stubGlobal("XMLHttpRequest", MockXhr);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("KbUploadModal · 范围卡片", () => {
  it("空间卡片写出当前空间名", () => {
    renderModal();
    expect(screen.getByTestId("kb-scope-card-space")).toHaveTextContent("空间 · 商超零售场景");
    expect(screen.getByTestId("kb-scope-card-space")).toHaveTextContent("这个空间的成员。");
  });

  it("身份未返回：团队卡片显示占位而非「仅团队管理员」，且不能提交", () => {
    mocks.me = null;
    // Admin opens Team scope: before identity arrives, do not treat as nonadmin and force Personal scope.
    const { rerenderSame } = renderModal({ defaultScope: "team" });
    pick("report.pdf");
    expect(screen.getByTestId("kb-scope-team-skeleton")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-scope-team-lock")).not.toBeInTheDocument();
    expect(screen.queryByText("仅团队管理员")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-upload-submit")).toBeDisabled();

    // Admin identity arrives: placeholder disappears, unlocked/submittable (negative control).
    mocks.me = ME_ADMIN;
    rerenderSame();
    expect(screen.queryByTestId("kb-scope-team-skeleton")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-scope-team-lock")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-scope-team")).not.toBeDisabled();
    expect(screen.getByTestId("kb-scope-team")).toBeChecked();
    expect(screen.getByTestId("kb-upload-submit")).not.toBeDisabled();
  });

  it("非团队管理员：团队卡片锁定并标「仅团队管理员」", () => {
    renderModal({ defaultScope: "team" });
    expect(screen.getByTestId("kb-scope-team-lock")).toHaveTextContent("仅团队管理员");
    expect(screen.getByTestId("kb-scope-team")).toBeDisabled();
    // Unavailable default Team scope falls back to Personal.
    expect(screen.getByTestId("kb-scope-personal")).toBeChecked();
  });

  it("范围单选：Home/End 选中首/尾可用范围，跳过禁用的团队", () => {
    renderModal();
    const personal = screen.getByTestId("kb-scope-personal");
    personal.focus();
    fireEvent.keyDown(personal, { key: "End" });
    // Non-team-admin: Team disabled; End lands on Space.
    expect(screen.getByTestId("kb-scope-space")).toBeChecked();
    expect(document.activeElement).toBe(screen.getByTestId("kb-scope-space"));
    fireEvent.keyDown(screen.getByTestId("kb-scope-space"), { key: "Home" });
    expect(personal).toBeChecked();
    expect(document.activeElement).toBe(personal);
  });

  it("没有空间：空间卡片禁用并给出加入/创建空间入口", () => {
    mocks.me = { ...ME_MEMBER, workspace_id: "" };
    renderModal();
    expect(screen.getByTestId("kb-scope-space")).toBeDisabled();
    expect(screen.getByTestId("kb-scope-card-space")).toHaveTextContent("你还没有加入任何空间。");
    expect(screen.getByTestId("kb-upload-join-space")).toHaveAttribute(
      "href",
      expect.stringContaining("/empty-workspace/"),
    );
  });
});

describe("KbUploadModal · 类型不支持", () => {
  it("选中不支持的文件：写出文件名与支持列表，弹窗不关，不能提交", () => {
    const { onClose } = renderModal();
    pick("门店平面图-仓山店.dwg");
    const alert = screen.getByTestId("kb-upload-error");
    expect(alert).toHaveTextContent("门店平面图-仓山店.dwg 无法加入知识库。");
    expect(alert).toHaveTextContent("支持 PDF、Word、PPT、Excel、CSV、Markdown、TXT、图片、ZIP。");
    expect(alert).toHaveTextContent("可以导出为 PDF 后再上传。");
    expect(screen.getByTestId("kb-upload-filecard")).toHaveTextContent("不支持此格式");
    expect(screen.getByTestId("kb-upload-modal")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByTestId("kb-upload-submit")).toBeDisabled();

    // Negative control: selecting a supported file clears error and enables submission.
    pick("门店平面图-仓山店.pdf");
    expect(screen.queryByTestId("kb-upload-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-upload-submit")).not.toBeDisabled();
  });
});

describe("KbUploadModal · 上传前拦截", () => {
  it("explains a missing embedding model and blocks transfer before upload", () => {
    mocks.readiness = "missing";
    renderModal();
    pick("report.pdf");
    expect(screen.getByTestId("kb-ingestion-readiness")).toHaveTextContent("团队尚未配置并启用嵌入模型");
    expect(screen.getByTestId("kb-ingestion-readiness")).toHaveTextContent("请联系团队管理员");
    expect(screen.getByTestId("kb-upload-submit")).toBeDisabled();
    expect(MockXhr.instances).toHaveLength(0);
  });

  it("links team administrators to model settings from the upload dialog", () => {
    mocks.me = ME_ADMIN;
    mocks.readiness = "missing";
    renderModal();
    expect(screen.getByRole("link", { name: "配置嵌入模型" })).toHaveAttribute("href", "/team/settings/models/");
  });

  it("空文件给出可执行的修复提示", () => {
    renderModal();
    pick("empty.txt", 0);
    expect(screen.getByTestId("kb-upload-error")).toHaveTextContent("文件为空");
    expect(screen.getByTestId("kb-upload-error")).toHaveTextContent("请选择有内容的文件");
    expect(screen.getByTestId("kb-upload-submit")).toBeDisabled();
  });

  it("超过 200 MB 时说明如何处理", () => {
    renderModal();
    const file = new File(["x"], "large.pdf");
    Object.defineProperty(file, "size", { value: 201 * 1024 * 1024 });
    fireEvent.change(screen.getByTestId("kb-file-input"), { target: { files: [file] } });
    expect(screen.getByTestId("kb-upload-error")).toHaveTextContent("超过 200 MB");
    expect(screen.getByTestId("kb-upload-error")).toHaveTextContent("拆分或压缩");
    expect(screen.getByTestId("kb-upload-submit")).toBeDisabled();
  });
});

describe("KbUploadModal · 拖放文件", () => {
  it("拖入多个文件时明确拒绝整批，不静默选中第一个", () => {
    renderModal();
    pick("previous.txt");
    fireEvent.drop(screen.getByTestId("kb-drop-zone"), {
      dataTransfer: { files: [new File(["a"], "a.txt"), new File(["b"], "b.txt")] },
    });
    expect(screen.getByTestId("kb-upload-error")).toHaveTextContent("一次只能上传 1 个文件。你拖入了 2 个");
    expect(screen.queryByTestId("kb-upload-filecard")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-upload-submit")).toBeDisabled();

    pick("a.txt");
    expect(screen.queryByTestId("kb-upload-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-upload-submit")).not.toBeDisabled();
  });
});

describe("KbUploadModal · 上传过程与失败", () => {
  it("上传中显示目标范围与进度；传完前连接被断开 → 提示网关大小限制，重试原样重发", async () => {
    mocks.me = ME_ADMIN;
    renderModal({ defaultScope: "space" });
    pick("OBU QBR Template.pptx", 4000);
    const xhr = await startUpload();
    expect(screen.getByTestId("kb-upload-subtitle")).toHaveTextContent("上传到：空间 · 商超零售场景");
    await act(async () => xhr.progress(1500, 4000));
    expect(screen.getByTestId("kb-upload-filecard")).toHaveTextContent("38%");

    await act(async () => xhr.onerror?.());
    const alert = await screen.findByTestId("kb-upload-error");
    expect(alert).toHaveTextContent("上传在传输中被断开。");
    expect(alert).toHaveTextContent("可能超出了访问网关允许的文件大小");
    expect(screen.getByTestId("kb-upload-filecard")).toHaveTextContent("未上传");

    await act(async () => {
      fireEvent.click(screen.getByTestId("kb-upload-retry"));
    });
    expect(MockXhr.instances).toHaveLength(2);
    const fd = MockXhr.instances[1].body as FormData;
    expect(fd.get("scope")).toBe("space");
    expect(fd.get("scope_id")).toBe("ws_1");
    expect((fd.get("file") as File).name).toBe("OBU QBR Template.pptx");
  });

  it("请求没发出去就失败 → 普通网络失败提示", async () => {
    renderModal();
    pick("a.pdf");
    const xhr = await startUpload();
    await act(async () => xhr.onerror?.());
    const alert = await screen.findByTestId("kb-upload-error");
    expect(alert).toHaveTextContent("网络连接失败，请检查网络后重试。");
    expect(alert).not.toHaveTextContent("传输中被断开");
  });

  it("请求体已完整发出后才断开 → 不归为传输中断（按传输完成与否判定，不按大小）", async () => {
    renderModal();
    pick("big.pdf", 9_000_000);
    const xhr = await startUpload();
    await act(async () => {
      xhr.progress(9_000_000, 9_000_000);
      xhr.upload.onload?.();
      xhr.onerror?.();
    });
    const alert = await screen.findByTestId("kb-upload-error");
    expect(alert).toHaveTextContent("网络连接失败");
    expect(alert).not.toHaveTextContent("传输中被断开");
  });

  it("取消上传：中止请求并回到选择状态，不报错", async () => {
    renderModal();
    pick("a.pdf");
    const xhr = await startUpload();
    await act(async () => {
      fireEvent.click(screen.getByTestId("kb-upload-cancel"));
    });
    expect(xhr.aborted).toBe(true);
    expect(screen.queryByTestId("kb-upload-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-upload-submit")).not.toBeDisabled();
  });

  it("上传中关闭按钮不可误触；取消上传后恢复可关闭", async () => {
    const { onClose } = renderModal();
    pick("a.pdf");
    const xhr = await startUpload();
    const close = screen.getByRole("button", { name: "关闭上传窗口" });
    expect(close).toBeDisabled();
    fireEvent.click(close);
    expect(onClose).not.toHaveBeenCalled();
    expect(xhr.aborted).toBe(false);
    await act(async () => fireEvent.click(screen.getByTestId("kb-upload-cancel")));
    expect(close).not.toBeDisabled();
  });
});

describe("KbUploadModal · ZIP 结果", () => {
  it("子文件仍在解析时引导到列表看进度，不把入库说成全部完成", async () => {
    const { onUploaded } = renderModal();
    pick("batch.zip");
    const xhr = await startUpload();
    await act(async () => {
      xhr.status = 200;
      xhr.responseText = JSON.stringify({
        uploaded: [{ file_id: "kb_1", filename: "a.txt", parse_status: "processing", scope: "personal" }],
        skipped: [], total_entries: 1, bundle_filename: "batch.zip", bundle_artifact_id: "art_1",
      });
      xhr.onload?.();
    });
    expect(screen.getByTestId("kb-bundle-done")).toHaveTextContent("返回文件列表");
    expect(screen.getByText("解析会继续，进度可在文件列表查看。")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("kb-bundle-done"));
    expect(onUploaded).toHaveBeenCalledTimes(1);
  });
});

describe("KbUploadModal · Esc", () => {
  it("未上传时 Esc 关闭弹窗；上传进行中 Esc 不关闭、不中断上传", async () => {
    const idle = renderModal();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(idle.onClose).toHaveBeenCalledTimes(1);
    cleanup();

    const uploading = renderModal();
    pick("report.pdf");
    const xhr = await startUpload();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(uploading.onClose).not.toHaveBeenCalled();
    expect(xhr.aborted).toBe(false);
  });
});

describe("KbUploadModal · CSV", () => {
  it("CSV 可以选中并提交（知识库接受表格导出）", () => {
    renderModal();
    pick("门店销售明细.csv");
    expect(screen.queryByTestId("kb-upload-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-upload-submit")).not.toBeDisabled();
  });
});
