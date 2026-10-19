import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";

let server, browser, origin;
const entries = ["a.txt", "b.txt", "c.txt"].map((path) => ({
  id: `id-${path}`, status: "modified", old_path: path, new_path: path,
  old_mode: "100644", new_mode: "100644",
}));
const source = (label, content) => ({ label, content, final_newline: true });
const route = (comparison, path = "a.txt") => `#diff?${new URLSearchParams({ path: "repo", comparison, file: path, view: "split" })}`;

before(async () => {
  server = createServer(async (request, response) => {
    const asset = { "/": "index.html", "/static/app.js": "app.js", "/static/app.css": "app.css" }[new URL(request.url, "http://localhost").pathname];
    if (!asset) { response.writeHead(404).end(); return; }
    response.writeHead(200, { "Content-Type": asset.endsWith(".js") ? "text/javascript" : asset.endsWith(".css") ? "text/css" : "text/html" });
    response.end(await readFile(new URL(`../web_edit/static/${asset}`, import.meta.url)));
  });
  await new Promise((resolve) => server.listen(0, "0.0.0.0", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}) });
});
after(async () => {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
});

async function open({ comparison = "unstaged", selected = "a.txt", fail = false, pending = null } = {}) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  page.setDefaultTimeout(5000);
  const requests = [], errors = [];
  let remaining = entries.map((entry) => ({ ...entry, action: comparison === "staged" ? "unstage" : "stage" }));
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/git/**", async (intercept) => {
    const request = intercept.request(), url = new URL(request.url());
    let data;
    if (url.pathname.endsWith("/context")) data = { available: true, repo_path: "repo" };
    else if (url.pathname.endsWith("/changes")) data = { repo_path: "repo", comparison, snapshot: `snapshot-${requests.length}`, entries: remaining };
    else if (url.pathname.endsWith("/action")) {
      requests.push({ method: request.method(), headers: request.headers(), body: request.postDataJSON() });
      if (pending) await pending;
      if (fail) { await intercept.fulfill({ status: 409, json: { error: "Repository changed. Refresh and retry." } }); return; }
      if (comparison === "all") remaining = remaining.map((entry) => entry.id === request.postDataJSON().entry ? { ...entry, action: "unstage" } : entry);
      else remaining = remaining.filter((entry) => entry.id !== request.postDataJSON().entry);
      data = { ok: true };
    } else {
      const entry = remaining.find((entry) => entry.id === url.searchParams.get("entry"));
      data = { entry, old: source("HEAD", `old ${entry.new_path}\n`), new: source("Working tree", `new ${entry.new_path}\n`), editable: true, working_path: `repo/${entry.new_path}`, version: "version", notices: [] };
    }
    await intercept.fulfill({ json: data });
  });
  await page.goto(origin + route(comparison, selected));
  await page.waitForFunction((selected) => document.querySelector("#diff-title").textContent === selected && !document.querySelector("#diff-section").inert, selected);
  return { page, requests, errors };
}
async function settled(page, notice) {
  await page.waitForFunction((notice) => document.querySelector("#notice").textContent === notice && !document.querySelector("#diff-section").inert, notice);
}

for (const [comparison, action, label] of [["unstaged", "stage", "Stage"], ["staged", "unstage", "Unstage"]]) {
  test(`${label} posts the versioned entry and selects the next file after removal`, async () => {
    const { page, requests, errors } = await open({ comparison });
    try {
      assert.equal(await page.locator("#diff-list a button").count(), 0);
      assert.equal(await page.locator(".diff-file-row.selected").count(), 1);
      assert.equal(await page.locator(".diff-file-row.selected").evaluate(row => getComputedStyle(row).backgroundColor), "rgb(226, 236, 230)");
      assert.equal(await page.locator("a[aria-current]").evaluate(link => getComputedStyle(link).backgroundColor), "rgba(0, 0, 0, 0)");
      const style = await page.locator(".diff-action").first().evaluate(button => {
        const action = getComputedStyle(button);
        const status = getComputedStyle(button.closest(".diff-file-row").querySelector("small"));
        return { background: action.backgroundColor, border: action.borderTopWidth, weight: action.fontWeight, size: action.fontSize, statusSize: status.fontSize };
      });
      assert.equal(style.background, "rgba(0, 0, 0, 0)");
      assert.equal(style.border, "0px");
      assert.equal(style.weight, "400");
      assert.equal(style.size, style.statusSize);
      await page.getByRole("button", { name: `${label} a.txt`, exact: true }).click();
      await settled(page, action === "stage" ? "File staged." : "File unstaged.");
      assert.equal(requests.length, 1);
      assert.equal(requests[0].method, "POST");
      assert.equal(requests[0].headers["x-web-edit"], "1");
      assert.equal(requests[0].headers["content-type"], "application/json");
      assert.deepEqual(requests[0].body, { path: "repo", comparison, entry: "id-a.txt", snapshot: "snapshot-0", action });
      assert.equal(await page.locator("#diff-list a").count(), 2);
      assert.equal(await page.locator("#diff-title").textContent(), "b.txt");
      assert.equal(await page.locator("#diff-list a[aria-current]").getAttribute("data-path"), "b.txt");
      assert.equal(await page.locator(".diff-file-row.selected a").getAttribute("data-path"), "b.txt");
      assert.equal(await page.locator("#diff-list a[aria-current]").evaluate((link) => document.activeElement === link), true);
      assert.deepEqual(errors, []);
    } finally { await page.close(); }
  });
}

test("last entry removal selects the previous file", async () => {
  const { page } = await open({ selected: "c.txt" });
  try {
    await page.getByRole("button", { name: "Stage c.txt", exact: true }).click();
    await settled(page, "File staged.");
    assert.equal(await page.locator("#diff-title").textContent(), "b.txt");
  } finally { await page.close(); }
});

test("staging all remaining entries leaves an empty comparison", async () => {
  const { page, requests } = await open();
  try {
    for (const path of ["a.txt", "b.txt", "c.txt"]) {
      await page.getByRole("button", { name: `Stage ${path}`, exact: true }).click();
      await settled(page, "File staged.");
    }
    assert.equal(requests.length, 3);
    assert.equal(await page.locator("#diff-list a").count(), 0);
    assert.equal(await page.locator("#diff-title").textContent(), "");
    assert.equal(await page.locator("#diff-editor").textContent(), "No changes");
    assert.equal(await page.locator("#diff-files").evaluate((button) => document.activeElement === button), true);
  } finally { await page.close(); }
});

test("All retains selection and switches Stage to Unstage", async () => {
  const { page, requests } = await open({ comparison: "all" });
  try {
    await page.getByRole("button", { name: "Stage a.txt", exact: true }).click();
    await settled(page, "File staged.");
    assert.equal(await page.locator("#diff-title").textContent(), "a.txt");
    assert.equal(await page.locator("#diff-list a").count(), 3);
    await page.getByRole("button", { name: "Unstage a.txt", exact: true }).click();
    await settled(page, "File unstaged.");
    assert.equal(requests[1].body.action, "unstage");
    assert.equal(requests[1].body.snapshot, "snapshot-1");
  } finally { await page.close(); }
});

test("unsaved edits disable every action without sending requests", async () => {
  const { page, requests } = await open();
  try {
    const content = page.locator("#diff-editor .cm-merge-b .cm-content");
    await content.fill("unsaved content\n");
    assert.match(await page.locator("#diff-save-state").textContent(), /Save or discard edits before staging\./);
    assert.equal(await page.locator("#diff-list button:disabled").count(), 3);
    await page.locator("#diff-list button").evaluateAll((buttons) => buttons.forEach((button) => button.click()));
    assert.equal(requests.length, 0);
    assert.match(await content.textContent(), /unsaved content/);
  } finally { await page.close(); }
});

test("failed actions retain the document, selection, and actionable list", async () => {
  const { page, requests, errors } = await open({ fail: true });
  try {
    const before = await page.locator("#diff-editor").textContent();
    await page.getByRole("button", { name: "Stage a.txt", exact: true }).click();
    await settled(page, "Repository changed. Refresh and retry.");
    assert.equal(await page.locator("#notice").getAttribute("class"), "error");
    assert.equal(await page.locator("#diff-editor").textContent(), before);
    assert.equal(await page.locator("#diff-title").textContent(), "a.txt");
    assert.equal(await page.locator("#diff-list a").count(), 3);
    assert.equal(await page.getByRole("button", { name: "Stage a.txt", exact: true }).isEnabled(), true);
    assert.equal(requests.length, 1);
    assert.equal(await page.evaluate(() => document.activeElement.matches("#diff-list a, #diff-files")), false);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});

test("an in-flight action blocks navigation, editing, and duplicate actions", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const { page, requests } = await open({ pending });
  try {
    await page.getByRole("button", { name: "Stage a.txt", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#diff-section").inert);
    assert.equal(await page.locator("#diff-save-state").textContent(), "Staging…");
    assert.equal(await page.locator("#notice").textContent(), "Staging…");
    await page.locator("#diff-list button").evaluateAll((buttons) => buttons.forEach((button) => button.click()));
    await page.evaluate((hash) => { location.hash = hash; }, route("unstaged", "c.txt"));
    await page.waitForFunction(() => location.hash.includes("file=a.txt"));
    assert.equal(await page.locator("#diff-title").textContent(), "a.txt");
    assert.equal(requests.length, 1);
    release();
    await settled(page, "File staged.");
  } finally { release(); await page.close(); }
});
