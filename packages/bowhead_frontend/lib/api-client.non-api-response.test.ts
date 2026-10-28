/**
 * Recognize infrastructure HTML error pages instead of treating them as unknown business errors.
 *
 * A reverse proxy can reject a URL-bearing body with an HTML 403 before the application receives it.
 * Preserve that distinction so the UI points administrators toward gateway rules.
 *
 * `readApiError` had **already identified** HTML (`looksLikeHtml`) but discarded the finding,
 * like 116's probe computing actual_model_id without persisting it: the code knew the answer but did not expose it.
 */

import { describe, expect, it } from "vitest";
import { ApiError, readApiError } from "./api-client";

const AZURE_APPGW_403 = `<html>
<head><title>403 Forbidden</title></head>
<body>
<center><h1>403 Forbidden</h1></center>
<hr><center>Microsoft-Azure-Application-Gateway/v2</center>
</body>
</html>`;

function htmlResponse(status: number, body: string, contentType = "text/html"): Response {
  return new Response(body, {
    status,
    headers: { "content-type": contentType },
  });
}

describe("readApiError 识别「不是本应用答的」", () => {
  it("网关的 HTML 403 → nonApiResponse", async () => {
    const error = await readApiError(htmlResponse(403, AZURE_APPGW_403));

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(403);
    expect(error.nonApiResponse).toBe(true);
    // Do not put HTML into body as error detail (existing behavior, locked here too)
    expect(error.body).toBeNull();
  });

  it("没有 content-type 但正文是 HTML 也认得出（代理常不带正确头）", async () => {
    const error = await readApiError(htmlResponse(502, "<!DOCTYPE html><html><body>bad gateway</body></html>", "application/octet-stream"));

    expect(error.nonApiResponse).toBe(true);
  });

  it("本应用的 JSON 错误不得被误判（负对照）", async () => {
    const response = new Response(
      JSON.stringify({ detail: { code: "config_version_conflict" } }),
      { status: 409, headers: { "content-type": "application/json" } },
    );

    const error = await readApiError(response);

    expect(error.nonApiResponse).toBeFalsy();
    expect(error.code).toBe("config_version_conflict");
  });

  it("非 HTML 的纯文本错误也不算（负对照）", async () => {
    const error = await readApiError(new Response("upstream timeout", { status: 504 }));

    expect(error.nonApiResponse).toBeFalsy();
    expect(error.body).toBe("upstream timeout");
  });
});
