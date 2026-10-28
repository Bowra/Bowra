import "@testing-library/jest-dom/vitest";
import { StrictMode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { undo, redo } from "@tiptap/pm/history";
import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";
import {
  parseInputDocument,
  type InputDocument,
  type ReferenceNode,
} from "@/lib/composer-input-document";
import {
  referenceEditorOptions,
  editorContentToInputDocument,
  updateReferenceParameters,
} from "@/lib/composer-reference-editor";
import type { ComposerPersistenceScope } from "@/lib/composer-persistence-scope";
import {
  ReferenceParameterPanel,
  type ReferenceParameterTarget,
} from "./reference-parameter-panel";

afterEach(cleanup);
const scope: ComposerPersistenceScope = {
  userId: "u_one",
  teamId: "t_one",
  workspaceId: "ws_one",
  requestedSessionId: null,
  authSessionGeneration: "auth_one",
};
const skill = {
  kind: "skill" as const,
  source_kind: "user" as const,
  id: "profitability",
  version: `sha256:${"a".repeat(64)}`,
};
const nodeId = "11111111-1111-1111-1111-111111111111";
const schema = () => ({
  type: "object",
  additionalProperties: false,
  required: ["company"],
  properties: {
    company: { type: "string", title: "Company", default: "Example" },
    period: {
      type: "string",
      title: "Period",
      enum: ["year", "quarter"],
      default: "quarter",
    },
    count: { type: "integer", title: "Count", minimum: 0 },
    enabled: { type: ["boolean", "null"], title: "Enabled" },
    date: { type: ["string", "null"], format: "date", title: "Date" },
    note: { type: ["string", "null"], title: "Note" },
    detail: {
      type: "object",
      title: "Detail",
      properties: { units: { type: "integer" } },
      additionalProperties: false,
      default: { units: 3 },
    },
  },
});
function doc(parameters: ReferenceNode["parameters"] = {}): InputDocument {
  return parseInputDocument({
    schema_version: 1,
    nodes: [
      { type: "text", text: "Analyse " },
      {
        type: "reference",
        node_id: nodeId,
        resource: skill,
        label: "杜邦分析",
        parameters,
      },
    ],
  });
}
function defaults(parameters: ReferenceNode["parameters"] = {}) {
  const document = doc(parameters);
  return {
    scope,
    document,
    target: {
      scope,
      editId: "edit_one",
      document,
      nodeId,
      resource: skill,
      parameterSchema: schema(),
    } satisfies ReferenceParameterTarget,
    onCommitIntent: vi.fn(),
    onDismiss: vi.fn(),
  };
}
type Props = React.ComponentProps<typeof ReferenceParameterPanel>;
function ui(props: Props, locale = "en-US") {
  return (
    <StrictMode>
      <NextIntlClientProvider
        locale={locale}
        messages={locale === "en-US" ? en : zh}
      >
        <ReferenceParameterPanel {...props} />
      </NextIntlClientProvider>
    </StrictMode>
  );
}
const apply = () => screen.getByRole("button", { name: "Apply Parameters" });
const field = (name: string) =>
  screen.getByRole("region", { name: new RegExp(`^${name}`) });
const change = (name: string, value: string) =>
  fireEvent.change(
    screen.getByLabelText(new RegExp(`^${name}`), {
      selector: "input,textarea,select",
    }),
    {
      target: { value },
    },
  );
function params(props: ReturnType<typeof defaults>) {
  expect(props.onCommitIntent).toHaveBeenCalledOnce();
  return props.onCommitIntent.mock.calls[0][0].parameters;
}

describe("real parameter panel and editor contract", () => {
  it("offers visible defaults without writing or silently supplying a complex default; cancellation discards edits", () => {
    const props = defaults();
    render(ui(props));
    expect(
      screen.getByLabelText(/^Company/, { selector: "input,textarea,select" }),
    ).toHaveValue("Example");
    expect(
      screen.getAllByText("Default value, confirmed when you apply."),
    ).toHaveLength(2);
    change("Company", "Changed");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(props.onCommitIntent).not.toHaveBeenCalled();
    expect(props.document).toEqual(doc());
    expect(props.onDismiss).toHaveBeenCalledWith("edit_one");
  });
  it("explicitly confirms scalar defaults and emits detached ownership, source and parameters once", () => {
    const props = defaults({ detail: { units: 2 } });
    render(ui(props));
    const button = apply();
    const key = Object.keys(button).find((key) =>
      key.startsWith("__reactProps$"),
    )!;
    const retained = (
      button as unknown as Record<string, { onClick: () => void }>
    )[key].onClick;
    fireEvent.click(button);
    act(retained);
    expect(props.onCommitIntent).toHaveBeenCalledOnce();
    expect(params(props)).toEqual({
      company: "Example",
      period: "quarter",
      detail: { units: 2 },
    });
    const intent = props.onCommitIntent.mock.calls[0][0];
    expect(intent).toMatchObject({
      scope,
      editId: "edit_one",
      sourceDocument: props.document,
      nodeId,
      resource: skill,
    });
    intent.scope.userId = "changed";
    intent.resource.id = "changed";
    intent.sourceDocument.nodes.length = 0;
    intent.parameters.detail.units = 9;
    expect(props.document).toEqual(doc({ detail: { units: 2 } }));
    expect(scope.userId).toBe("u_one");
    expect(skill.id).toBe("profitability");
  });
  it("preserves false, zero, null, multiline whitespace, empty strings and explicit omission", () => {
    const props = defaults({
      company: "",
      enabled: false,
      count: 0,
      date: null,
      note: " \n\n",
    });
    render(ui(props));
    expect(
      screen.getByLabelText(/^Company/, { selector: "input,textarea,select" }),
    ).toHaveValue("");
    expect(
      screen.getByLabelText(/^Count/, { selector: "input,textarea,select" }),
    ).toHaveValue("0");
    expect(
      screen.getByLabelText(/^Note/, { selector: "input,textarea,select" }),
    ).toHaveValue(" \n\n");
    fireEvent.click(
      within(field("Company")).getByRole("button", { name: "Complete Later" }),
    );
    fireEvent.click(apply());
    expect(params(props)).toEqual({
      enabled: false,
      count: 0,
      date: null,
      note: " \n\n",
      period: "quarter",
    });
  });
  it("allows missing required draft values without claiming execution readiness", () => {
    const props = defaults();
    render(ui(props));
    fireEvent.click(
      within(field("Company")).getByRole("button", { name: "Complete Later" }),
    );
    expect(apply()).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "execution requires complete parameters and authorization",
    );
    fireEvent.click(apply());
    expect(params(props)).toEqual({ period: "quarter" });
  });
  it.each([
    "-",
    "1.",
    "1e",
    " 1",
    "0x10",
    "Infinity",
    "9007199254740992",
    "-1",
    "1.5",
  ])(
    "keeps incomplete or invalid integer %s editable, without coercing it",
    (value) => {
      const props = defaults();
      render(ui(props));
      change("Count", value);
      expect(
        screen.getByLabelText(/^Count/, { selector: "input,textarea,select" }),
      ).toHaveValue(value);
      expect(apply()).toBeDisabled();
      fireEvent.click(apply());
      expect(props.onCommitIntent).not.toHaveBeenCalled();
      change("Count", "0");
      fireEvent.click(apply());
      expect(params(props).count).toBe(0);
    },
  );
  it("uses literal ISO dates, explicit null and meaningful boolean/enum choices", () => {
    const props = defaults();
    render(ui(props));
    change("Date", "2024-02-29");
    change("Enabled", "1");
    change("Period", "0");
    fireEvent.click(
      within(field("Note")).getByRole("button", {
        name: "Leave Empty Explicitly",
      }),
    );
    fireEvent.click(apply());
    expect(params(props)).toMatchObject({
      date: "2024-02-29",
      enabled: false,
      period: "year",
      note: null,
    });
  });
  it("does not auto-fill hidden complex defaults, and lets users explicitly clear existing structured values", () => {
    const props = defaults({ detail: { units: 2 } });
    render(ui(props));
    fireEvent.click(
      within(field("Detail")).getByRole("button", { name: "Complete Later" }),
    );
    fireEvent.click(apply());
    expect(params(props)).toEqual({ company: "Example", period: "quarter" });
  });
  it("preserves unsupported existing parameters and blocks instead of silently discarding their names or values", () => {
    const props = defaults({ private_unknown: "private_value" });
    render(ui(props));
    expect(apply()).toBeDisabled();
    expect(document.body.textContent).not.toContain("private_unknown");
    expect(document.body.textContent).not.toContain("private_value");
    expect(props.document).toEqual(doc({ private_unknown: "private_value" }));
  });
  it("blocks an edit that fits the parameter budget but exceeds the whole document budget", () => {
    const props = defaults();
    props.document.nodes.unshift({ type: "text", text: "a".repeat(261000) });
    render(ui(props));
    change("Note", "a".repeat(2000));
    expect(apply()).toBeDisabled();
    expect(props.onCommitIntent).not.toHaveBeenCalled();
  });
  it.each([
    "userId",
    "teamId",
    "workspaceId",
    "requestedSessionId",
    "authSessionGeneration",
  ] as const)("masks an old panel immediately when %s changes", (key) => {
    const props = defaults();
    const view = render(ui(props));
    const old = apply();
    view.rerender(ui({ ...props, scope: { ...scope, [key]: "different" } }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.textContent).not.toContain("杜邦分析");
    fireEvent.click(old);
    expect(props.onCommitIntent).not.toHaveBeenCalled();
  });
  it.each([
    "userId",
    "teamId",
    "workspaceId",
    "authSessionGeneration",
  ] as const)(
    "refuses incomplete %s even when the target and owner match",
    (key) => {
      const props = defaults();
      props.scope = { ...scope, [key]: "" };
      props.target.scope = props.scope;
      render(ui(props));
      expect(screen.queryByRole("dialog")).toBeNull();
    },
  );
  it.each(["version", "id", "source_kind"] as const)(
    "requires exact schema resource %s",
    (key) => {
      const props = defaults();
      props.target.resource = { ...skill, [key]: "different" };
      render(ui(props));
      expect(screen.queryByRole("dialog")).toBeNull();
    },
  );
  it.each(["same", "connection_binding_id", "connection_subject_fingerprint", "local_contract_revision", "operation_contract_revision"])("binds the schema to the complete Tool identity: %s", (field) => {
    const props: Props = defaults();
    const resource = {
      kind: "mcp_tool" as const, source_id: "ots_finance",
      local_contract_revision: `sha256:${"1".repeat(64)}`,
      connection_binding_id: "ots_finance", connection_subject_fingerprint: `sha256:${"2".repeat(64)}`,
      operation_name: "teammcp.ots_finance.margin", operation_contract_revision: `sha256:${"3".repeat(64)}`,
    };
    props.document = parseInputDocument({ ...props.document, nodes: [{ type: "reference", node_id: nodeId, resource, label: "Margin", parameters: {} }] });
    // Metadata JSON property order does not change the exact resource identity.
    const metadata = Object.fromEntries(Object.entries(resource).reverse());
    if (field !== "same") metadata[field] = field === "connection_binding_id" ? "ots_other" : `sha256:${"f".repeat(64)}`;
    props.target = { ...props.target!, document: props.document, resource: metadata as typeof resource };
    render(ui(props));
    if (field === "same") {
      change("Company", "Example Company");
      fireEvent.click(apply());
      expect(props.onCommitIntent).toHaveBeenCalledWith(expect.objectContaining({ resource, parameters: expect.objectContaining({ company: "Example Company" }) }));
    } else {
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(props.onCommitIntent).not.toHaveBeenCalled();
    }
  });
  it("invalidates changed document and removed tag, while reopening starts a fresh edit", () => {
    const props = defaults();
    const view = render(ui(props));
    change("Company", "Unsaved");
    const old = apply();
    view.rerender(ui({ ...props, document: doc({ company: "New" }) }));
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(old);
    expect(props.onCommitIntent).not.toHaveBeenCalled();
    view.rerender(
      ui({ ...props, target: { ...props.target, editId: "edit_two" } }),
    );
    expect(
      screen.getByLabelText(/^Company/, { selector: "input,textarea,select" }),
    ).toHaveValue("Example");
    view.rerender(ui({ ...props, document: { schema_version: 1, nodes: [] } }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("resets local edits when the parameter contract changes", () => {
    const props = defaults();
    const view = render(ui(props));
    change("Company", "Unsaved");
    const updated = schema();
    updated.properties.company.default = "New default";
    view.rerender(
      ui({ ...props, target: { ...props.target, parameterSchema: updated } }),
    );
    expect(
      screen.getByLabelText(/^Company/, { selector: "input,textarea,select" }),
    ).toHaveValue("New default");
  });
  it("does not commit when read-only, unmounted, or during IME confirmation", () => {
    const props = defaults();
    const view = render(ui(props));
    const button = apply();
    fireEvent.compositionStart(
      screen.getByLabelText(/^Company/, { selector: "input,textarea,select" }),
    );
    fireEvent.click(button);
    fireEvent.keyDown(screen.getByRole("dialog"), {
      key: "Escape",
      isComposing: true,
    });
    expect(props.onCommitIntent).not.toHaveBeenCalled();
    expect(props.onDismiss).not.toHaveBeenCalled();
    fireEvent.compositionEnd(
      screen.getByLabelText(/^Company/, { selector: "input,textarea,select" }),
    );
    view.rerender(ui({ ...props, readOnly: true }));
    expect(apply()).toBeDisabled();
    fireEvent.click(button);
    view.unmount();
    fireEvent.click(button);
    expect(props.onCommitIntent).not.toHaveBeenCalled();
  });
  it("contains Enter/Escape, supports cancellation and renders both locales without raw contracts", () => {
    const props = defaults();
    const bubble = vi.fn();
    const view = render(<div onKeyDown={bubble}>{ui(props)}</div>);
    fireEvent.keyDown(
      screen.getByLabelText(/^Company/, { selector: "input,textarea,select" }),
      { key: "Enter" },
    );
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(bubble).not.toHaveBeenCalled();
    expect(props.onDismiss).toHaveBeenCalledOnce();
    expect(document.body.textContent).not.toContain("sha256:");
    view.rerender(ui(props, "zh-CN"));
    expect(screen.getByRole("button", { name: "应用参数" })).toBeVisible();
    expect(screen.getByRole("dialog")).toHaveAccessibleName("编辑参数");
  });
  it("edits the real PM document as one undoable transaction and rejects an intent after another edit", () => {
    const props = defaults();
    const editor = new Editor(
      referenceEditorOptions(props.document, {
        ariaLabel: "Input",
        onChange: () => {},
        onRejected: () => {},
      }),
    );
    const captured = editor.state.doc;
    props.onCommitIntent.mockImplementation((intent) => {
      expect(
        updateReferenceParameters(
          captured,
          intent.nodeId,
          intent.parameters,
        )(editor.state, editor.view.dispatch),
      ).toBe(true);
    });
    render(ui(props));
    change("Company", "Confirmed");
    fireEvent.click(apply());
    expect(
      (editorContentToInputDocument(editor.state.doc).nodes[1] as ReferenceNode)
        .parameters.company,
    ).toBe("Confirmed");
    act(() => {
      undo(editor.state, editor.view.dispatch);
    });
    expect(editorContentToInputDocument(editor.state.doc)).toEqual(
      props.document,
    );
    act(() => {
      redo(editor.state, editor.view.dispatch);
    });
    expect(
      (editorContentToInputDocument(editor.state.doc).nodes[1] as ReferenceNode)
        .parameters.company,
    ).toBe("Confirmed");
    expect(
      updateReferenceParameters(captured, nodeId, { company: "Stale" })(
        editor.state,
        editor.view.dispatch,
      ),
    ).toBe(false);
    editor.destroy();
  });
  it("rejects retained callbacks after unmount, scope replacement or read-only changes", () => {
    // React detaches DOM event delegation on unmount. Retain its actual callback
    // too, so a dead DOM node cannot make a removed lifetime guard look covered.
    const retain = (button: HTMLElement) => {
      const key = Object.keys(button).find((key) =>
        key.startsWith("__reactProps$"),
      )!;
      return (button as unknown as Record<string, { onClick: () => void }>)[key]
        .onClick;
    };
    for (const mode of ["unmount", "scope", "readonly"]) {
      const props = defaults();
      const view = render(ui(props));
      const invoke = retain(apply());
      if (mode === "unmount") view.unmount();
      else if (mode === "scope")
        view.rerender(
          ui({ ...props, scope: { ...scope, workspaceId: "ws_other" } }),
        );
      else view.rerender(ui({ ...props, readOnly: true }));
      act(invoke);
      expect(props.onCommitIntent).not.toHaveBeenCalled();
      view.unmount();
    }
  });
  it("reconciles IME completion from native input even when compositionend is absent", () => {
    const props = defaults();
    render(ui(props));
    const input = screen.getByLabelText(/^Company/, { selector: "textarea" });
    fireEvent.compositionStart(input);
    fireEvent.input(input, {
      target: { value: "经营分析" },
      isComposing: false,
      inputType: "insertText",
    });
    fireEvent.click(apply());
    expect(params(props).company).toBe("经营分析");
  });
  it("escapes labels and metadata as text without evaluating markup", () => {
    const props = defaults();
    const markup = '<img src=x onerror="alert(1)">';
    (props.document.nodes[1] as ReferenceNode).label = markup;
    props.target.parameterSchema.properties.company.title = markup;
    render(ui(props));
    expect(document.querySelector("img")).toBeNull();
    expect(document.body.textContent).toContain(markup);
    fireEvent.click(apply());
    expect(params(props).company).toBe("Example");
  });
  it("refuses untrusted or unsupported schema metadata and lets the owner dismiss it", () => {
    const props = defaults();
    render(
      ui({
        ...props,
        target: {
          ...props.target,
          parameterSchema: { $ref: "https://private.example/schema" },
        },
      }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.textContent).not.toContain("private.example");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(props.onDismiss).toHaveBeenCalledWith("edit_one");
  });
});
