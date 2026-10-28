import { expect, test, type Page, type Route } from "@playwright/test";

const PNG_1X1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
  "base64",
);

type DesignLayer = {
  id: string;
  type: "text";
  text: string;
  x: number;
  y: number;
  w: number;
  h: number;
  font_family?: string;
  font_size: number;
  font_weight?: number;
  color: string;
  align: "left" | "center" | "right";
  z?: number;
};

const initialLayers: DesignLayer[] = [
  {
    id: "headline",
    type: "text",
    text: "夏日冰爽",
    x: 0.12,
    y: 0.12,
    w: 0.76,
    h: 0.12,
    font_family: "Noto Sans SC",
    font_size: 76,
    font_weight: 800,
    color: "#12324a",
    align: "center",
    z: 10,
  },
  {
    id: "cta",
    type: "text",
    text: "第二杯半价",
    x: 0.18,
    y: 0.76,
    w: 0.64,
    h: 0.08,
    font_family: "Noto Sans SC",
    font_size: 48,
    color: "#ffffff",
    align: "center",
    z: 20,
  },
];

function designSection(layers: DesignLayer[], rev: number) {
  return {
    id: "sec_p31_design_demo",
    kind: "design",
    title: "Editable design demo",
    size_preset: "portrait_3_4",
    canvas_width: 1080,
    canvas_height: 1440,
    background: {
      type: "artifact",
      artifact_id: "art_000000000001",
      fit: "cover",
      opacity: 1,
    },
    layers,
    flattened_artifact_id: rev > 2 ? "art_flat_design" : null,
    flattened_source_rev: rev > 2 ? rev - 1 : null,
    flattened_at: rev > 2 ? "2026-06-10T00:00:00Z" : null,
    rev,
  };
}

async function mockDesignApis(page: Page) {
  let rev = 1;
  let layers = initialLayers.map((layer) => ({ ...layer }));
  const patches: Array<Record<string, unknown>> = [];
  const flattenBodies: Array<Record<string, unknown>> = [];

  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/files/art_000000000001?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "image/png",
      body: PNG_1X1,
    });
  });
  await page.route("**/api/v1/files/art_flat_design", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "image/png",
      headers: { "content-disposition": "attachment; filename=design.png" },
      body: PNG_1X1,
    });
  });
  await page.route("**/api/v1/metrics/event", async (route) => {
    await route.fulfill(json({ ok: true }));
  });
  await page.route(
    /\/api\/v1\/canvas\/cv_spike_p31\/sections\/sec_p31_design_demo\/flatten$/,
    async (route: Route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      flattenBodies.push(body);
      rev += 1;
      await route.fulfill(json({
        artifact_id: "art_flat_design",
        url: "/api/v1/files/art_flat_design",
        rev,
        section: designSection(layers, rev),
      }));
    },
  );
  await page.route(
    /\/api\/v1\/canvas\/cv_spike_p31\/sections\/sec_p31_design_demo$/,
    async (route: Route) => {
      if (route.request().method() !== "PATCH") {
        await route.fallback();
        return;
      }
      const body = route.request().postDataJSON() as {
        patch?: { layers?: DesignLayer[] };
      } & Record<string, unknown>;
      patches.push(body);
      const maybeLayers = body.patch?.layers;
      if (Array.isArray(maybeLayers)) {
        layers = maybeLayers.map((layer) => ({ ...layer }));
      }
      rev += 1;
      await route.fulfill(json({ rev }));
    },
  );

  return {
    patches,
    flattenBodies,
    latestLayers: () => layers,
  };
}

test.describe("editable design dogfood", () => {
  test("desktop can edit text and flatten the current design", async ({ page }) => {
    const api = await mockDesignApis(page);
    await page.goto("/spike/p31?designEdit=1");

    const shell = page.locator(".canvas-report-shell");
    await expect(shell).toHaveAttribute("data-edit-mode", "on");
    await expect(page.getByTestId("design-stage")).toBeVisible();
    await expect(page.getByRole("img", { name: "Editable design demo" })).toBeVisible();

    const ctaLayer = page.getByTestId("design-layer-cta");
    await ctaLayer.click();
    await expect(page.getByTestId("design-style-toolbar")).toBeVisible();

    const ctaText = ctaLayer.locator('[contenteditable="true"]');
    await expect(ctaText).toHaveText("第二杯半价");
    await ctaText.fill("第二杯 5 折");
    await ctaText.evaluate((el) => (el as HTMLElement).blur());

    await expect.poll(() => api.patches.length).toBeGreaterThan(0);
    expect(api.latestLayers().find((layer) => layer.id === "cta")?.text).toBe("第二杯 5 折");
    expect(api.patches.at(-1)).toMatchObject({
      patch: {
        layers: expect.arrayContaining([
          expect.objectContaining({ id: "cta", text: "第二杯 5 折" }),
        ]),
      },
      expected_rev: 1,
    });

    await page.getByTestId("design-flatten-button").click();
    await expect.poll(() => api.flattenBodies.length).toBe(1);
    expect(api.flattenBodies[0]).toEqual({ expected_rev: 2 });
  });

  test.describe("390px mobile", () => {
    test.use({ viewport: { width: 390, height: 844 } });

    test("keeps the design stage readable without horizontal overflow", async ({ page }) => {
      await mockDesignApis(page);
      await page.goto("/spike/p31?designEdit=1");

      const stage = page.getByTestId("design-stage");
      await expect(stage).toBeVisible();
      await expect(page.getByTestId("design-layer-headline")).toContainText("夏日冰爽");
      await expect(page.getByTestId("design-layer-cta")).toContainText("第二杯半价");

      const box = await stage.boundingBox();
      expect(box?.width ?? 0).toBeGreaterThan(240);
      expect(box?.width ?? 999).toBeLessThanOrEqual(390);
    });
  });
});
