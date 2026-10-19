import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";

let server, browser, origin;
const entry = { id: "wide", status: "modified", old_path: "wide.txt", new_path: "wide.txt", old_mode: "100644", new_mode: "100644" };
const longLine = "long line " + "abcdefghij".repeat(120);
const lines = (prefix) => Array.from({ length: 180 }, (_, i) => i === 90 ? "alignment marker" : i > 90 && i < 100 ? `unchanged context ${i}` : i === 0 ? longLine : `${prefix} line ${i}`);
const oldLines = lines("old"), newLines = lines("new");
newLines.splice(60, 0, ...Array.from({ length: 12 }, (_, i) => `inserted line ${i}`));
const source = (label, lines) => ({ label, content: lines.join("\n") + "\n", final_newline: true });

before(async () => {
  server = createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const asset = { "/": "index.html", "/static/app.js": "app.js", "/static/app.css": "app.css" }[url.pathname];
    if (asset) {
      response.writeHead(200, { "Content-Type": asset.endsWith(".js") ? "text/javascript" : asset.endsWith(".css") ? "text/css" : "text/html" });
      response.end(await readFile(new URL(`../web_edit/static/${asset}`, import.meta.url)));
      return;
    }
    let data;
    if (url.pathname.endsWith("/context")) data = { available: true, repo_path: "repo" };
    else if (url.pathname.endsWith("/changes")) data = { repo_path: "repo", snapshot: "snapshot", entries: [entry, ...Array.from({ length: 24 }, (_, i) => ({ ...entry, id: `file-${i}`, new_path: `folder/file-${i}.txt` }))] };
    else if (url.pathname.endsWith("/file")) data = { entry, old: source("HEAD", oldLines), new: source("Working tree", newLines), editable: true, working_path: "repo/wide.txt", version: "version", notices: [] };
    else { response.writeHead(404).end(); return; }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify(data));
  });
  await new Promise(resolve => server.listen(0, "0.0.0.0", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}) });
});
after(async () => {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
});
async function open(width = 1920, view = "split") {
  const page = await browser.newPage({ viewport: { width, height: 1080 } });
  await page.goto(`${origin}/#diff?path=repo&comparison=all&file=wide.txt&view=${view}`);
  await page.waitForFunction(() => document.querySelector("#diff-title").textContent === "wide.txt" && !document.querySelector("#diff-section").inert);
  return page;
}

test("split comparison fills desktop width while controls and file list stay centered", async () => {
  const page = await open();
  try {
    const comparison = await page.locator(".diff-comparison-area").boundingBox();
    const controls = await page.locator(".diff-controls").boundingBox();
    const list = await page.locator("#diff-list").boundingBox();
    assert.equal(comparison.x, 24);
    assert.equal(comparison.width, 1920 - 48);
    assert(controls.width <= 1128);
    assert(list.width <= 1128);
    assert(Math.abs(list.x + list.width / 2 - 960) < 1);
    assert(list.height <= 162);
    assert(list.y + list.height <= comparison.y);
    assert(await page.locator("#diff-list").evaluate(el => el.scrollHeight > el.clientHeight));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.setViewportSize({ width: 1280, height: 900 });
    const smaller = await page.locator(".diff-comparison-area").boundingBox();
    assert.equal(smaller.x, 24);
    assert.equal(smaller.width, 1280 - 48);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.locator("#diff-layout").selectOption("unified");
    const unified = await page.locator(".diff-comparison-area").boundingBox();
    assert(unified.width <= 1128);
    assert(Math.abs(unified.x + unified.width / 2 - 960) < 1);
  } finally { await page.close(); }
});

