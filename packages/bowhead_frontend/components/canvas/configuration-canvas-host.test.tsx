import "@testing-library/jest-dom/vitest";
import { useState } from "react";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { ConfigurationCanvasHost, ConfigurationCanvasStrip } from "./configuration-canvas-host";

afterEach(cleanup);

it.each([
  { locale: "en-US" as const, open: "Open…", label: "New Agent" },
  { locale: "zh-CN" as const, open: "打开…", label: "新建智能体" },
])("offers New Agent from the Open menu in $locale", async ({ locale, open, label }) => {
  const create = vi.fn();
  renderIntl(<ConfigurationCanvasHost selectedKey="a" onSelect={() => {}} openMenu={{ onNewAgent: create }} objects={[
    { key: "a", kind: "agent", title: "Sales", content: "Saved draft" },
  ]} />, { locale });
  fireEvent.click(screen.getAllByRole("button", { name: open })[0]);
  fireEvent.click(await screen.findByRole("menuitem", { name: label }));
  expect(create).toHaveBeenCalledOnce();
});

it("lists document drafts in the Open menu with message rows instead of disabled items", async () => {
  const open = vi.fn();
  renderIntl(<ConfigurationCanvasHost selectedKey="a" onSelect={() => {}} objects={[
    { key: "a", kind: "document", title: "Q2 review", content: "Preview" },
  ]} openMenu={{ documents: {
    items: [{ id: "djob_1", title: "Q2 review", detail: "Ready for review · 20 slides" }],
    loading: true, error: false, hasPrevious: false, hasNext: false,
    onOpen: open, onRefresh: () => {}, onPrevious: () => {}, onNext: () => {},
  } }} />, { locale: "en-US" });
  fireEvent.click(screen.getAllByRole("button", { name: "Open…" })[0]);
  const item = await screen.findByRole("menuitem", { name: /Q2 review/ });
  expect(item).toHaveTextContent("Ready for review · 20 slides");
  expect(screen.getByText("Loading drafts…")).not.toHaveAttribute("role", "menuitem");
  fireEvent.click(item);
  expect(open).toHaveBeenCalledWith("djob_1");
});

it("closes an object only from its tab ×, without selecting it", () => {
  const close = vi.fn();
  const select = vi.fn();
  renderIntl(<ConfigurationCanvasHost selectedKey="a" onSelect={select} objects={[
    { key: "a", kind: "document", title: "Quarterly review", content: "Preview", onClose: close },
  ]} />);
  expect(screen.getByRole("tab", { name: "Quarterly review" })).toBeVisible();
  const closeButton = screen.getAllByRole("button", { name: "关闭「Quarterly review」" })[0];
  act(() => closeButton.focus());
  expect(closeButton).toHaveFocus();
  fireEvent.click(closeButton);
  expect(close).toHaveBeenCalledOnce();
  expect(select).not.toHaveBeenCalled();
});

it("pins Results first, without a close control", () => {
  const results = vi.fn();
  renderIntl(<ConfigurationCanvasHost selectedKey="a" onSelect={() => {}} onResults={results} objects={[
    { key: "a", kind: "agent", title: "Sales", content: "Draft", onClose: () => {} },
  ]} />, { locale: "en-US" });
  const tabs = screen.getAllByRole("tab");
  expect(tabs[0]).toHaveAccessibleName("Results");
  expect(screen.queryByRole("button", { name: "Close “Results”" })).not.toBeInTheDocument();
  fireEvent.click(tabs[0]);
  expect(results).toHaveBeenCalledOnce();
});

it("names the status marker with the object (name · status word)", () => {
  renderIntl(<ConfigurationCanvasHost selectedKey="a" onSelect={() => {}} objects={[
    { key: "a", kind: "document", title: "Q2 review", marker: "attention", statusLabel: "Ready for review", content: "A" },
    { key: "b", kind: "agent", title: "Growth analyst", marker: "dirty", content: "B" },
  ]} />, { locale: "en-US" });
  expect(screen.getByRole("tab", { name: "Q2 review · Ready for review" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "Growth analyst · Unsaved" })).toBeInTheDocument();
});

