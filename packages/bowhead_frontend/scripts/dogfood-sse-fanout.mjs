// dogfood: SSE fan-out bus — two-tab + mid-turn reconnect UI verification.
// Run: node scripts/dogfood-sse-fanout.mjs
// Assertions: ① turn finishes after mid-turn reload (final answer visible, no stuck running state)
//             ② second tab on same session also finishes completely
//             ③ no duplicate canvas-section titles after completion (terminal check, tech-design §3)
// Screenshots: /tmp/dogfood-{1440,412}-*.png
import { chromium } from "playwright";

const FE = "http://127.0.0.1:3011";
const QUERY = "各产品线销售额排名前5，分析季度变化并输出一份报告";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fail(msg) { console.error("DOGFOOD_FAIL:", msg); process.exit(1); }

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
page.on("pageerror", (e) => console.log("pageerror:", String(e).slice(0, 200)));

// ── Login ──
await page.goto(FE + "/login", { waitUntil: "networkidle" });
await page.screenshot({ path: "/tmp/dogfood-1440-login.png" });
const email = page.locator('input[type="email"], input[name="email"]').first();
await email.fill("u_dev_alice@local.dev");
await page.locator('input[type="password"]').first().fill("dogfood-sse-2026");
await page.locator('button[type="submit"]').first().click();
await page.waitForURL((u) => !u.pathname.includes("login"), { timeout: 15000 })
  .catch(() => fail("login did not redirect"));
console.log("login OK →", page.url());

// ── Send long turn ──
await sleep(2000);
const composer = page.locator("textarea").first();
await composer.waitFor({ timeout: 15000 });
await composer.fill(QUERY);
await composer.press("Enter");
console.log("query sent");

// Wait until turn really starts (steps appear), then mid-turn reload = disconnect/reconnect.
await sleep(20000);
await page.screenshot({ path: "/tmp/dogfood-1440-midturn-before-reload.png" });
const urlMid = page.url();
await page.reload({ waitUntil: "domcontentloaded" });
console.log("mid-turn reload done (断连重连模拟)");

// ── Second tab, same session (concurrent two-tab subscription) ──
const page2 = await ctx.newPage();
await page2.goto(urlMid, { waitUntil: "domcontentloaded" });
console.log("tab2 opened same session");
await sleep(8000);
await page.screenshot({ path: "/tmp/dogfood-1440-tab1-after-reload.png" });
await page2.screenshot({ path: "/tmp/dogfood-1440-tab2-midturn.png" });

// ── Await completion: poll both tabs for done state (composer accepts input/no running spinner) ──
let done = false;
for (let i = 0; i < 36; i++) {
  await sleep(10000);
  const t1 = await page.evaluate(() => document.body.innerText);
  if (/(已完成|分析完成|报告|总结)/.test(t1) && !/思考中|运行中|生成中/.test(t1)) { done = true; break; }
}
console.log("turn finished (tab1 判定):", done);
await sleep(3000);

// ── Terminal assertions ──
const body1 = await page.evaluate(() => document.body.innerText);
const body2 = await page2.evaluate(() => document.body.innerText);
const stuck1 = /运行中/.test(body1);
const stuck2 = /运行中/.test(body2);

// Duplicate canvas-section title detection (tab1).
const dupCheck = await page.evaluate(() => {
  const hs = [...document.querySelectorAll("h1,h2,h3")].map((h) => h.innerText.trim()).filter(Boolean);
  const seen = {}, dups = [];
  for (const h of hs) { seen[h] = (seen[h] || 0) + 1; if (seen[h] === 2) dups.push(h); }
  return { total: hs.length, dups };
});

await page.screenshot({ path: "/tmp/dogfood-1440-tab1-final.png", fullPage: false });
await page2.screenshot({ path: "/tmp/dogfood-1440-tab2-final.png", fullPage: false });
await page.setViewportSize({ width: 412, height: 915 });
await sleep(1500);
await page.screenshot({ path: "/tmp/dogfood-412-tab1-final.png" });

console.log(JSON.stringify({
  turn_done_detected: done,
  tab1_stuck_running: stuck1,
  tab2_stuck_running: stuck2,
  canvas_headings: dupCheck.total,
  canvas_dup_headings: dupCheck.dups,
  tab1_len: body1.length, tab2_len: body2.length,
}, null, 2));

if (!done) fail("turn 未在 6min 内完成或完成态未检出");
if (stuck1 || stuck2) fail("有 tab 卡『运行中』—— mid-stream 事件仍在丢");
if (dupCheck.dups.length) fail("canvas section 标题重复: " + dupCheck.dups.join(","));
console.log("DOGFOOD_PASS");
await browser.close();
