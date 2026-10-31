import { createServer } from "node:http";
import { createSocket } from "node:dgram";
import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { buildInteractiveHtmlDocument } from "../../lib/interactive-html";
import { INTERACTIVE_HTML_PERMISSIONS, INTERACTIVE_HTML_SANDBOX } from "../../lib/html-preview-contract";

const escape = (value: string) => value.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
const fixture = `<!doctype html><html><head><title>Offline controls</title></head><body>
  <button id="count">0</button><button id="inline" onclick="this.textContent='clicked'">Inline</button>
  <form id="calculator"><input id="amount" type="number" value="3"><button>Calculate</button><output id="total"></output></form>
  <button id="fail" onclick="throw new Error('synthetic-private-detail')">Fail script</button><button id="tab">Details</button><section id="detail" hidden>Complete content</section><canvas id="chart" width="20" height="20"></canvas>
  <script>"use strict";
  let count=0;document.querySelector('#count').addEventListener('click',()=>document.querySelector('#count').textContent=String(++count));
  document.querySelector('form').addEventListener('submit',event=>{event.preventDefault();document.querySelector('#total').textContent=String(Number(document.querySelector('#amount').value)*2)});
  document.querySelector('#tab').onclick=()=>document.querySelector('#detail').hidden=false;
  const ctx=document.querySelector('canvas').getContext('2d');ctx.fillStyle='red';ctx.fillRect(0,0,20,20);
  document.body.dataset.strict=String((function(){return this})()===undefined);
  </script></body></html>`;

