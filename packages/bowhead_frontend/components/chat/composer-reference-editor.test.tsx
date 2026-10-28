import { StrictMode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import type { Editor, TiptapEditorHTMLElement } from "@tiptap/core";
import { TextSelection } from "@tiptap/pm/state";
import fixtures from "../../../../contracts/chat-references/catalog-v1.json";
import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";
import { apiJson, ApiError } from "@/lib/api-client";
import {
  inputDocumentFromText,
  inputDocumentDisplayText,
  parseInputDocument,
  type InputDocument,
} from "@/lib/composer-input-document";
import type { ComposerPersistenceScope } from "@/lib/composer-persistence-scope";
import {
  ComposerReferenceEditor,
  type ComposerReferenceEditorSnapshot,
} from "./composer-reference-editor";

vi.mock("@/lib/api-client", async (original) => ({
  ...(await original<typeof import("@/lib/api-client")>()),
  apiJson: vi.fn(),
}));
const scope: ComposerPersistenceScope = {
  userId: "u_one",
  teamId: "t_one",
  workspaceId: "ws_one",
  requestedSessionId: null,
  authSessionGeneration: "auth_one",
};
const snapshot = (
  text = "",
  owner = scope,
  resetKey = "initial",
): ComposerReferenceEditorSnapshot => ({
  scope: owner,
  resetKey,
  document: inputDocumentFromText(text),
});
const defaults = () => ({
  scope,
  snapshot: snapshot(),
  onChange: vi.fn(),
  onSubmitIntent: vi.fn(),
});
type Props = React.ComponentProps<typeof ComposerReferenceEditor>;
function ui(props: Props, locale: "zh-CN" | "en-US" = "zh-CN") {
  return (
    <NextIntlClientProvider
      locale={locale}
      messages={locale === "zh-CN" ? zh : en}
    >
      <ComposerReferenceEditor {...props} />
    </NextIntlClientProvider>
  );
}
async function editor(): Promise<Editor> {
  const current = ((await screen.findByRole("combobox")) as TiptapEditorHTMLElement).editor!;
  // jsdom has no text-range geometry. Native scrolling/focus is covered by the browser fixture.
  current.setOptions({ editorProps: { ...current.options.editorProps, handleScrollToSelection: () => true } });
  return current;
}
async function type(value: string) {
  const current = await editor();
  await act(async () => {
    const { from, to } = current.state.selection;
    const handled = current.view.someProp("handleTextInput", (handler) =>
      handler(current.view, from, to, value, () =>
        current.state.tr.insertText(value, from, to),
      ),
    );
    expect(handled).toBeTruthy();
  });
}
async function press(key: string, more: KeyboardEventInit = {}) {
  fireEvent.keyDown(await screen.findByRole("combobox"), {
    key,
    keyCode: key === "Tab" ? 9 : key === "Enter" ? 13 : 0,
    ...more,
  });
}
function response(labels = ["经营分析师"]) {
  const result = structuredClone(fixtures.cases[0].response);
  const original = result.groups[0].items[0];
  result.groups[0].items = labels.map((label, i) => ({
    ...original,
    id: `osa_${i}`,
    label,
    resource: {
      ...original.resource!,
      id: `osa_${i}`,
      version: `arv_${i}`,
      assignment_id: `asa_${i}`,
      release_id: `arl_${i}`,
    },
  }));
  return result;
}
function selectableResponse() {
  const data = response(["经营分析师", "第二位分析师"]) as any;
  Object.assign(data.entry_support[0], {
    adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null,
  });
  data.groups[0].items.forEach((item: any) => Object.assign(item, { availability: "baseline", reason_code: null }));
  return data;
}
function pending() {
  let resolve!: (value: unknown) => void;
  const promise = new Promise<unknown>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
beforeEach(() => {
  vi.mocked(apiJson).mockReset();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("real React reference editor and catalog", () => {
  it.each(["Enter", "Tab", "click"])("selects an exact Agent with %s without submitting or ghost handling", async (key) => {
    const data = selectableResponse();
    vi.mocked(apiJson).mockResolvedValue(data);
    const props = { ...defaults(), onGhostKeyDown: vi.fn(() => false) };
    render(ui(props));
    await type("分析 ");
    await type("@");
    const option = (await screen.findAllByRole("option"))[0];
    expect(option.getAttribute("aria-disabled")).toBe("false");
    if (key === "click") fireEvent.click(option);
    else await press(key);
    const document: InputDocument = props.onChange.mock.calls.at(-1)![0];
    const refs = document.nodes.filter((node) => node.type === "reference");
    expect(refs).toEqual([{ type: "reference", node_id: expect.any(String), label: "经营分析师", parameters: {}, resource: data.groups[0].items[0].resource }]);
    expect(inputDocumentDisplayText(document)).toContain("分析 ");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("combobox")).toBe(window.document.activeElement);
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
    expect(props.onGhostKeyDown).not.toHaveBeenCalled();
    await press("Enter", { repeat: true });
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
    await press("Enter");
    expect(props.onSubmitIntent).toHaveBeenCalledWith(document, scope);
  });

  it("replaces the primary Agent in one undo step and preserves the surrounding task", async () => {
    vi.mocked(apiJson).mockResolvedValue(selectableResponse());
    const props = defaults();
    render(ui(props));
    await type("分析 ");
    await type("@");
    await screen.findAllByRole("option");
    await press("Enter");
    await type("@");
    await type("第二");
    await screen.findAllByRole("option");
    await press("End");
    await press("Tab");
    const replaced: InputDocument = props.onChange.mock.calls.at(-1)![0];
    expect(replaced.nodes.filter((node) => node.type === "reference").map((node) => node.resource.kind === "agent" ? node.resource.id : null)).toEqual(["osa_1"]);
    expect(inputDocumentDisplayText(replaced)).toContain("分析 ");
    await press("z", { ctrlKey: true });
    const restored: InputDocument = props.onChange.mock.calls.at(-1)![0];
    expect(restored.nodes.filter((node) => node.type === "reference").map((node) => node.resource.kind === "agent" ? node.resource.id : null)).toEqual(["osa_0"]);
    expect(inputDocumentDisplayText(restored)).toContain("@第二");
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
  });

  it("cannot select an obsolete row after the owner becomes read-only", async () => {
    vi.mocked(apiJson).mockResolvedValue(selectableResponse());
    const props = defaults();
    const mounted = render(ui(props));
    await type("@");
    const row = (await screen.findAllByRole("option"))[0];
    const before = props.onChange.mock.calls.length;
    mounted.rerender(ui({ ...props, readOnly: true }));
    fireEvent.click(row);
    await press("Enter");
    expect(props.onChange).toHaveBeenCalledTimes(before);
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox").textContent).toBe("@");
  });

  it.each(["zh-CN", "en-US"] as const)("labels each same-named Skill by its actual source in %s", async (locale) => {
    const samples = ["builtin_skill", "personal_skill", "team_skill"].map((name) => structuredClone(fixtures.cases.find((item) => item.name === name)!.response));
    const data = { ...samples[0], groups: samples[0].groups.map((group, index) => index === 0 ? { ...group, items: samples.map((sample) => ({ ...sample.groups[0].items[0], label: "杜邦分析" })) } : group) };
    vi.mocked(apiJson).mockResolvedValue(data);
    const props = defaults();
    render(ui(props, locale));
    await type("/");
    const options = await screen.findAllByRole("option");
    // Assert the source span within each row, not another group/label with the
    // same words. Removing this rendering must change the observable result.
    expect(options.map((option) => option.querySelector(".composer-reference-menu__source")?.textContent)).toEqual(locale === "zh-CN" ? ["内置 Skill", "个人 Skill", "团队 Skill"] : ["Built-in Skill", "Personal Skill", "Team Skill"]);
    expect(options.every((option) => option.getAttribute("aria-disabled") === "true")).toBe(true);
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
  });

  it.each(["zh-CN", "en-US"] as const)("distinguishes team and system Agent rows in %s", async (locale) => {
    const team = structuredClone(fixtures.cases.find((item) => item.name === "team_agent")!.response);
    const system = structuredClone(fixtures.cases.find((item) => item.name === "system_agent")!.response);
    const data = { ...team, groups: [team.groups[0], system.groups[1]].map((group) => ({ ...group, items: group.items.map((item) => ({ ...item, label: "同名分析师" })) })) };
    vi.mocked(apiJson).mockResolvedValue(data);
    const props = defaults();
    render(ui(props, locale));
    await type("@");
    const options = await screen.findAllByRole("option");
    expect(options.map((option) => option.querySelector(".composer-reference-menu__source")?.textContent)).toEqual(locale === "zh-CN" ? ["团队智能体", "系统智能体"] : ["Team Agent", "System Agent"]);
    expect(options.every((option) => option.getAttribute("aria-disabled") === "true")).toBe(true);
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
  });
  it("labels the viewer's own Agent as created by them, not as a team Agent", async () => {
    const team = structuredClone(fixtures.cases.find((item) => item.name === "team_agent")!.response);
    const data = { ...team, groups: team.groups.map((group, index) => index === 0 ? { ...group, items: group.items.map((item) => ({ ...item, created_by_me: true })) } : group) };
    vi.mocked(apiJson).mockResolvedValue(data);
    render(ui(defaults(), "zh-CN"));
    await type("@");
    const [option] = await screen.findAllByRole("option");
    expect(option.querySelector(".composer-reference-menu__source")?.textContent).toBe("我创建的");
  });
  it("mounts without rewriting the source or querying a restored literal", async () => {
    const props = { ...defaults(), snapshot: snapshot("@restored") };
    render(ui(props));
    expect((await screen.findByRole("combobox")).textContent).toBe("@restored");
    expect(props.onChange).not.toHaveBeenCalled();
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
    expect(apiJson).not.toHaveBeenCalled();
  });
  it("tells members an unbindable agent is still reachable through Lead", async () => {
    const data = response() as any;
    data.groups[0].items[0].reason_code = "unsupported_binding";
    data.groups[1].error = "unsupported_binding";
    vi.mocked(apiJson).mockResolvedValue(data);
    render(ui(defaults()));
    await type("@");
    const option = await screen.findByRole("option");
    expect(option.textContent).toContain("这个智能体目前无法直接指定，可以在对话里请 Lead 安排。");
    const groupMessage = document.querySelector(".composer-reference-menu__message");
    expect(groupMessage?.textContent).toContain("这个智能体目前无法直接指定");
    expect(document.body.textContent).not.toContain("暂时无法确认此资源的可用版本");
  });
  it("explains why a Skill without read-only content cannot be referenced", async () => {
    const data = structuredClone(fixtures.cases.find((item) => item.name === "team_skill")!.response) as any;
    data.groups[0].items[0].reason_code = "unsupported_binding";
    data.groups[0].items[0].availability = "unavailable";
    data.groups[0].items[0].read_only_parameter_schema = null;
    vi.mocked(apiJson).mockResolvedValue(data);
    render(ui(defaults()));
    await type("/");
    const option = await screen.findByRole("option");
    expect(option.textContent).toContain("此技能尚未提供可用于快捷引用的只读内容。");
    expect(option.textContent).not.toContain("请 Lead 安排");
    fireEvent.click(option);
    expect(document.body.textContent).toContain("此技能尚未提供可用于快捷引用的只读内容。");
  });
  it("explains the missing compatible receiver without suggesting a new permission path", async () => {
    const data = structuredClone(fixtures.cases.find((item) => item.name === "team_skill")!.response) as any;
    data.groups[0].items[0].reason_code = "unsupported_binding";
    data.groups[0].items[0].availability = "unavailable";
    data.groups[0].items[0].read_only_parameter_schema = { type: "object", properties: {}, additionalProperties: false };
    vi.mocked(apiJson).mockResolvedValue(data);
    render(ui(defaults()));
    await type("/");
    const option = await screen.findByRole("option");
    expect(option.textContent).toContain("请选择已配置此技能（含插件提供）的智能体，或本次任务允许选择的只读方法；也可请智能体负责人添加。");
    expect(option.textContent).not.toContain("请 Lead 安排");
  });
  it("reads the catalog on native input and exposes disabled reasons without a selection or send", async () => {
    vi.mocked(apiJson).mockResolvedValue(response());
    const props = defaults();
    render(ui(props));
    await type("@");
    const option = await screen.findByRole("option");
    expect(option.getAttribute("aria-disabled")).toBe("true");
    expect(option.textContent).toContain("此资源暂时无法用于当前任务");
    expect(
      (await screen.findByRole("combobox")).getAttribute(
        "aria-activedescendant",
      ),
    ).toBe(option.id);
    const inputBefore = props.onChange.mock.calls.length;
    fireEvent.click(option);
    await press("Enter");
    expect(document.querySelector(".composer-reference-editor__notice")?.classList.contains("composer-v2__sr-only")).toBe(true);
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
    expect(props.onChange).toHaveBeenCalledTimes(inputBefore);
    expect((await screen.findByRole("combobox")).textContent).toBe("@");
    expect(apiJson).toHaveBeenCalledOnce();
  });
  it("navigates disabled options for their reasons and keeps raw identities out of the DOM", async () => {
    vi.mocked(apiJson).mockResolvedValue(
      response(["经营分析师", '<img src=x onerror="alert(1)">']),
    );
    render(ui(defaults()));
    await type("@");
    const options = await screen.findAllByRole("option");
    await press("End");
    expect(
      screen.getByRole("combobox").getAttribute("aria-activedescendant"),
    ).toBe(options[1].id);
    // Layout/scroll can send pointer-enter with a stationary mouse after End.
    // Only subsequent pointer movement should take selection back from keys.
    fireEvent.pointerEnter(options[0]);
    expect(
      screen.getByRole("combobox").getAttribute("aria-activedescendant"),
    ).toBe(options[1].id);
    fireEvent.pointerMove(options[0]);
    expect(
      screen.getByRole("combobox").getAttribute("aria-activedescendant"),
    ).toBe(options[0].id);
    await press("Home");
    expect(
      screen.getByRole("combobox").getAttribute("aria-activedescendant"),
    ).toBe(options[0].id);
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByRole("listbox").textContent).not.toContain("arv_");
    expect(screen.getByRole("listbox").textContent).not.toContain("asa_");
    expect(screen.getByRole("listbox").textContent).not.toContain("sha256:");
  });
  it("keeps the full query when closing by button or outside click", async () => {
    vi.mocked(apiJson).mockResolvedValue(response());
    const props = defaults();
    render(ui(props));
    await type("@");
    await type("analyst");
    fireEvent.click(
      await screen.findByRole("button", { name: "继续输入文字" }),
    );
    await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
    expect(screen.getByRole("combobox").textContent).toBe("@analyst");
    await type(" ");
    await type("@");
    expect(await screen.findByRole("listbox")).toBeTruthy();
    fireEvent.pointerDown(document.body);
    await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
    expect(screen.getByRole("combobox").textContent).toBe("@analyst @");
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
  });
  it("empty-menu Tab and Shift-Enter cannot accept ghost or submit", async () => {
    const props = { ...defaults(), onGhostKeyDown: vi.fn(() => true) };
    render(ui(props));
    await type("@");
    await press("Enter", { shiftKey: true });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("combobox").textContent).toBe("@\n");
    await type("@");
    await press("Tab");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(props.onGhostKeyDown).not.toHaveBeenCalled();
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
  });
  it("only emits a submit intent after the menu is closed and task validation passes", async () => {
    const props = defaults();
    render(ui(props));
    await press("Enter");
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
    expect(screen.getByText("请输入任务或添加附件后再发送。")).toBeTruthy();
    await type("分析盈利");
    await press("Enter");
    expect(props.onSubmitIntent).toHaveBeenCalledWith(
      inputDocumentFromText("分析盈利"),
      scope,
    );
  });
  it("uses an unchanged source key for local editing, and explicit reset for an external replacement", async () => {
    const props = { ...defaults(), snapshot: snapshot("old") };
    const rendered = render(ui(props));
    const original = await editor();
    await act(async () =>
      original.view.dispatch(
        original.state.tr.setSelection(TextSelection.atEnd(original.state.doc)),
      ),
    );
    await type(" edit");
    rendered.rerender(
      ui({
        ...props,
        snapshot: {
          ...props.snapshot,
          document: inputDocumentFromText("old edit"),
        },
      }),
    );
    expect(await editor()).toBe(original);
    expect(screen.getByRole("combobox").textContent).toBe("old edit");
    rendered.rerender(
      ui({ ...props, snapshot: snapshot("recovered", scope, "replacement") }),
    );
    await waitFor(() =>
      expect(screen.getByRole("combobox").textContent).toBe("recovered"),
    );
    expect(await editor()).not.toBe(original);
  });
  it("hides a previous owner's draft immediately, ignores its late catalog and ignores old editor callbacks", async () => {
    const request = pending();
    vi.mocked(apiJson).mockReturnValueOnce(request.promise);
    const props = defaults();
    const rendered = render(ui(props));
    await type("@");
    await waitFor(() => expect(apiJson).toHaveBeenCalledOnce());
    const oldEditor = await editor();
    const other = {
      ...scope,
      workspaceId: "ws_other",
      authSessionGeneration: "auth_other",
    };
    const before = props.onChange.mock.calls.length;
    rendered.rerender(ui({ ...props, scope: other }));
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByRole("listbox")).toBeNull();
    await act(async () => {
      if (!oldEditor.isDestroyed)
        oldEditor.view.dispatch(oldEditor.state.tr.insertText("late"));
      request.resolve(response(["private old label"]));
    });
    expect(props.onChange).toHaveBeenCalledTimes(before);
    expect(screen.queryByText("private old label")).toBeNull();
    rendered.rerender(
      ui({ ...props, scope: other, snapshot: snapshot("new draft", other) }),
    );
    expect((await screen.findByRole("combobox")).textContent).toBe("new draft");
  });
  it("readOnly keeps local text and immediately closes and aborts the query", async () => {
    const request = pending();
    vi.mocked(apiJson).mockReturnValueOnce(request.promise);
    const props = defaults();
    const rendered = render(ui(props));
    await type("@");
    await type("analyst");
    await waitFor(() => expect(apiJson).toHaveBeenCalledOnce());
    rendered.rerender(ui({ ...props, readOnly: true }));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("combobox").getAttribute("contenteditable")).toBe(
      "false",
    );
    expect(screen.getByRole("combobox").textContent).toBe("@analyst");
    await press("Enter");
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
    await act(async () => request.resolve(response()));
    expect(screen.queryByRole("option")).toBeNull();
    rendered.rerender(ui(props));
    expect(screen.getByRole("combobox").getAttribute("contenteditable")).toBe(
      "true",
    );
    expect(screen.queryByRole("listbox")).toBeNull();
  });
  it("reports a finite network error and retries the current query", async () => {
    vi.mocked(apiJson).mockRejectedValueOnce(
      new ApiError(500, "secret backend stack"),
    );
    vi.mocked(apiJson).mockResolvedValueOnce(response());
    render(ui(defaults()));
    await type("@");
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "资源加载失败，请重试。",
    );
    expect(document.body.textContent).not.toContain("secret backend stack");
    await press("Enter");
    expect(await screen.findByRole("option")).toBeTruthy();
    expect(apiJson).toHaveBeenCalledTimes(2);
  });
  it("renders both languages and translates an existing notice on locale change", async () => {
    const props = defaults();
    const rendered = render(ui(props));
    await press("Enter");
    rendered.rerender(ui(props, "en-US"));
    expect(
      await screen.findByRole("combobox", { name: "Task input" }),
    ).toBeTruthy();
    expect(
      screen.getByText("Enter a task or add an attachment before sending."),
    ).toBeTruthy();
    await type("/");
    expect(
      screen.getByRole("button", { name: "Continue Typing" }),
    ).toBeTruthy();
  });
  it("retains a reference source and announces atomic removal", async () => {
    const value: InputDocument = {
      schema_version: 1,
      nodes: [
        {
          type: "reference",
          node_id: "11111111-1111-4111-8111-111111111111",
          label: "Agent",
          parameters: { nested: { keep: null } },
          resource: {
            kind: "agent",
            source_kind: "system",
            id: "agent",
            version: "r1",
          },
        },
        { type: "text", text: " task" },
      ],
    };
    const props = {
      ...defaults(),
      snapshot: { ...snapshot(), document: value },
    };
    render(ui(props));
    const current = await editor();
    expect(props.onChange).not.toHaveBeenCalled();
    await act(async () =>
      current.view.dispatch(
        current.state.tr.setSelection(
          TextSelection.create(current.state.doc, 2),
        ),
      ),
    );
    await press("Backspace");
    expect(props.onChange).toHaveBeenLastCalledWith(
      inputDocumentFromText(" task"),
      scope,
    );
    expect(screen.getByRole("status").textContent).toBe("已选引用：0");
  });
  it("preserves the exact unedited source when emitting intent", async () => {
    const value: InputDocument = {
      schema_version: 1,
      nodes: [
        { type: "text", text: "" },
        {
          type: "reference",
          node_id: "11111111-1111-4111-8111-111111111111",
          label: "Agent",
          parameters: {},
          resource: {
            kind: "agent",
            source_kind: "system",
            id: "agent",
            version: "r1",
          },
        },
        { type: "text", text: " task" },
      ],
    };
    const props = {
      ...defaults(),
      snapshot: { ...snapshot(), document: value },
    };
    render(ui(props));
    await press("Enter");
    expect(props.onSubmitIntent).toHaveBeenCalledWith(
      parseInputDocument(value),
      scope,
    );
    expect(props.onChange).not.toHaveBeenCalled();
  });
  it.each(["unmount", "invalid replacement"])(
    "ignores delayed editor callbacks after %s",
    async (action) => {
      const props = defaults();
      const rendered = render(ui(props));
      const current = await editor();
      const observed = vi.fn();
      current.on("update", observed);
      if (action === "unmount") rendered.unmount();
      else
        rendered.rerender(
          ui({
            ...props,
            snapshot: {
              ...snapshot(),
              document: {
                schema_version: 2,
                nodes: [],
              } as unknown as InputDocument,
            },
          }),
        );
      expect(screen.queryByRole("combobox")).toBeNull();
      // useEditor's destruction is deferred; deliver a real transaction in that gap.
      act(() => current.view.dispatch(current.state.tr.insertText("late")));
      expect(observed).toHaveBeenCalledOnce();
      expect(props.onChange).not.toHaveBeenCalled();
    },
  );
  it("keeps callback payloads detached from its local source", async () => {
    const props = defaults();
    props.onChange.mockImplementation((value) => {
      value.nodes[0].text = "accidental mutation";
    });
    render(ui(props));
    await type("task");
    await press("Enter");
    expect(props.onSubmitIntent).toHaveBeenCalledWith(
      inputDocumentFromText("task"),
      scope,
    );
    expect(screen.getByRole("combobox").textContent).toBe("task");
  });
  it("accepts an attachment-only task but blocks a memory command with references", async () => {
    const props = { ...defaults(), hasAttachments: true };
    const rendered = render(ui(props));
    await press("Enter");
    expect(props.onSubmitIntent).toHaveBeenCalledOnce();
    props.onSubmitIntent.mockClear();
    const value: InputDocument = {
      schema_version: 1,
      nodes: [
        {
          type: "reference",
          node_id: "11111111-1111-4111-8111-111111111111",
          label: "Agent",
          parameters: {},
          resource: {
            kind: "agent",
            source_kind: "system",
            id: "agent",
            version: "r1",
          },
        },
        { type: "text", text: "/remember preference" },
      ],
    };
    rendered.rerender(
      ui({
        ...props,
        snapshot: { ...snapshot("", scope, "command"), document: value },
      }),
    );
    await press("Enter");
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toContain("记忆命令");
    expect(props.onChange).not.toHaveBeenCalled();
  });
  it("loads another page once and keeps all rows disabled", async () => {
    vi.mocked(apiJson).mockResolvedValueOnce({
      ...response(Array.from({ length: 20 }, (_, index) => `Agent ${index}`)),
      next_cursor: "next",
    });
    const page = response(["second"]);
    page.groups[0].items[0].id = "osa_second";
    page.groups[0].items[0].resource!.id = "osa_second";
    const request = pending();
    vi.mocked(apiJson).mockReturnValueOnce(request.promise);
    render(ui(defaults()));
    await type("@");
    const button = await screen.findByRole("option", { name: "加载更多" });
    await press("End");
    expect(
      screen.getByRole("combobox").getAttribute("aria-activedescendant"),
    ).toBe(button.id);
    await press("Enter");
    fireEvent.click(button);
    await waitFor(() => expect(apiJson).toHaveBeenCalledTimes(2));
    expect(button.getAttribute("aria-disabled")).toBe("true");
    await act(async () => request.resolve(page));
    expect(await screen.findAllByRole("option")).toHaveLength(21);
    await press("End");
    expect(
      screen.getByRole("combobox").getAttribute("aria-activedescendant"),
    ).toBe(screen.getAllByRole("option")[20].id);
    expect(screen.queryByRole("option", { name: "加载更多" })).toBeNull();
  });
  it("displays unavailable slash groups without suggesting a pointless retry", async () => {
    vi.mocked(apiJson).mockResolvedValue(fixtures.cases[1].response);
    render(ui(defaults()));
    await type("/");
    await waitFor(() =>
      expect(screen.getAllByText("此类资源暂未开放。")).toHaveLength(3),
    );
    expect(screen.queryByRole("option")).toBeNull();
    expect(screen.queryByRole("option", { name: "重试" })).toBeNull();
    await press("Enter");
    expect(screen.getByRole("combobox").textContent).toBe("/");
  });
  it("keeps valid Agents selectable when one published listing is malformed", async () => {
    const data = selectableResponse();
    data.groups[0].error = "catalog_metadata_unavailable";
    vi.mocked(apiJson).mockResolvedValue(data);
    render(ui(defaults()));
    await type("@");
    expect((await screen.findAllByRole("option"))[0].getAttribute("aria-disabled")).toBe("false");
    expect(screen.getByText("部分资源暂时无法显示，仍可选择上方可用项。")).not.toBeNull();
    expect(screen.queryByText("暂时无法加载此类资源，请重试。")).toBeNull();
  });
  it.each([
    "userId",
    "teamId",
    "workspaceId",
    "authSessionGeneration",
    "requestedSessionId",
  ] as const)("does not mount a draft with an incomplete %s", async (key) => {
    const owner = { ...scope, [key]: "" };
    render(
      ui({
        ...defaults(),
        scope: owner,
        snapshot: snapshot("private draft", owner),
      }),
    );
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByText("private draft")).toBeNull();
    expect(apiJson).not.toHaveBeenCalled();
  });
  it("StrictMode does not rewrite the source or duplicate catalog reads", async () => {
    vi.mocked(apiJson).mockResolvedValue(response());
    const props = defaults();
    render(<StrictMode>{ui(props)}</StrictMode>);
    await editor();
    expect(props.onChange).not.toHaveBeenCalled();
    await type("@");
    await screen.findByRole("option");
    expect(apiJson).toHaveBeenCalledOnce();
  });
});

