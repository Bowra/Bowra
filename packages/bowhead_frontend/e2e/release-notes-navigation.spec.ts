import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

for (const width of [1440, 390]) {
  test(`release notes: search and section navigation at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await mockAuthedApp(page);
    await page.goto("/release-notes/");
    const search = page.getByRole("searchbox");
    await expect(search).toBeVisible();
    const headings = page.locator("article h2");
    // Check actual layout: complete version groups and the footer must fit,
    // and an additional version must exceed the available height (unless capped).
    await expect
      .poll(async () =>
        page
          .locator("aside")
          .filter({ has: search })
          .evaluate((sidebar) => {
            const nav = sidebar.querySelector("nav")!;
            const groups = Array.from(
              nav.querySelectorAll<HTMLElement>("[data-release-version]"),
            );
            const visible = groups.filter(
              (group) => group.dataset.measuring === "false",
            );
            const button = nav.querySelector("button")!;
            const status = sidebar.querySelector('[role="status"]')!;
            const available =
              sidebar.clientHeight -
              parseFloat(getComputedStyle(sidebar).paddingBottom) -
              (nav.getBoundingClientRect().top -
                sidebar.getBoundingClientRect().top +
                sidebar.scrollTop) -
              status.getBoundingClientRect().height -
              parseFloat(getComputedStyle(status).marginTop);
            const used =
              visible.at(-1)!.getBoundingClientRect().bottom -
              nav.getBoundingClientRect().top;
            const footer =
              button.getBoundingClientRect().height +
              parseFloat(getComputedStyle(button).marginTop);
            const count = visible.length;
            const fits =
              count === 1 ||
              used + (count < groups.length ? footer : 0) <= available + 1;
            const next = groups[count];
            const full =
              count === Math.min(10, groups.length) ||
              used +
                next.scrollHeight +
                parseFloat(getComputedStyle(next).marginTop) +
                (count + 1 < groups.length ? footer : 0) >
                available - 1;
            return (
              count >= 1 &&
              count <= 10 &&
              fits &&
              full &&
              (count === 1 || sidebar.scrollHeight <= sidebar.clientHeight + 1)
            );
          }),
      )
      .toBe(true);
    const totalCount = await page.locator("[data-release-version]").count();
    const initialCount = await headings.count();
    await page.setViewportSize({ width, height: 3000 });
    await expect.poll(() => headings.count()).toBeGreaterThan(initialCount);
    await page.setViewportSize({ width, height: 900 });
    await expect(headings).toHaveCount(initialCount);
    const loadMore = page.getByRole("button", { name: /加载更多|Load more/ });
    await expect(loadMore).toBeVisible();
    await search.fill("v1.5.0");
    await expect(
      page.getByRole("link", { name: "v1.5.0", exact: true }),
    ).toBeVisible();
    await expect(page.locator("article h2")).toHaveCount(1);
    await expect(page.locator("article mark").first()).toHaveText("v1.5.0");
    await expect(page.locator("article time")).toHaveCount(1);
    await expect(page.locator("article h2").last()).toHaveText("v1.5.0");
    await search.fill("");
    await expect(headings).toHaveCount(initialCount);
    const firstSection = page.locator("article h3").first();
    const firstId = await firstSection.getAttribute("id");
    await page.locator(`nav a[href="#${firstId}"]`).click();
    // Move away from the explicit link before appending history.
    const readingState = await page.locator("article").evaluate((article) => {
      const root =
        getComputedStyle(article).overflowY === "visible"
          ? article.closest("main")!
          : article;
      root.scrollTop += 90;
      return { top: root.scrollTop, hash: location.hash };
    });
    await loadMore.click();
    if (width > 920) {
      await expect
        .poll(() =>
          page.locator("article").evaluate((article) => article.scrollTop),
        )
        .toBe(readingState.top);
    }
    const loadedCount = Math.min(totalCount, initialCount + 3);
    await expect(headings).toHaveCount(loadedCount);
    await page.setViewportSize({ width, height: 500 });
    await expect(headings).toHaveCount(loadedCount);
    await page.setViewportSize({ width, height: 900 });
    await expect(headings).toHaveCount(loadedCount);
    while (await loadMore.count()) {
      await loadMore.click();
    }
    await expect(
      page.getByRole("link", { name: "v1.5.0", exact: true }),
    ).toBeVisible();
    await expect(page.locator("article h2").last()).toHaveText("v1.5.0");
    await expect(
      page.getByRole("status").filter({ hasText: /全部|All/ }),
    ).toBeVisible();
    const heading = page.locator("article h3").last();
    const title = (await heading.textContent())!;
    const id = await heading.getAttribute("id");
    const nav = page.getByRole("navigation", {
      name: /版本与小节|Versions and sections/,
    });
    await search.fill(title);
    const result = nav.locator(`a[href="#${id}"]`);
    await expect(result).toBeVisible();
    await result.focus();
    await page.keyboard.press("Enter");
    await expect(heading).toBeInViewport();
    await expect(result).toHaveAttribute("aria-current", "location");
    await expect(page).toHaveURL(new RegExp(`#${id}$`));
    await page.reload();
    await expect(page).toHaveURL(new RegExp(`#${id}$`));
    await expect(page.locator(`article [id="${id}"]`)).toBeInViewport();
    const body = page
      .locator("article h3")
      .first()
      .locator("xpath=following-sibling::p[1]");
    await search.fill((await body.textContent())!.trim().slice(0, 20));
    await expect(nav.locator("a[href$='section-1']")).not.toHaveCount(0);
    await search.fill("no-matching-release-phrase");
    await expect(nav.getByRole("link")).toHaveCount(0);
    await expect(
      page.getByRole("status").filter({ hasText: /未找到|No results/ }),
    ).toBeVisible();
    await search.fill("");
    await expect(nav.locator("a[href^='#release-v']").first()).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  });
}

