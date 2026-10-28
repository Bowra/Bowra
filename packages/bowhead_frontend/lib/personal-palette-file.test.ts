import { describe, expect, it } from "vitest";

import {
  paletteFileToPersonalPalette,
  parsePersonalPaletteFile,
  PERSONAL_PALETTE_FILE_MAX_BYTES,
  PERSONAL_PALETTE_TEMPLATE,
} from "./personal-palette-file";

describe("personal palette files", () => {
  it("parses the versioned format and normalizes colors", () => {
    const parsed = parsePersonalPaletteFile(
      JSON.stringify({
        version: 1,
        name: "  Product blue  ",
        accent: "#2563eb",
        background: { light: "#f8fafc", dark: "#0f172a" },
      }),
    );

    expect(parsed).toEqual({
      version: 1,
      name: "Product blue",
      accent: "#2563EB",
      background: { light: "#F8FAFC", dark: "#0F172A" },
    });
    expect(paletteFileToPersonalPalette(parsed)).toEqual({
      accent: { kind: "custom", seed: "#2563EB" },
      background: {
        kind: "custom",
        light: "#F8FAFC",
        dark: "#0F172A",
      },
    });
  });

  it("ships a valid editable template", () => {
    expect(parsePersonalPaletteFile(PERSONAL_PALETTE_TEMPLATE).name).toBe(
      "My SaaS palette",
    );
  });

  it.each([
    ["unknown fields", '{"version":1,"name":"A","accent":"#2563EB","background":{"light":"#FFFFFF","dark":"#000000"},"extra":true}'],
    ["short colors", '{"version":1,"name":"A","accent":"#123","background":{"light":"#FFFFFF","dark":"#000000"}}'],
    ["missing dark mode", '{"version":1,"name":"A","accent":"#2563EB","background":{"light":"#FFFFFF"}}'],
    ["unversioned documents", '{"version":2,"name":"A","accent":"#2563EB","background":{"light":"#FFFFFF","dark":"#000000"}}'],
  ])("rejects %s", (_label, value) => {
    expect(() => parsePersonalPaletteFile(value)).toThrow();
  });

  it("rejects oversized files before parsing", () => {
    expect(() =>
      parsePersonalPaletteFile(" ".repeat(PERSONAL_PALETTE_FILE_MAX_BYTES + 1)),
    ).toThrow("palette_file_too_large");
  });
});