describe("parameter editing from a saved Skill token", () => {
  function setup(kind: "skill" | "capability" | "mcp_tool" | "mcp_prompt" = "skill") {
    const response: any = structuredClone(fixtures.cases.find((item) => item.name === "builtin_skill")!.response);
    let item = response.groups[0].items[0];
    if (kind !== "skill") {
      const binding = {
        source_id: "ots_finance", local_contract_revision: `sha256:${"1".repeat(64)}`,
        connection_binding_id: "ots_finance", connection_subject_fingerprint: `sha256:${"2".repeat(64)}`,
      };
      const resource = kind === "capability" ? { kind, id: "cap_visualize", version: 1 }
        : kind === "mcp_tool" ? { kind, ...binding, operation_name: "teammcp.ots_finance.margin", operation_contract_revision: `sha256:${"3".repeat(64)}` }
        : { kind, ...binding, prompt_name: "margin", prompt_contract_revision: `sha256:${"3".repeat(64)}` };
      item = {
        kind, source_kind: kind === "capability" ? "platform" : "team",
        id: kind === "capability" ? "cap_visualize" : kind === "mcp_tool" ? "teammcp.ots_finance.margin" : "mcpprompt.ots_finance.margin",
        label: item.label, summary: item.summary, resource,
        parameter_schema: item.read_only_parameter_schema,
        availability: "unavailable", reason_code: "unsupported_entry",
        ...(kind === "capability" ? {} : { source_id: binding.source_id, remote_version: { status: "unknown", value: null, evidence: null } }),
        ...(kind === "mcp_prompt" ? { prompt_name: "margin" } : {}),
      };
      response.groups.forEach((group: any) => { group.items = []; });
      response.groups[kind === "capability" ? 1 : 2] = { id: kind === "capability" ? "capabilities" : "mcp", error: null, items: [item] };
    }
    const document = parseInputDocument({ schema_version: 1, nodes: [
      { type: "text", text: "分析盈利 " },
      { type: "reference", node_id: "12345678-1234-4123-8123-123456789abc", resource: item.resource, label: item.label, parameters: { company: "Original" } },
    ] });
    const props = { ...defaults(), onCompositionStateChange: vi.fn(), snapshot: { ...snapshot(), document } };
    vi.mocked(apiJson).mockResolvedValue(response);
    return { response, props, item };
  }
  it.each(["skill", "capability", "mcp_tool", "mcp_prompt"] as const)("edits saved %s parameters in one undo and submits the same document", async (kind) => {
    const { props, item } = setup(kind);
    render(ui(props));
    const current = await editor();
    expect(apiJson).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "编辑 /杜邦分析 的参数" }));
    await screen.findByRole("dialog");
    expect(JSON.parse(vi.mocked(apiJson).mock.calls[0][1]!.body as string).exact_resource).toEqual(item.resource);
    const input = screen.getByRole("textbox", { name: /company/ });
    expect((input as HTMLInputElement).value).toBe("Original");
    fireEvent.compositionStart(input);
    fireEvent.compositionEnd(input);
    expect(props.onCompositionStateChange).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "Revised" } });
    fireEvent.click(screen.getByRole("button", { name: "应用参数" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(props.onChange).toHaveBeenCalledTimes(1);
    const saved = props.onChange.mock.calls[0][0] as InputDocument;
    expect(saved.nodes[1]).toEqual({ ...props.snapshot.document.nodes[1], parameters: { company: "Revised" } });
    await press("Enter");
    expect(props.onSubmitIntent).toHaveBeenCalledWith(saved, scope);
    await press("z", { ctrlKey: true });
    expect(props.onChange.mock.calls.at(-1)![0]).toEqual(props.snapshot.document);
  });
  it.each(["connection_binding_id", "connection_subject_fingerprint", "local_contract_revision", "operation_contract_revision"])("preserves a Tool draft when %s changes during lookup", async (field) => {
    const { props, response, item } = setup("mcp_tool");
    const deferred = pending();
    vi.mocked(apiJson).mockReturnValue(deferred.promise as any);
    render(ui(props));
    await editor();
    fireEvent.click(await screen.findByRole("button", { name: "编辑 /杜邦分析 的参数" }));
    item.resource[field] = field === "connection_binding_id" ? "ots_other" : `sha256:${"f".repeat(64)}`;
    await act(async () => deferred.resolve(response));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(props.onChange).not.toHaveBeenCalled();
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
  });
  it.each(["cancel", "readonly", "edited", "scope", "version"])("preserves full source when parameter editing is %s", async (change) => {
    const { props, response } = setup();
    const deferred = pending();
    vi.mocked(apiJson).mockReturnValue(deferred.promise as any);
    const mounted = render(ui(props));
    const current = await editor();
    fireEvent.keyDown(await screen.findByRole("button", { name: "编辑 /杜邦分析 的参数" }), { key: "Enter" });
    if (change === "readonly") mounted.rerender(ui({ ...props, readOnly: true }));
    if (change === "scope") mounted.rerender(ui({ ...props, scope: { ...scope, workspaceId: "ws_other" } }));
    if (change === "edited") await act(async () => { current.commands.insertContent("draft edit"); });
    if (change === "version") (response.groups[0].items[0].resource as any).version = "sha256:" + "c".repeat(64);
    await act(async () => deferred.resolve(response));
    if (change === "cancel") {
      await screen.findByRole("dialog");
      fireEvent.click(screen.getByRole("button", { name: "取消" }));
    }
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(props.onChange).toHaveBeenCalledTimes(change === "edited" ? 1 : 0);
    if (change === "edited") expect(props.onChange.mock.calls[0][0].nodes.find((n: any) => n.type === "reference")).toEqual(props.snapshot.document.nodes[1]);
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
  });
});

