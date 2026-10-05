// 로컬 평가 HTML은 서버 없이 열려야 하며 좁은 화면에서도 표와 ID가 넘치지 않아야 한다.
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({headless: true, channel: "msedge"});
try {
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  const failures = [];
  page.on("pageerror", error => failures.push(error.message));
  const root = process.env.LAYOUT_EVALUATION_OUTPUT || "data/layout-evaluation-20261005-final";
  await page.goto(pathToFileURL(resolve(root, "evaluation.html")).href);
  assert.match(await page.locator("body").innerText(), /분류 정답 작성률/);
  assert.match(await page.locator("body").innerText(), /14 \/ 29 Block/);
  await page.screenshot({path: resolve(root, "desktop.png")});
  await page.setViewportSize({width: 390, height: 844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({path: resolve(root, "mobile.png")});
  assert.deepEqual(failures, []);
  console.log("PASS: offline evaluation HTML, coverage, desktop/mobile layout");
} finally { await browser.close(); }