test("split panes expand vertically, scroll horizontally, and keep line alignment", async () => {
  const page = await open();
  try {
    const a = page.locator(".cm-merge-a .cm-scroller");
    const b = page.locator(".cm-merge-b .cm-scroller");
    for (const pane of [a, b]) {
      assert(await pane.evaluate(el => el.scrollWidth > el.clientWidth));
      assert(await pane.evaluate(el => ["auto", "scroll"].includes(getComputedStyle(el).overflowX)));
    }
    await b.evaluate(el => { el.scrollLeft = 500; });
    assert(await b.evaluate(el => el.scrollLeft > 0));
    assert.equal(await a.evaluate(el => el.scrollLeft), 0);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await b.evaluate(el => { el.scrollLeft = 0; });
    await page.locator(".cm-merge-b .cm-line").first().click();
    await page.keyboard.press("Home");
    await page.keyboard.press("End");
    await page.waitForFunction(() => document.querySelector(".cm-merge-b .cm-scroller").scrollLeft > 0);
    await page.locator("#diff-title").click();
    await page.waitForTimeout(100);
    for (const pane of [a, b]) {
      assert(await pane.evaluate(el => el.clientHeight > innerHeight));
      assert(await pane.evaluate(el => el.scrollHeight <= el.clientHeight + 1));
    }
    const before = await page.evaluate(() => scrollY);
    await b.hover();
    await page.mouse.wheel(0, 500);
    await page.waitForFunction(top => scrollY > top, before);
    assert.equal(await a.evaluate(el => el.scrollTop), 0);
    assert.equal(await b.evaluate(el => el.scrollTop), 0);
    await page.waitForTimeout(300);
    await page.locator("#diff-editor").evaluate(el => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY + 1800));
    const markers = page.locator("#diff-editor .cm-line").filter({ hasText: "alignment marker" });
    await page.waitForFunction(() => [...document.querySelectorAll("#diff-editor .cm-line")].filter(el => el.textContent === "alignment marker").length === 2);
    const bounds = await Promise.all([markers.nth(0).boundingBox(), markers.nth(1).boundingBox()]);
    assert(Math.abs(bounds[0].y - bounds[1].y) < 2, `unchanged lines differ: ${bounds.map(b => b.y)}`);
  } finally { await page.close(); }
});

test("line wrapping applies to both layouts and preserves edits and undo history", async () => {
  const page = await open();
  try {
    await page.locator(".cm-merge-b .cm-line").first().click();
    await page.keyboard.press("End");
    await page.keyboard.insertText(" added text");
    await page.locator("#diff-wrap").check();
    await page.waitForFunction(() => [...document.querySelectorAll("#diff-editor .cm-scroller")].every(el => el.scrollWidth <= el.clientWidth + 1));
    assert.equal(await page.locator("#diff-editor .cm-content.cm-lineWrapping").count(), 2);
    assert.equal(await page.locator("#diff-save-state").textContent(), "Unsaved changes");
    await page.locator("#diff-wrap").uncheck();
    assert(await page.locator(".cm-merge-b .cm-scroller").evaluate(el => el.scrollWidth > el.clientWidth));
    await page.locator("#diff-wrap").check();
    await page.locator("#diff-layout").selectOption("unified");
    assert.equal(await page.locator("#diff-editor .cm-content.cm-lineWrapping").count(), 1);
    await page.locator("#diff-editor [contenteditable=true]").press("Control+z");
    assert.equal(await page.locator("#diff-save-state").textContent(), "All changes saved");
    await page.reload();
    await page.waitForFunction(() => !document.querySelector("#diff-section").inert && document.querySelector("#diff-editor .cm-editor"));
    assert(await page.locator("#diff-wrap").isChecked());
    assert.equal(await page.locator("#diff-editor .cm-content.cm-lineWrapping").count(), 1);
  } finally { await page.close(); }
});

test("mobile comparison retains page gutters without horizontal page overflow", async () => {
  const page = await open(390, "unified");
  try {
    const box = await page.locator(".diff-comparison-area").boundingBox();
    assert.equal(box.x, 14);
    assert.equal(box.width, 390 - 28);
    const unifiedScroller = page.locator("#diff-editor > .cm-editor > .cm-scroller");
    assert(await unifiedScroller.evaluate(el => el.clientHeight > innerHeight));
    assert(await unifiedScroller.evaluate(el => el.scrollHeight <= el.clientHeight + 1));
    assert.equal((await page.locator("#diff-editor").boundingBox()).x, 14);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator("#diff-layout").selectOption("split");
    assert.equal((await page.locator("#diff-editor").boundingBox()).x, 14);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  } finally { await page.close(); }
});