it("tells identical names apart with an ordinal, never an internal ID", () => {
  renderIntl(<ConfigurationCanvasHost selectedKey="a" onSelect={() => {}} objects={[
    { key: "a", kind: "agent", title: "New Agent", content: "A" },
    { key: "b", kind: "agent", title: "New Agent", content: "B" },
  ]} />);
  expect(screen.getByRole("tab", { name: "New Agent · 第 1 份" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "New Agent · 第 2 份" })).toBeInTheDocument();
});

it("moves between tabs with arrows and closes the current object with Delete", () => {
  const close = vi.fn();
  const select = vi.fn();
  renderIntl(<ConfigurationCanvasHost selectedKey="a" onSelect={select} objects={[
    { key: "a", kind: "agent", title: "A", content: "A", onClose: close },
    { key: "b", kind: "agent", title: "B", content: "B" },
  ]} />);
  const tab = screen.getByRole("tab", { name: "A" });
  fireEvent.keyDown(tab, { key: "ArrowRight" });
  expect(select).toHaveBeenCalledWith("b");
  fireEvent.keyDown(tab, { key: "Delete" });
  expect(close).toHaveBeenCalledOnce();
});

it("keeps the close buttons out of the tablist while it owns every tab", () => {
  renderIntl(<ConfigurationCanvasHost selectedKey="a" onSelect={() => {}} objects={[
    { key: "a", kind: "agent", title: "A", content: "A", onClose: () => {} },
    { key: "b", kind: "agent", title: "B", content: "B", onClose: () => {} },
  ]} />);
  const tablist = screen.getByRole("tablist");
  const owned = tablist.getAttribute("aria-owns")!.split(" ");
  expect(owned).toEqual(screen.getAllByRole("tab").map((tab) => tab.id));
  // The tablist's own subtree holds no buttons; each × is a sibling, reachable by pointer, and Delete closes.
  expect(tablist.querySelectorAll("button")).toHaveLength(0);
  const close = screen.getAllByRole("button", { name: "关闭「A」" })[0];
  expect(close).toHaveAttribute("tabindex", "-1");
  expect(screen.getByRole("tab", { name: "A" })).toHaveAttribute("aria-keyshortcuts", "Delete");
});

it("shows the strip above Results with Results selected", () => {
  const select = vi.fn();
  renderIntl(<ConfigurationCanvasStrip onSelect={select} onResults={() => {}} objects={[
    { key: "a", kind: "skill", title: "Store summary", content: "Skill" },
  ]} />, { locale: "en-US" });
  expect(screen.getByRole("tab", { name: "Results" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: "Store summary" }));
  expect(select).toHaveBeenCalledWith("a");
});

function ClosableHost() {
  const [keys, setKeys] = useState(["a", "b"]);
  const [selected, setSelected] = useState("a");
  return <ConfigurationCanvasHost selectedKey={selected} onSelect={setSelected} objects={keys.map((key) => ({
    key, kind: "agent" as const, title: key, content: <input aria-label={`Input ${key}`} />,
    onClose: () => { setKeys((current) => current.filter((item) => item !== key)); setSelected("b"); },
  }))} />;
}

it("restores keyboard focus when its close control disappears", () => {
  renderIntl(<ClosableHost />);
  const close = screen.getAllByRole("button", { name: "关闭「a」" })[0];
  act(() => close.focus());
  fireEvent.click(close);
  expect(screen.getByRole("tab", { name: "b" })).toHaveFocus();
});

it("does not steal focus from the newer editor when another object closes", () => {
  renderIntl(<ClosableHost />);
  fireEvent.click(screen.getByRole("tab", { name: "b" }));
  const input = screen.getByRole("textbox", { name: "Input b" });
  act(() => input.focus());
  fireEvent.click(screen.getAllByRole("button", { name: "关闭「a」" })[0]);
  expect(input).toHaveFocus();
});
