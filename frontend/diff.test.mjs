import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";

let server, browser, origin;
const entries = ["a.txt", "b.txt"].map((path) => ({
  id: path, status: "modified", old_path: path, new_path: path,
  old_mode: "100644", new_mode: "100644",
}));
const source = (label, content) => ({
  label, content, byte_size: content.length, hash: "test-version", bom: false,
  line_endings: { crlf: 0, lf: 1, cr: 0 }, final_newline: true,
});
const file = (entry) => ({
  entry, old: source("HEAD", `old ${entry.new_path}\n`),
  new: source("Working tree", `new ${entry.new_path}\n`),
  reason: null, notices: [], editable: true, working_path: `repo/${entry.new_path}`,
  version: "test-version", old_oid: null, new_oid: null,
});
const route = (path, view = "split") => `#diff?${new URLSearchParams({ path: "repo", comparison: "all", file: path, view })}`;

before(async () => {
  // Serve the shipped assets, not a separate test build. Git responses use the API contract.
  server = createServer(async (request, response) => {
    const path = new URL(request.url, "http://localhost").pathname;
    const asset = { "/": "index.html", "/static/app.js": "app.js", "/static/app.css": "app.css" }[path];
    if (!asset) { response.writeHead(404).end(); return; }
    const type = asset.endsWith(".js") ? "text/javascript" : asset.endsWith(".css") ? "text/css" : "text/html";
    response.writeHead(200, { "Content-Type": type });
    response.end(await readFile(new URL(`../web_edit/static/${asset}`, import.meta.url)));
  });
  await new Promise((resolve) => server.listen(0, "0.0.0.0", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
  });
});
after(async () => {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
});

async function openPage(onFile = async () => {}) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/git/**", async (request) => {
    const url = new URL(request.request().url());
    let data;
    if (url.pathname.endsWith("/context")) data = { available: true, reason: null, repo_path: "repo" };
    else if (url.pathname.endsWith("/changes")) data = { repo_path: "repo", comparison: "all", snapshot: "snapshot", entries };
    else {
      const entry = entries.find((entry) => entry.id === url.searchParams.get("entry"));
      await onFile(entry);
      data = file(entry);
    }
    await request.fulfill({ json: data }).catch(() => {}); // A delayed request can be aborted by navigation.
  });
  await page.goto(origin + route("a.txt"));
  await page.waitForFunction(() => document.querySelector("#diff-title").textContent === "a.txt" && !document.querySelector("#diff-section").inert);
  return { page, errors };
}

test("Back invalidates a pending file load and unlocks the visible comparison", { timeout: 20000 }, async () => {
  let release, requested;
  const gate = new Promise((resolve) => { release = resolve; });
  const pending = new Promise((resolve) => { requested = resolve; });
  const { page, errors } = await openPage(async (entry) => {
    if (entry.id === "b.txt") { requested(); await gate; }
  });
  try {
    await page.locator("#diff-list a").filter({ hasText: "b.txt" }).click();
    await pending;
    assert.equal(await page.locator("#diff-section").evaluate((section) => section.inert), true);
    await page.goBack();
    await page.waitForFunction(() => !document.querySelector("#diff-section").inert);
    release();
    await page.waitForTimeout(200);
    assert.equal(await page.locator("#diff-title").textContent(), "a.txt");
    assert.equal(new URLSearchParams(new URL(page.url()).hash.split("?")[1]).get("file"), "a.txt");
    await page.locator(".cm-merge-b .cm-line").first().click();
    await page.keyboard.type("kept edit");
    assert.equal(await page.locator("#diff-save-state").textContent(), "Unsaved changes");
    assert.deepEqual(errors, []);
  } finally { release(); await page.close(); }
});

test("working-tree edits survive layout changes and cancelled navigation", { timeout: 20000 }, async () => {
  const { page, errors } = await openPage();
  try {
    assert.equal(await page.locator("#diff-editor [contenteditable=true]").count(), 1);
    await page.locator("#diff-editor [contenteditable=true]").fill("unsaved replacement\n");
    await page.locator("#diff-layout").selectOption("unified");
    assert.equal(await page.locator("#diff-save-state").textContent(), "Unsaved changes");
    await page.locator("#diff-list a").filter({ hasText: "b.txt" }).click();
    await page.locator("#confirmation").waitFor({ state: "visible" });
    await page.locator('#confirmation button[value="cancel"]').click();
    assert.equal(await page.locator("#diff-title").textContent(), "a.txt");
    await page.locator("#diff-layout").selectOption("split");
    assert.match(await page.locator(".cm-merge-b").innerText(), /unsaved replacement/);
    await page.locator("#diff-editor [contenteditable=true]").press("Control+z");
    assert.equal(await page.locator("#diff-save-state").textContent(), "All changes saved");
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test("save conflicts retain edited text and send the working-tree version", { timeout: 20000 }, async () => {
  const { page, errors } = await openPage();
  try {
    let body;
    await page.route("**/api/text", async (request) => {
      body = request.request().postDataJSON();
      await request.fulfill({ status: 409, json: { error: "File changed on disk." } });
    });
    await page.locator("#diff-editor [contenteditable=true]").fill("keep these edits\n");
    await page.locator("#diff-save").click();
    await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("Your edits are still here."));
    assert.deepEqual(body, { path: "repo/a.txt", content: "keep these edits\n", version: "test-version" });
    assert.match(await page.locator(".cm-merge-b").innerText(), /keep these edits/);
    assert.equal(await page.locator("#diff-save-state").textContent(), "Unsaved changes");
    assert.equal(await page.locator("#diff-save").isEnabled(), true);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test("layout selection updates all file routes and survives selecting another file", { timeout: 20000 }, async () => {
  const { page, errors } = await openPage();
  try {
    await page.locator("#diff-layout").selectOption("unified");
    for (const href of await page.locator("#diff-list a").evaluateAll((links) => links.map((link) => link.href))) {
      assert.equal(new URLSearchParams(new URL(href).hash.split("?")[1]).get("view"), "unified");
    }
    await page.locator("#diff-list a").filter({ hasText: "b.txt" }).click();
    await page.waitForFunction(() => document.querySelector("#diff-title").textContent === "b.txt" && !document.querySelector("#diff-section").inert);
    assert.equal(await page.locator("#diff-layout").inputValue(), "unified");
    assert.equal(await page.locator(".cm-mergeView").count(), 0);
    assert.equal(await page.locator("#diff-editor > .cm-editor").count(), 1);
    assert.equal(new URLSearchParams(new URL(page.url()).hash.split("?")[1]).get("view"), "unified");
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});