test("offline isolation blocks HTTP, navigation, host access, and WebRTC in fresh realms", async ({ page }) => {
  const hits: string[] = [];
  const packets: number[] = [];
  const udp = createSocket("udp4");
  udp.on("message", (packet) => packets.push(packet.length));
  await new Promise<void>((resolve) => udp.bind(0, "127.0.0.1", resolve));
  const address = udp.address();
  if (typeof address === "string") throw new Error("Expected a UDP address");
  let body = "";
  const server = createServer((request, response) => {
    if (request.url?.startsWith("/sink")) hits.push(request.url);
    response.setHeader("Content-Type", "text/html");
    response.setHeader("Set-Cookie", "host_guard=synthetic; SameSite=Lax");
    response.end(body);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const endpoint = server.address();
  if (!endpoint || typeof endpoint === "string") throw new Error("Expected an HTTP address");
  const origin = `http://127.0.0.1:${endpoint.port}`;
  const rtc = `try{const pc=new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:${address.port}'}]});pc.createDataChannel('probe');pc.createOffer().then(o=>pc.setLocalDescription(o));setTimeout(()=>pc.close(),1000);document.body.dataset.rtc='constructed'}catch(e){document.body.dataset.rtc=e.name}`;
  const wrap = (document: string) => `<!doctype html><iframe title="outer" sandbox="${INTERACTIVE_HTML_SANDBOX}" allow="${INTERACTIVE_HTML_PERMISSIONS}" srcdoc="${escape(document)}"></iframe>`;
  try {
    // Positive control: the UDP sink must detect an actual unguarded browser
    // connection before its absence can establish a regression boundary.
    body = wrap(`<script>${rtc}</script>`);
    await page.goto(origin);
    await expect.poll(() => packets.length).toBeGreaterThan(0);
    await page.waitForTimeout(1100);
    packets.length = 0;
    const attack = `<button id="navigate" onclick="location.href='${origin}/sink/navigation'">Navigate</button><button id="submit" onclick="document.querySelector('form').submit()">Submit</button><p id="result"></p><form action="${origin}/sink/form" method="post"><input name="q" value="fixture"></form><script>
      function Object(){};
      const outcomes={};for(const key of ['parent','cookie','storage']){
        try{outcomes[key]=String(key==='parent'?parent.document.body:key==='cookie'?document.cookie:localStorage.length)}catch(e){outcomes[key]=e.name}
      }
      document.querySelector('#result').textContent=JSON.stringify(outcomes);
      ${rtc};
      fetch('${origin}/sink/fetch').catch(()=>{});new Image().src='${origin}/sink/image';
      const f=document.createElement('iframe');f.srcdoc=${JSON.stringify(`<script>${rtc};parent.postMessage('unguarded-ran','*')</script>`)};document.body.append(f);
      addEventListener('message',event=>{if(event.data==='unguarded-ran')document.body.dataset.fresh='escaped'});
      document.body.dataset.prepared='yes';
      </script>`;
    // JSON script text must escape the HTML parser's end-tag delimiter.
    const prepared = await buildInteractiveHtmlDocument(attack.replace("</script>\"", "<\\/script>\""));
    body = wrap(prepared.document);
    await page.goto(origin);
    const inner = page.frameLocator('iframe[title="outer"]').frameLocator('iframe[title="HTML content"]');
    await expect(inner.locator("body")).toHaveAttribute("data-prepared", "yes");
    await expect(inner.locator("body")).toHaveAttribute("data-rtc", "TypeError");
    const outcomes = JSON.parse(await inner.locator("#result").innerText());
    expect(outcomes.parent).toBe("SecurityError");
    expect(outcomes.storage).toBe("SecurityError");
    expect(["SecurityError", ""]).toContain(outcomes.cookie);
    await page.waitForTimeout(1300);
    expect(hits).toEqual([]);
    expect(packets).toEqual([]);
    await expect(inner.locator("body")).not.toHaveAttribute("data-fresh", "escaped");
    await inner.locator("#submit").click();
    await page.waitForTimeout(150);
    expect(hits).toEqual([]);
    const copied = await buildInteractiveHtmlDocument(`<p id="root-marker"></p><output id="copy"></output><script>
      if(document.querySelector('#root-marker')){
        addEventListener('message',event=>{if(event.data==='copy-TypeError')document.querySelector('#copy').textContent=event.data});
        const child=document.createElement('iframe');
        child.srcdoc='<scr'+'ipt>'+document.currentScript.textContent+'</scr'+'ipt>';
        document.body.append(child);
      }else{
        try{new RTCPeerConnection();parent.postMessage('copy-escaped','*')}
        catch(error){parent.postMessage('copy-'+error.name,'*')}
      }
      </script>`);
    body = wrap(copied.document);
    await page.goto(origin);
    await expect(inner.locator("#copy")).toHaveText("copy-TypeError");
    expect(packets).toEqual([]);
    body = wrap(prepared.document);
    // Use a fresh document after a blocked submission: Chromium may show an
    // error document rather than preserving the current srcdoc.
    await page.goto(origin);
    await inner.locator("#navigate").click();
    await page.waitForTimeout(150);
    expect(hits).toEqual([]);
  } finally {
    await page.goto("about:blank");
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    udp.close();
  }
});

for (const width of [1280, 390]) {
  test(`HTML controls retain state through full view at ${width}px`, async ({ page }) => {
    const height = 844;
    const downloadServer = createServer((_request, response) => {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.setHeader("Content-Disposition", 'attachment; filename="executive-brief.html"');
      response.end(fixture);
    });
    await new Promise<void>((resolve) => downloadServer.listen(0, "127.0.0.1", resolve));
    const address = downloadServer.address();
    if (!address || typeof address === "string") throw new Error("Expected a download address");
    try {
      await page.setViewportSize({ width, height });
      await page.request.post(`http://127.0.0.1:${process.env.PLAYWRIGHT_API_PORT ?? "18080"}/api/v1/_test/reset`);
      await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
        dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50, interactive_html: true,
      } }));
      await page.route("**/api/v1/sessions/*/artifacts/art_delivery/content", (route) => route.fulfill({ contentType: "text/html", body: fixture }));
      // Use real HTTP attachment transport: WebKit does not emit a download for
      // the same HTML attachment synthesized with route.fulfill.
      await page.route(/\/api\/v1\/files\/art_delivery$/, (route) => route.continue({
        url: `http://127.0.0.1:${address.port}/original`,
      }));
      await page.goto("/");
      const composer = page.locator("textarea").first();
      await composer.fill("resultsworkspace");
      const sent = page.waitForResponse((response) => response.url().endsWith("/api/v1/chat/send"));
      await composer.press("Enter");
      const { turn_id: turnId, session_id: sessionId } = await (await sent).json();
      const detail = await (await page.request.get(`/api/v1/sessions/${sessionId}`)).json();
      await page.route(`**/api/v1/sessions/${sessionId}`, (route) => route.fulfill({ json: {
        ...detail, turns: [{ id: turnId, seq: 1, user_text: "resultsworkspace", intent: "analysis", status: "completed",
        started_at: "2026-10-27T00:00:00Z", ended_at: "2026-10-27T00:00:01Z", final_answer: "Report ready", trace_steps: [], summary: null }],
      } }));
      await expect(page.locator(".canvas-report-article")).toBeAttached();
      await page.goto("/?session=s_e2e&result=art_delivery&mode=preview");
      const frame = page.frameLocator('iframe[title="executive-brief.html"]').frameLocator('iframe[title="HTML content"]');
      await expect(frame.locator("body")).toHaveAttribute("data-strict", "true");
      await frame.locator("#count").click();
      await frame.locator("#inline").click();
      await expect(frame.locator("#inline")).toHaveText("clicked");
      await frame.locator("#amount").fill("7");
      await frame.getByRole("button", { name: "Calculate" }).click();
      await expect(frame.locator("#total")).toHaveText("14");
      await frame.locator("#tab").click();
      await expect(frame.locator("#detail")).toBeVisible();
      const trigger = page.getByRole("button", { name: "全屏查看", exact: true });
      await trigger.click();
      const dialog = page.getByRole("dialog", { name: "executive-brief.html", exact: true });
      await expect.poll(() => dialog.boundingBox()).toEqual({ x: 0, y: 0, width, height });
      await expect(frame.locator("#count")).toHaveText("1");
      await frame.locator("#count").click();
      await frame.locator("#count").press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(frame.locator("#count")).toHaveText("2");
      await expect(trigger).toBeFocused();
      await page.screenshot({ path: test.info().outputPath(`html-interactive-${width}.jpg`) });
      await frame.locator("#fail").click();
      await expect(page.getByRole("status").filter({ hasText: "页面脚本运行出错" })).toBeVisible();
      await expect(page.locator(".safe-html-preview")).not.toContainText("synthetic-private-detail");
      await page.getByRole("radio", { name: "静态阅读", exact: true }).click();
      await expect(page.locator('iframe[title="executive-brief.html"]')).toHaveAttribute("sandbox", "");
      await page.getByRole("radio", { name: "交互", exact: true }).click();
      await expect(frame.locator("#count")).toHaveText("0");
      const downloaded = page.waitForEvent("download");
      const downloadHref = await page.getByRole("link", { name: "下载", exact: true }).getAttribute("href");
      expect(downloadHref).not.toBeNull();
      await page.goto(new URL(downloadHref!, page.url()).href).catch((error: Error) => {
        if (!/download|aborted|interrupted/i.test(error.message)) throw error;
      });
      const path = await (await downloaded).path();
      expect(path).not.toBeNull();
      expect(await readFile(path!, "utf8")).toBe(fixture);
    } finally {
      downloadServer.closeAllConnections();
      await new Promise<void>((resolve) => downloadServer.close(() => resolve()));
    }
  });
}