describe("Skill composition selection", () => {
  it.each(["baseline", "readonly_extension"] as const)("inserts a %s Skill with no implicit defaults or execution", async (mode) => {
    const response: any = structuredClone(fixtures.cases.find((item) => item.name === "builtin_skill")!.response);
    for (const cell of response.entry_support) if (["agent", "skill"].includes(cell.resource_kind)) Object.assign(cell, { adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null });
    const item = response.groups[0].items[0];
    Object.assign(item, { availability: mode, reason_code: null });
    vi.mocked(apiJson).mockResolvedValue(response);
    const agent = fixtures.cases[0].response.groups[0].items[0];
    const document = parseInputDocument({ schema_version: 1, nodes: [
      { type: "reference", node_id: "12345678-1234-4123-8123-123456789123", resource: agent.resource, label: agent.label, parameters: {} },
      { type: "text", text: " 分析盈利 " },
    ] });
    const props = { ...defaults(), snapshot: { ...snapshot(), document } };
    render(ui(props));
    await type("/");
    await screen.findByRole("option", { name: /杜邦分析/ });
    await press(mode === "baseline" ? "Enter" : "Tab");
    const changed: InputDocument = props.onChange.mock.calls.at(-1)![0];
    expect(changed.nodes.filter((node) => node.type === "reference").map((node) => node.resource)).toContainEqual(item.resource);
    expect(changed.nodes.find((node) => node.type === "reference" && node.resource.kind === "skill")).toMatchObject({ parameters: {} });
    expect(changed.nodes).toContainEqual(document.nodes[0]);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(props.onSubmitIntent).not.toHaveBeenCalled();
    await press("Enter");
    expect(props.onSubmitIntent).toHaveBeenCalledWith(changed, scope);
    await press("z", { ctrlKey: true });
    const restored: InputDocument = props.onChange.mock.calls.at(-1)![0];
    expect(restored.nodes.filter((node) => node.type === "reference")).toEqual([document.nodes[0]]);
    expect(inputDocumentDisplayText(restored)).toContain("/");
  });
});

// Behavior batch A (Menus Spec §4): completion lists end with the “↑↓ 选择 · ↵ 确认 · esc 关闭” keyboard-hint bar
// (CSS shows it only for desktop pointers and hides it from screen readers).
describe("「@」补全列表的键盘提示条", () => {
  it("ends the completion list with the keyboard hint bar hidden from screen readers", async () => {
    vi.mocked(apiJson).mockResolvedValue(response());
    render(ui(defaults()));
    await type("@");
    const listbox = await screen.findByRole("listbox");
    const menu = listbox.closest(".composer-reference-menu")!;
    const hints = menu.querySelector(".ui-menu-hints");
    expect(hints).not.toBeNull();
    expect(hints!.getAttribute("aria-hidden")).toBe("true");
    expect(hints!.textContent).toBe("↑↓选择↵确认esc关闭");
  });
});
