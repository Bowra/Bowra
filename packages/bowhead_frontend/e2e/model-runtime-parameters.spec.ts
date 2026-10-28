import { expect, test, type Page } from "@playwright/test";

const profile = {
  id: "dashscope-mixed-chat-v1",
  label: "DashScope mixed thinking",
  family: "openai",
  provider_types: ["openai_compatible"],
  thinking_modes: ["off", "on", "omit"],
  efforts: [],
  budget_min: 1,
  budget_max: 32768,
  temperature_max: 2,
  source: "https://help.aliyun.com/zh/model-studio/deep-thinking",
};

async function openEditor(page: Page, locale: string, colorScheme: string) {
  await page.route("**/api/v1/admin/llm-config/context-window-catalog", (route) => route.fulfill({ json: {
    default: 131072, models: {}, output_budget_defaults: {
      version: "model-output-budget.v1", automatic_ceiling: 256000, unknown_capacity: 32000, models: {},
    },
  } }));
  await page.addInitScript(
    (theme) => localStorage.setItem("bowhead.theme", theme),
    colorScheme,
  );
  await page
    .context()
    .addCookies([
      { name: "NEXT_LOCALE", value: locale, domain: "localhost", path: "/" },
    ]);
  await page.route(/\/api\/v1\/auth\/me$/, async (route) => {
    const response = await route.fetch();
    const user = await response.json();
    await route.fulfill({
      response,
      json: {
        ...user,
        team_role: "owner",
        is_team_admin: true,
        ui_language: locale,
      },
    });
  });
  await page.route("**/api/v1/admin/llm-config", async (route) => {
    await route.fulfill({
      json: {
        version: 1,
        config: {
          schema_version: 2,
          providers: {
            demo: {
              type: "openai_compatible",
              base_url: "https://example.test/v1",
              models: [
                {
                  id: "demo-model",
                  capabilities: ["chat", "tool", "vision"],
                  runtime_parameters: {
                    profile: profile.id,
                    defaults: { thinking_mode: "off" },
                  },
                },
              ],
            },
          },
          tiers: { primary: { provider: "demo", model: "demo-model" } },
          kb: { models: {}, active: {} },
        },
        providers: [],
        features: { model_discovery_v2: true, model_runtime_parameters: true },
        parameter_profiles: [profile],
        provider_identities: { demo: "llmp_00000000000000000000000000000000" },
      },
    });
  });
  await page.route("**/api/v1/theme/effective", async (route) => {
    const response = await route.fetch();
    const theme = await response.json();
    await route.fulfill({ response, json: { ...theme, effective: { ...theme.effective, mode: colorScheme } } });
  });
  await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: /(?:Edit|编辑).*demo/ }).click();
  await expect(page.locator("input[name='provider_model_output_capacity_0']")).toHaveValue("32000");
  await expect(page.locator(".mset-output-budget__policy")).toContainText("32,000");
  const section = page.getByTestId("runtime-parameters-0");
  await section.locator("summary").first().click();
  await expect(section.locator("select[id$='-thinking']")).toHaveValue("off");
  return section;
}

