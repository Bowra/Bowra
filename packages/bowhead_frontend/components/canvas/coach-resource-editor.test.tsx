import { useState } from "react";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import { readCoachResourceEditor, type CoachResourceEditor as Projection } from "@/lib/coach-resource-editors";
import type { CoachResourceObject } from "@/lib/use-coach-resource-editors";
import { CoachResourceEditor } from "./coach-resource-editor";

vi.mock("@/lib/coach-resource-editors", async (original) => ({ ...await original<typeof import("@/lib/coach-resource-editors")>(), readCoachResourceEditor: vi.fn() }));
vi.mock("@/components/skill-drawer", () => ({ SkillDrawer: () => {
  const [text, setText] = useState("");
  return <textarea aria-label="Original editor" value={text} onChange={(event) => setText(event.target.value)} />;
} }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn() }) }));
afterEach(() => vi.clearAllMocks());
const object: CoachResourceObject = { key: "original", incarnation: "incarnation", title: "", editorRef: { current: null },
  pointer: { requestId: "a".repeat(32), userId: "u_owner", teamId: "team_original", workspaceId: "ws_original", sessionId: "s_original", turnId: "t_original",
    reference: { kind: "skill", source_kind: "user", id: "sk_original", revision: "sha256:original" }, parent: { kind: "agent_draft", id: "sad_parent", version: 1 } } };
const projection = { reference: object.pointer.reference, editor_kind: "skill", title: "Private resource", target: { kind: "skill", skill: { id: "sk_original", name: "method", source_type: "user_upload" } }, read_only: false } as Projection;

it("retains original input on failed revalidation and returns without disposing the child", async () => {
  vi.mocked(readCoachResourceEditor).mockResolvedValueOnce(projection).mockRejectedValue(new Error("Disconnected"));
  let select!: (active: boolean) => void;
  const back = vi.fn();
  function Host() {
    const [active, setActive] = useState(true); select = setActive;
    return <CoachResourceEditor object={object} active={active} onClose={vi.fn()} onActivate={vi.fn()} onTitle={vi.fn()} onReturn={back} />;
  }
  renderIntl(<Host />);
  fireEvent.change(await screen.findByRole("textbox"), { target: { value: "Retained input" } });
  fireEvent.click(screen.getByRole("button", { name: "返回智能体" }));
  expect(back).toHaveBeenCalledWith(object.pointer.parent);
  act(() => select(false)); act(() => select(true));
  await screen.findByRole("alert");
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Retained input");
});

it("clears the original editor on current access denial", async () => {
  vi.mocked(readCoachResourceEditor).mockResolvedValueOnce(projection).mockRejectedValue(new ApiError(404, "Unavailable"));
  let select!: (active: boolean) => void;
  const title = vi.fn();
  function Host() { const [active, setActive] = useState(true); select = setActive;
    return <CoachResourceEditor object={object} active={active} onClose={vi.fn()} onActivate={vi.fn()} onTitle={title} onReturn={vi.fn()} />; }
  renderIntl(<Host />);
  await screen.findByRole("textbox");
  act(() => select(false)); act(() => select(true));
  await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
  expect(title).toHaveBeenLastCalledWith("");
});
