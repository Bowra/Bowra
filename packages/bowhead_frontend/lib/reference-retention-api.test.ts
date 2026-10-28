import { afterEach, describe, expect, it, vi } from "vitest";
import { apiJson } from "./api-client";
import { getReferenceRetention, setReferenceRetention } from "./reference-retention-api";

vi.mock("./api-client", async (original) => ({ ...await original<typeof import("./api-client")>(), apiJson: vi.fn() }));
afterEach(() => vi.resetAllMocks());

describe("Reference retention API", () => {
  it("sends the exact compare-and-set version, without a client-supplied team", async () => {
    vi.mocked(apiJson).mockResolvedValue({ days: 7, version: 4 });
    expect(await setReferenceRetention(7, 3)).toEqual({ days: 7, version: 4 });
    expect(apiJson).toHaveBeenCalledWith("/api/v1/team-settings/reference-retention", expect.objectContaining({
      method: "PUT", body: JSON.stringify({ days: 7, expected_version: 3 }),
    }));
  });
  it.each([null, { days: "7", version: 0 }, { days: 181, version: 0 }, { days: 1, version: -1 }, { days: 1, version: 1.1 }])("rejects an invalid read response", async (value) => {
    vi.mocked(apiJson).mockResolvedValue(value);
    await expect(getReferenceRetention()).rejects.toMatchObject({ code: "reference_retention_response_invalid" });
  });
  it.each([{ days: 30, version: 4 }, { days: 7, version: 3 }])("does not acknowledge another value or version", async (value) => {
    vi.mocked(apiJson).mockResolvedValue(value);
    await expect(setReferenceRetention(7, 3)).rejects.toMatchObject({ code: "reference_retention_response_invalid" });
  });
});
