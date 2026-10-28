import "@testing-library/jest-dom/vitest";
import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import deMessages from "@/messages/de-DE.json";
import enMessages from "@/messages/en-US.json";
import frMessages from "@/messages/fr-FR.json";

const state = vi.hoisted(() => ({
  fetchOverview: vi.fn(),
  fetchSlack: vi.fn(),
  fetchTeams: vi.fn(),
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("@/lib/api", () => ({
  fetchIntegrationOverview: () => state.fetchOverview(),
}));
vi.mock("@/lib/slack-integration-api", () => ({
  fetchSlackAdminConfig: () => state.fetchSlack(),
  beginSlackInstall: vi.fn(),
  updateSlackAdminConfig: vi.fn(),
}));
vi.mock("@/lib/teams-integration-api", () => ({
  fetchTeamsAdminConfig: () => state.fetchTeams(),
  updateTeamsAdminConfig: vi.fn(),
}));

import { SettingsIntegrations } from "./integrations";
import { SlackIntegrationDetail } from "./slack-integration-detail";
import { TeamsIntegrationDetail } from "./teams-integration-detail";

const HAN = /\p{Script=Han}/u;
// User-visible error text must not include status codes such as “（500）”, “(404)” or “HTTP 502”.
const STATUS_CODE = /[（(]\s*[1-5]\d\d\s*[)）]|HTTP\s*[1-5]\d\d/;

/** All visible/screen-reader-readable page text: body plus accessible names, tooltips and placeholders. */
function userFacingText(root: HTMLElement): string {
  const attributes = Array.from(
    root.querySelectorAll("[aria-label], [title], [placeholder], [alt]"),
  ).flatMap((element) =>
    ["aria-label", "title", "placeholder", "alt"]
      .map((name) => element.getAttribute(name))
      .filter((value): value is string => Boolean(value)),
  );
  return [root.textContent ?? "", ...attributes].join("\n");
}

const overviewItems = {
  items: [
    { provider: "feishu", available: true, configured: true, enabled: true, healthy: true },
    { provider: "slack", available: true, configured: true, enabled: false, healthy: false },
    { provider: "teams", available: true, configured: false, enabled: false, healthy: false },
  ],
};

// Slack config with every step expanded: installation complete, callback URL and personal MCP all populated.
const slackInstalled = {
  configured: true,
  status: "verified",
  channel_answers_enabled: false,
  client_id: "C1",
  client_secret_configured: true,
  signing_secret_configured: true,
  event_url: "https://example.com/events",
  command_url: "https://example.com/commands",
  interaction_url: "https://example.com/interactions",
  oauth_callback_url: "https://example.com/oauth/callback",
  personal_mcp_oauth_callback_url: "https://example.com/personal-mcp/slack/oauth/callback",
  slack_settings: {
    events: "https://api.slack.com/apps/A1/event-subscriptions",
    slash_commands: "https://api.slack.com/apps/A1/slash-commands",
    interactivity: "https://api.slack.com/apps/A1/interactive-messages",
  },
};
const slackDraft = { configured: false, status: "draft", channel_answers_enabled: false };

const LOCALES = ["en-US", "de-DE", "fr-FR"] as const;
const CATALOGS = { "en-US": enMessages, "de-DE": deMessages, "fr-FR": frMessages } as const;

/** The integrations copy of the rendered locale, so each locale is checked against its own catalog. */
function copyFor(locale: (typeof LOCALES)[number]) {
  return CATALOGS[locale].settings.integrations;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("integration pages follow the interface language", () => {
  it.each(LOCALES)("overview has no Chinese in %s", async (locale) => {
    state.fetchOverview.mockResolvedValue(overviewItems);
    const { container } = renderIntl(<SettingsIntegrations />, { locale });

    expect(await screen.findByText(copyFor(locale).provider_status_healthy)).toBeInTheDocument();
    expect(screen.getByText("Feishu")).toBeInTheDocument();
    expect(screen.getByText("Microsoft Teams")).toBeInTheDocument();
    const text = userFacingText(container);
    expect(text).not.toMatch(HAN);
    expect(text).not.toMatch(STATUS_CODE);
  });

  it.each(LOCALES)("overview empty states have no Chinese in %s", async (locale) => {
    state.fetchOverview.mockResolvedValue({ items: [] });
    const { container } = renderIntl(<SettingsIntegrations />, { locale });

    expect(
      await screen.findByText(copyFor(locale).overview_planned_empty),
    ).toBeInTheDocument();
    expect(userFacingText(container)).not.toMatch(HAN);
  });

  it.each(LOCALES)("Slack setup with every step open has no Chinese in %s", async (locale) => {
    state.fetchSlack.mockResolvedValue(slackInstalled);
    const { container } = renderIntl(<SlackIntegrationDetail />, { locale });

    expect(await screen.findByRole("heading", { name: "Slack" })).toBeInTheDocument();
    const copyEventUrl = copyFor(locale).detail_common.copy_value.replace("{label}", "Event Request URL");
    expect(screen.getByRole("button", { name: copyEventUrl })).toBeInTheDocument();
    // Slack-console button names remain separately bolded in rich-text steps.
    expect(screen.getByText("User Token Scopes")).toBeInTheDocument();
    const text = userFacingText(container);
    expect(text).not.toMatch(HAN);
    expect(text).not.toMatch(STATUS_CODE);
  });

  it.each(LOCALES)("Slack setup before installation and its load error have no Chinese in %s", async (locale) => {
    state.fetchSlack.mockResolvedValueOnce(slackDraft);
    const first = renderIntl(<SlackIntegrationDetail />, { locale });
    expect(await screen.findByRole("heading", { name: "Slack" })).toBeInTheDocument();
    expect(userFacingText(first.container)).not.toMatch(HAN);
    first.unmount();

    state.fetchSlack.mockRejectedValueOnce(new Error("boom"));
    const second = renderIntl(<SlackIntegrationDetail />, { locale });
    expect(
      await screen.findByText(copyFor(locale).slack.load_failed),
    ).toBeInTheDocument();
    const text = userFacingText(second.container);
    expect(text).not.toMatch(HAN);
    expect(text).not.toMatch(STATUS_CODE);
  });

  it.each(LOCALES)("Teams setup and its guide have no Chinese in %s", async (locale) => {
    state.fetchTeams.mockResolvedValue({ configured: false, status: "draft", allowed_scopes: ["personal"] });
    const { container } = renderIntl(<TeamsIntegrationDetail />, { locale });

    fireEvent.click(await screen.findByRole("button", { name: copyFor(locale).teams.help_show }));
    expect(screen.getByRole("heading", { name: copyFor(locale).teams.help_title })).toBeInTheDocument();
    expect(screen.getByText("/api/v1/integrations/teams/messages")).toBeInTheDocument();
    const text = userFacingText(container);
    expect(text).not.toMatch(HAN);
    expect(text).not.toMatch(STATUS_CODE);
  });

  it.each(LOCALES)("Teams load error has no Chinese in %s", async (locale) => {
    state.fetchTeams.mockRejectedValue(new Error("boom"));
    const { container } = renderIntl(<TeamsIntegrationDetail />, { locale });

    expect(
      await screen.findByText(copyFor(locale).teams.load_failed),
    ).toBeInTheDocument();
    const text = userFacingText(container);
    expect(text).not.toMatch(HAN);
    expect(text).not.toMatch(STATUS_CODE);
  });

  it("keeps the Chinese interface in Chinese", async () => {
    state.fetchTeams.mockResolvedValue({ configured: false, status: "draft", allowed_scopes: ["personal"] });
    renderIntl(<TeamsIntegrationDetail />);

    fireEvent.click(await screen.findByRole("button", { name: "查看对接帮助" }));
    expect(screen.getByRole("heading", { name: "完成 Teams 对接" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "已验证" })).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(STATUS_CODE);
  });
});

describe("translated integration copy is not an English placeholder", () => {
  it.each(["de-DE", "fr-FR"] as const)("%s strings checked above differ from en-US", (locale) => {
    const en = copyFor("en-US");
    const own = copyFor(locale);
    const pick = (c: typeof en) => [
      c.provider_status_healthy,
      c.overview_planned_empty,
      c.detail_common.copy_value,
      c.slack.load_failed,
      c.teams.help_show,
      c.teams.help_title,
      c.teams.load_failed,
    ];
    const expected = pick(own);
    pick(en).forEach((enValue, i) => expect(expected[i]).not.toBe(enValue));
  });
});