test("release notes retain reading state through search and loading and follow manual scrolling", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockAuthedApp(page);
  await page.goto("/release-notes/");
  const article = page.locator("article");
  const sections = article.locator("h3");
  const firstId = (await sections.first().getAttribute("id"))!;
  const firstLink = page.locator(`nav a[href="#${firstId}"]`);
  await firstLink.click();
  await expect(firstLink).toHaveAttribute("aria-current", "location");
  await article.evaluate((body) => {
    body.scrollTop += 70;
  });
  const originalPosition = await article.evaluate((body) => body.scrollTop);
  const originalHash = await page.evaluate(() => location.hash);
  const initialCount = await article.locator("h2").count();
  const search = page.getByRole("searchbox");
  await search.fill("v1.5.0");
  await expect(article.locator("h2")).toHaveCount(1);
  await expect(article.locator("mark").first()).toHaveText("v1.5.0");
  await expect(article.locator("time")).toHaveAttribute(
    "datetime",
    /^\d{4}-\d{2}-\d{2}$/,
  );
  await search.fill("");
  await expect(article.locator("h2")).toHaveCount(initialCount);
  await expect
    .poll(async () =>
      Math.abs(
        (await article.evaluate((body) => body.scrollTop)) - originalPosition,
      ),
    )
    .toBeLessThan(1);
  const secondId = (await sections.nth(1).getAttribute("id"))!;
  await sections.nth(1).evaluate((heading) => heading.scrollIntoView());
  await expect(page.locator(`nav a[href="#${secondId}"]`)).toHaveAttribute(
    "aria-current",
    "location",
  );
  expect(await page.evaluate(() => location.hash)).toBe(originalHash);
  const readingPosition = await article.evaluate((body) => body.scrollTop);
  await page.getByRole("button", { name: /加载更多|Load more/ }).click();
  await expect
    .poll(async () =>
      Math.abs(
        (await article.evaluate((body) => body.scrollTop)) - readingPosition,
      ),
    )
    .toBeLessThan(1);
});
