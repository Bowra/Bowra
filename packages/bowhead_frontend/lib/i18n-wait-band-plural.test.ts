/**
 * Lock English plurals for the wait band's N Charts Produced text.
 *
 * On device (116, 2026-08-29, en-US turn): after the first report chart appeared, the wait band said **"1 charts produced"**.
 * Original copy used bare interpolation `{n} charts produced` with no ICU plural branch, failing at n=1.
 * Chinese has no plural inflection; lock only en-US.
 */

import { IntlMessageFormat } from "intl-messageformat";
import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";

type Bundle = { waitBand: Record<string, string> };

const band = (en as unknown as Bundle).waitBand;

describe("waitBand 英文复数", () => {
  it.each(["lede_produced", "aria_running"])("%s 的 n=1 用单数", (key) => {
    const one = new IntlMessageFormat(band[key], "en-US").format({ n: 1 }) as string;
    const many = new IntlMessageFormat(band[key], "en-US").format({ n: 3 }) as string;
    expect(one).toContain("1 chart");
    expect(one).not.toContain("1 charts");
    expect(many).toContain("3 charts");
  });
});
