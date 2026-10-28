/** Create Tool card: parsing, authorization and creation failures show only localized explanations; raw API text and status codes go into Copy Diagnostics. */
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import de from "@/messages/de-DE.json";
import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

const previewSource = vi.hoisted(() => vi.fn());
const createSource = vi.hoisted(() => vi.fn());
const authorizeSource = vi.hoisted(() => vi.fn());
vi.mock("@/lib/team-tool-sources-api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  previewSource: (...args: unknown[]) => previewSource(...args),
  createSource: (...args: unknown[]) => createSource(...args),
  proposeDescriptions: vi.fn(),
  authorizeSource: (...args: unknown[]) => authorizeSource(...args),
}));

import { TeamToolApiError } from "@/lib/team-tool-sources-api";
import { CreateToolCard, createToolFailureKey } from "./create-tool-card";

afterEach(() => {
  cleanup();
  previewSource.mockReset();
  createSource.mockReset();
  authorizeSource.mockReset();
});

async function openOauthConfigure() {
  previewSource.mockResolvedValue({ route_away: false, kind: "mcp", needs_oauth: true, mcp_url: "https://mcp.example.com/mcp", provider_hint: "Example" });
  renderIntl(<CreateToolCard onClose={() => {}} />);
  fireEvent.click(screen.getByRole("tab", { name: "MCP 服务器" }));
  fireEvent.change(screen.getByPlaceholderText(/https:\/\//), { target: { value: "https://mcp.example.com/mcp" } });
  fireEvent.click(screen.getByRole("button", { name: /解析|Parse/ }));
  fireEvent.click(await screen.findByRole("button", { name: new RegExp(zh.create_tool.detect_oauth_cta) }));
  fireEvent.click(await screen.findByRole("button", { name: new RegExp(zh.create_tool.create_and_authorize.replace(/[→\s]+$/, "")) }));
}

describe("create and authorize", () => {
  it("a failure while creating the source is reported as a create failure, not an OAuth problem", async () => {
    // 500 copy differs by step (source creation: retry later; authorization: check OAuth configuration), making attribution distinguishable.
    createSource.mockRejectedValue(new TeamToolApiError(500, "db down"));
    await openOauthConfigure();

    expect(await screen.findByText((text) => text.includes(zh.create_tool.create_failed_retry))).toBeTruthy();
    expect(screen.queryByText((text) => text.includes(zh.create_tool.authorize_failed_hint))).toBeNull();
    expect(authorizeSource).not.toHaveBeenCalled();
  });

  it("a failure after the source exists is reported as an authorization failure", async () => {
    createSource.mockResolvedValue({ id: "src_1" });
    authorizeSource.mockRejectedValue(new TeamToolApiError(502, "upstream"));
    await openOauthConfigure();

    expect(await screen.findByText((text) => text.includes(zh.create_tool.authorize_failed_hint))).toBeTruthy();
  });
});

describe("create tool card parse failure", () => {
  it.each([
    ["zh-CN", zh, "复制诊断"],
    ["en-US", en, "Copy diagnostics"],
    ["de-DE", de, "Diagnoseinformationen kopieren"],
  ] as const)("%s: an unreadable spec says what to enter, not the server's words", async (locale, catalog, copy) => {
    previewSource.mockRejectedValue(new TeamToolApiError(422, "spec fetch failed: 404 Not Found", "spec_invalid", "0123456789abcdef0123456789abcdef"));
    renderIntl(<CreateToolCard onClose={() => {}} />, { locale });
    fireEvent.change(screen.getByPlaceholderText(/https:\/\//), { target: { value: "https://api.example.com/openapi.json" } });
    fireEvent.click(screen.getByRole("button", { name: /解析|Parse|Analysieren|Auslesen/ }));

    const reason = await screen.findByText((text) => text.includes(catalog.create_tool.parse_failed_invalid));
    expect(document.body.textContent).not.toMatch(/spec fetch failed|404 Not Found/);
    const button = screen.getByRole("button", { name: copy });
    expect(reason.contains(button)).toBe(false);
  });
});

describe("createToolFailureKey", () => {
  it.each([
    ["parse", { status: 422 }, "parse_failed_invalid"],
    ["parse", { status: 502 }, "parse_failed_unreachable"],
    ["parse", { status: 500 }, "parse_failed_retry"],
    ["parse", { status: 403 }, "failed_forbidden"],
    ["authorize", { status: 500 }, "authorize_failed_hint"],
    ["authorize", { status: 403 }, "failed_forbidden"],
    ["authorize", { status: 409 }, "create_failed_conflict"],
    ["authorize", { status: 422 }, "create_failed_invalid"],
    ["create", { status: 409 }, "create_failed_conflict"],
    ["create", { status: 422 }, "create_failed_invalid"],
    ["create", { status: 500 }, "create_failed_retry"],
    ["create", new TypeError("Failed to fetch"), "failed_network"],
  ] as const)("%s %o → %s", (step, cause, key) => {
    expect(createToolFailureKey(step, cause)).toBe(key);
  });
});
