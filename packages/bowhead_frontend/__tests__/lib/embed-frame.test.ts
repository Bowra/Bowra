import { describe, expect, it } from "vitest";

import { embedFrame } from "@/lib/embed-frame";

const HOST = "https://bowhead.example.com";

describe("embedFrame", () => {
  it("frames an external https page with its own storage but no top navigation", () => {
    const frame = embedFrame("https://app.powerbi.com/view?r=1", HOST);
    expect(frame?.src).toBe("https://app.powerbi.com/view?r=1");
    expect(frame?.sandbox.split(" ")).toEqual(
      expect.arrayContaining(["allow-scripts", "allow-same-origin", "allow-forms", "allow-popups"]),
    );
    expect(frame?.sandbox).not.toContain("allow-top-navigation");
  });

  it("frames intranet http pages for local deployments", () => {
    expect(embedFrame("http://10.0.0.5:8080/dash", HOST)?.src).toBe("http://10.0.0.5:8080/dash");
  });

  it.each(["javascript:alert(1)", "data:text/html,<script>1</script>", "/relative/page", "not a url", ""])(
    "refuses to frame %j",
    (raw) => {
      expect(embedFrame(raw, HOST)).toBeNull();
    },
  );

  it("never pairs allow-scripts with allow-same-origin for Bowhead's own origin", () => {
    const frame = embedFrame(`${HOST}/reports/`, HOST);
    expect(frame?.sandbox).toContain("allow-scripts");
    expect(frame?.sandbox).not.toContain("allow-same-origin");
  });
});