for (const scenario of [
  {
    name: "desktop-en",
    width: 1440,
    locale: "en-US",
    colorScheme: "light" as const,
  },
  {
    name: "narrow-en",
    width: 360,
    locale: "en-US",
    colorScheme: "light" as const,
  },
  {
    name: "narrow-zh",
    width: 412,
    locale: "zh-CN",
    colorScheme: "light" as const,
  },
  {
    name: "dark-zh",
    width: 1440,
    locale: "zh-CN",
    colorScheme: "dark" as const,
  },
]) {
  test.describe(scenario.name, () => {
    test.use({
      viewport: { width: scenario.width, height: 1000 },
      locale: scenario.locale,
      colorScheme: scenario.colorScheme,
    });
    test("retains defaults, edits one Tool override and safely previews without saving", async ({
      page,
      browserName,
    }, testInfo) => {
      let previews = 0;
      let writes = 0;
      page.on("request", (request) => {
        if (request.method() === "PUT") writes += 1;
      });
      await page.route(
        "**/api/v1/admin/llm-config/parameter-preview",
        async (route) => {
          previews += 1;
          const body = route.request().postDataJSON();
          expect(body.provider_draft.models[0]).not.toHaveProperty("max_output_tokens");
          expect(
            body.provider_draft.models[0].runtime_parameters.defaults
              .thinking_mode,
          ).toBe("off");
          expect(
            body.provider_draft.models[0].runtime_parameters
              .capability_overrides.tool,
          ).toEqual({ thinking_mode: "on" });
          await route.fulfill({
            json: {
              capability: body.capability,
              synthetic: true,
              provider_request_count: 0,
              configuration_writes: 0,
              parameters: [
                {
                  field: "enable_thinking",
                  value: true,
                  source: "capability_override",
                },
              ],
            },
          });
        },
      );
      const section = await openEditor(
        page,
        scenario.locale,
        scenario.colorScheme,
      );
      expect(previews).toBe(0);
      const profileInput = section.locator("select[id$='-profile']");
      await profileInput.focus();
      await page.keyboard.press(browserName === "webkit" ? "Alt+Tab" : "Tab");
      await expect(section.locator(".mset-runtime__source")).toBeFocused();
      await page.keyboard.press(browserName === "webkit" ? "Alt+Tab" : "Tab");
      await expect(
        section.getByRole("button", { name: /^(Model defaults|模型默认值)$/ }),
      ).toBeFocused();
      await section
        .getByRole("button", { name: /^(Capability overrides|按能力覆盖)/ })
        .click();
      await section.locator("select[id$='-thinking']").selectOption("on");
      await section
        .getByRole("button", { name: /^(Refresh preview|刷新预览)$/ })
        .click();
      await expect(section.locator(".mset-runtime__projection")).toContainText(
        "enable_thinking",
      );
      expect(previews).toBe(1);
      expect(writes).toBe(0);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth > window.innerWidth + 1,
      );
      expect(overflow).toBe(false);
      await expect(page.locator("html")).toHaveAttribute("data-theme", scenario.colorScheme);
      const metrics = await section.evaluate((element) => {
        const input = element.querySelector("input, select")!;
        const help = element.querySelector(".mset-runtime__help")!;
        const outputHelp = document.querySelector(".mset-output-budget__help")!;
        function luminance(color: string) {
          const rgb = color.match(/[\d.]+/g)!.slice(0, 3).map(Number).map((channel) => {
            const value = channel / 255;
            return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
          });
          return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
        }
        let surface = outputHelp.parentElement!;
        while (getComputedStyle(surface).backgroundColor === "rgba(0, 0, 0, 0)" && surface.parentElement) surface = surface.parentElement;
        const foreground = luminance(getComputedStyle(outputHelp).color);
        const background = luminance(getComputedStyle(surface).backgroundColor);

        return {
          outputContrast: (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05),
          width: element.getBoundingClientRect().width,
          foreground: getComputedStyle(help).color,
          background: getComputedStyle(element).backgroundColor,
          inputHeight: input.getBoundingClientRect().height,
          gap: getComputedStyle(
            element.querySelector(".mset-runtime__content")!,
          ).gap,
        };
      });
      expect(metrics.outputContrast).toBeGreaterThanOrEqual(4.5);
      await testInfo.attach("layout-metrics", {
        body: JSON.stringify(metrics),
        contentType: "application/json",
      });
      if (testInfo.project.name === "chromium-zh") {
        await page.locator(".mset-output-budget").evaluate((element) => element.scrollIntoView({ block: "center" }));
        await page.screenshot({
          path: `/private/tmp/model-parameters-${scenario.name}.png`,
        });
      }
    });
  });
}
