import { createServer } from "node:http";
import { expect, test, type Page } from "@playwright/test";
import { buildInteractiveHtmlDocument } from "../../lib/interactive-html";
import { INTERACTIVE_HTML_PERMISSIONS, INTERACTIVE_HTML_SANDBOX } from "../../lib/html-preview-contract";

const escape = (value: string) => value.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");

/** Each attack has a real HTTP sink and synthetic credentials on the host. */
async function attackHost(page: Page) {
  let document = "";
  const hits: string[] = [];
  const server = createServer((request, response) => {
    if (request.url?.startsWith("/sink")) hits.push(request.url);
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    response.setHeader("Set-Cookie", [
      "bowhead_jwt=synthetic; HttpOnly; SameSite=Lax; Path=/",
      "readable_preference=synthetic; SameSite=Lax; Path=/",
    ]);
    response.end(document);
  });
  server.on("upgrade", (request, socket) => {
    hits.push(request.url ?? "/socket");
    socket.destroy();
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Expected a loopback HTTP address");
  const origin = `http://127.0.0.1:${address.port}`;
  return {
    origin, hits,
    async render(raw: string, isolated = true) {
      const prepared = isolated ? await buildInteractiveHtmlDocument(raw) : null;
      document = prepared
        ? `<script>localStorage.setItem('host-secret','synthetic')</script><iframe title="outer" sandbox="${INTERACTIVE_HTML_SANDBOX}" allow="${INTERACTIVE_HTML_PERMISSIONS}" srcdoc="${escape(prepared.document)}"></iframe>`
        : raw;
      await page.goto(origin);
    },
    async close() {
      await page.goto("about:blank");
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}

test("fresh realms and alternate storage APIs cannot acquire the host identity", async ({ page }) => {
  const host = await attackHost(page);
  try {
    await host.render(`<output id="result"></output><script>
      const outcomes={};
      const probe=async(key,run)=>{try{outcomes[key]=String(await run())}catch(error){outcomes[key]=error.name}};
      (async()=>{
        await probe('cookie',()=>document.cookie);
        await probe('cookie-write',()=>{document.cookie='readable_preference=overwritten; Path=/';return document.cookie});
        await probe('host-dom',()=>top.document.body);
        await probe('host-fetch',()=>top.fetch('${host.origin}/sink/borrow'));
        await probe('parent-dom',()=>parent.document.body);
        await probe('domain',()=>document.domain='127.0.0.1');
        await probe('local-storage',()=>localStorage.getItem('host-secret'));
        await probe('session-storage',()=>sessionStorage.length);
        await probe('indexed-db',()=>indexedDB.open('host-secret'));
        await probe('cache-storage',()=>caches.open('host-secret'));
        await probe('cookie-store',()=>cookieStore.getAll());
        await probe('service-worker',()=>navigator.serviceWorker.register('${host.origin}/sink/worker.js'));
        for(const kind of ['unset','blank','srcdoc','data','blob']){
          const child=document.createElement('iframe');
          if(kind==='blank')child.src='about:blank';
          if(kind==='srcdoc')child.srcdoc='<p>fresh</p>';
          if(kind==='data')child.src='data:text/html,<p>fresh</p>';
          if(kind==='blob')child.src=URL.createObjectURL(new Blob(['<p>fresh</p>'],{type:'text/html'}));
          document.body.append(child);
          await probe(kind+'-immediate',()=>child.contentWindow.RTCPeerConnection);
          await new Promise(resolve=>setTimeout(resolve,30));
          await probe(kind+'-loaded',()=>child.contentWindow.document.cookie);
        }
        document.querySelector('#result').textContent=JSON.stringify(outcomes);
      })();
    </script>`);
    const frame = page.frameLocator('iframe[title="outer"]').frameLocator('iframe[title="HTML content"]');
    await expect(frame.locator("#result")).not.toBeEmpty();
    const outcomes = JSON.parse(await frame.locator("#result").innerText()) as Record<string, string>;
    expect(Object.keys(outcomes)).toHaveLength(22);
    for (const [key, value] of Object.entries(outcomes)) {
      // Engines without an optional API may report ReferenceError/TypeError.
      expect(["SecurityError", "ReferenceError", "TypeError", ""], key).toContain(value);
    }
    expect(await page.evaluate(() => document.cookie)).toContain("readable_preference=synthetic");
    expect(host.hits).toEqual([]);
    // Positive controls verify that the credentials exist and HttpOnly alone
    // cannot prevent an authenticated host request.
    expect(await page.evaluate(() => localStorage.getItem("host-secret"))).toBe("synthetic");
    await page.evaluate(() => fetch("/sink/positive", { credentials: "include" }));
    expect(host.hits).toEqual(["/sink/positive"]);
  } finally { await host.close(); }
});

test("independent HTTP, socket, worker, CSS and SVG sinks cannot send requests", async ({ page }) => {
  const host = await attackHost(page);
  try {
    await host.render(`<output id="attempted"></output><script>
      const base='${host.origin}/sink/';const attempted=[];
      const probe=(name,run)=>{attempted.push(name);try{const result=run();if(result&&result.catch)result.catch(()=>{})}catch(error){}};
      probe('fetch',()=>fetch(base+'fetch',{method:'POST',credentials:'include',mode:'no-cors',body:'synthetic'}));
      probe('xhr',()=>{const xhr=new XMLHttpRequest();xhr.open('GET',base+'xhr');xhr.withCredentials=true;xhr.send()});
      probe('beacon',()=>navigator.sendBeacon(base+'beacon','synthetic'));
      probe('event-source',()=>new EventSource(base+'events',{withCredentials:true}));
      probe('websocket',()=>new WebSocket(base.replace('http:','ws:')+'socket'));
      probe('worker',()=>new Worker(base+'worker.js'));
      probe('shared-worker',()=>new SharedWorker(base+'shared.js'));
      probe('image',()=>{new Image().src=base+'image'});
      probe('css',()=>{const style=document.createElement('style');style.textContent='@import url("'+base+'css");body{background:url("'+base+'background")}';document.head.append(style)});
      probe('svg',()=>{const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');const image=document.createElementNS(svg.namespaceURI,'image');image.setAttribute('href',base+'svg');svg.append(image);document.body.append(svg)});
      probe('object',()=>{const object=document.createElement('object');object.data=base+'object';document.body.append(object)});
      probe('nested-frame',()=>{const child=document.createElement('iframe');child.src=base+'frame';document.body.append(child)});
      probe('preload',()=>{const link=document.createElement('link');link.rel='preload';link.as='script';link.href=base+'preload';document.head.append(link)});
      probe('dynamic-import',()=>import(base+'module.js'));
      document.querySelector('#attempted').textContent=JSON.stringify(attempted);
    </script>`);
    const frame = page.frameLocator('iframe[title="outer"]').frameLocator('iframe[title="HTML content"]');
    await expect(frame.locator("#attempted")).not.toBeEmpty();
    expect(JSON.parse(await frame.locator("#attempted").innerText())).toHaveLength(14);
    await page.waitForTimeout(300);
    expect(host.hits).toEqual([]);
    await host.render(`<script>fetch('/sink/positive',{credentials:'include'})</script>`, false);
    await expect.poll(() => host.hits).toEqual(["/sink/positive"]);
  } finally { await host.close(); }
});

const navigations = {
  assign: "location.assign(url)",
  replace: "location.replace(url)",
  "meta-refresh": "const meta=document.createElement('meta');meta.httpEquiv='refresh';meta.content='0;url='+url;document.head.append(meta)",
  "anchor-self": "const a=document.createElement('a');a.href=url;a.click()",
  "anchor-parent": "const a=document.createElement('a');a.href=url;a.target='_parent';a.click()",
  "anchor-top": "const a=document.createElement('a');a.href=url;a.target='_top';a.click()",
  popup: "window.open(url,'_blank')",
  download: "const a=document.createElement('a');a.href=url;a.download='original.html';a.click()",
  "post-form": "const form=document.createElement('form');form.action=url;form.method='post';document.body.append(form);form.submit()",
  "get-form": "const form=document.createElement('form');form.action=url;document.body.append(form);form.submit()",
};

for (const [name, action] of Object.entries(navigations)) {
  test(`user activation cannot escape through ${name}`, async ({ page }) => {
    const host = await attackHost(page);
    let popups = 0;
    let downloads = 0;
    page.on("popup", () => { popups++; });
    page.on("download", () => { downloads++; });
    try {
      await host.render(`<button id="attack">Attack</button><script>
        document.querySelector('#attack').onclick=()=>{const url='${host.origin}/sink/${name}';${action}};
      </script>`);
      const frame = page.frameLocator('iframe[title="outer"]').frameLocator('iframe[title="HTML content"]');
      await frame.locator("#attack").click();
      await page.waitForTimeout(200);
      expect(host.hits).toEqual([]);
      expect(popups).toBe(0);
      expect(downloads).toBe(0);
      expect(page.url()).toBe(`${host.origin}/`);
    } finally { await host.close(); }
  });
}

test("document rewriting cannot discard the inherited network policy", async ({ page }) => {
  const host = await attackHost(page);
  try {
    await host.render(`<button id="rewrite">Rewrite</button><script>
      document.querySelector('#rewrite').onclick=()=>{
        document.open();document.write('<p id="rewritten">Rewritten</p><img src="${host.origin}/sink/write">');document.close();
        fetch('${host.origin}/sink/after-write',{credentials:'include'}).catch(()=>{});
      };
    </script>`);
    const frame = page.frameLocator('iframe[title="outer"]').frameLocator('iframe[title="HTML content"]');
    await frame.locator("#rewrite").click();
    await expect(frame.locator("#rewritten")).toHaveText("Rewritten");
    await page.waitForTimeout(200);
    expect(host.hits).toEqual([]);
  } finally { await host.close(); }
});
