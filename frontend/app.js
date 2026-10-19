import { basicSetup } from "codemirror";
import { EditorView, keymap } from "@codemirror/view";
import { Compartment, EditorState } from "@codemirror/state";
import { historyField } from "@codemirror/commands";
import { MergeView, unifiedMergeView, goToNextChunk, goToPreviousChunk } from "@codemirror/merge";
import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";

const $ = (id) => document.getElementById(id);
const loadingTemplate = document.querySelector(".loading-state").cloneNode(true);
const normalise = (text) => text.replace(/\r\n?/g, "\n");
const parentPath = (path) => path.split("/").slice(0, -1).join("/");
const joinPath = (folder, name) => folder ? `${folder.replace(/\/$/, "")}/${name}` : name;
const routeURL = (mode, path = "") => `#${mode}?path=${encodeURIComponent(path)}`;
const apiURL = (endpoint, path) => `./api/${endpoint}?path=${encodeURIComponent(path)}`;
let currentFolder = "", rootName = "Files", editor = null, editingPath = "", fileVersion = null;
let savedText = "", isNew = false, saving = false, currentHash = "", navigation = 0;
const language = new Compartment();
const diffWrapping = new Compartment();
let wrapDiff = false;
try { wrapDiff = localStorage.getItem("web-edit-diff-wrap") === "true"; } catch { /* Use the session preference. */ }
$("diff-wrap").checked = wrapDiff;
let diff = null, diffEditor = null, merge = null, diffAbort = null, contextAbort = null;
let gitAction = null;
let preferredLayout = null;
try { preferredLayout = localStorage.getItem("web-edit-diff-layout"); } catch { /* Session preference remains available. */ }
if (!["split", "unified"].includes(preferredLayout)) preferredLayout = null;
const defaultLayout = () => preferredLayout || (window.innerWidth >= 900 ? "split" : "unified");
const diffConfig = { scanLimit: 500, timeout: 1000 };
// Images reach the clipboard as PNG, which needs the async clipboard API (HTTPS or localhost).
const canCopyImages = Boolean(navigator.clipboard?.write && window.ClipboardItem);
// The largest canvas iOS Safari allows; keeps PNG conversion consistent across browsers.
const COPY_PIXEL_LIMIT = 4096 * 4096;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function loadingState(message) {
  const state = loadingTemplate.cloneNode(true);
  state.querySelector(".loading-label").textContent = message;
  return state;
}
function notify(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
  $("notice").hidden = !message;
}
function confirmAction(title, message, action) {
  const dialog = $("confirmation");
  if (dialog.open) return Promise.resolve(false);
  $("confirmation-title").textContent = title;
  $("confirmation-message").textContent = message;
  $("confirm-action").textContent = action;
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "accept"), { once: true });
    dialog.showModal();
  });
}
function size(bytes) {
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1e6) return `${(bytes / 1000).toFixed(1)} KB`;
  if (bytes < 1e9) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${(bytes / 1e9).toFixed(1)} GB`;
}
async function api(endpoint, options = {}) {
  const response = await fetch(endpoint, options);
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error || `Request failed (${response.status}).`), { status: response.status, data });
  return data;
}
async function copyText(text) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  // Plain HTTP, such as a Tailscale IP address, has no async clipboard API.
  const focused = document.activeElement;
  const area = element("textarea");
  area.value = text; area.readOnly = true;
  area.style.cssText = "position: fixed; top: 0; left: -9999px; font-size: 16px;";
  document.body.append(area);
  area.select(); area.setSelectionRange(0, text.length);
  const copied = document.execCommand("copy");
  area.remove(); focused?.focus({ preventScroll: true });
  if (!copied) throw new Error("Copy failed.");
}
function imagePNG(image) {
  return new Promise((resolve, reject) => {
    const canvas = element("canvas");
    canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    canvas.getContext("2d").drawImage(image, 0, 0);
    canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("Conversion failed.")), "image/png");
  });
}
async function copyImage() {
  const image = $("viewer-content").querySelector("img");
  const path = $("viewer-title").textContent;
  const pixels = image.naturalWidth * image.naturalHeight;
  if (!pixels) { notify("This image has no fixed size, so it cannot be copied.", true); return; }
  if (pixels > COPY_PIXEL_LIMIT) { notify("Images over 16 megapixels are too large to copy. You can download it instead.", true); return; }
  // Safari only allows clipboard writes started directly by the click, so pass promises.
  const types = { "image/png": imagePNG(image) };
  if (/\.svg$/i.test(path)) {
    types["text/plain"] = fetch(apiURL("image", path)).then(async (response) => {
      if (!response.ok) throw new Error("Request failed.");
      return new Blob([await response.text()], { type: "text/plain" });
    });
  }
  try {
    await navigator.clipboard.write([new ClipboardItem(types)]);
    notify(`Copied ${path} to the clipboard.`);
  } catch { notify("Could not copy this image to the clipboard.", true); }
}
function dirty() { return (editor && editor.state.doc.toString() !== savedText) || diffDirty(); }
function updateSaveState() {
  $("save-state").textContent = saving ? "Saving…" : dirty() ? "Unsaved changes" : isNew ? "New file · not saved yet" : "All changes saved";
  $("save").disabled = saving || (!isNew && !dirty());
}
function languageFor(path) {
  const ext = path.split(".").pop().toLowerCase();
  if (["js", "jsx", "mjs", "cjs", "ts", "tsx"].includes(ext)) return ["JavaScript / TypeScript", javascript({ jsx: ext.endsWith("x"), typescript: ext.startsWith("ts") })];
  if (ext === "py") return ["Python", python()];
  if (["html", "htm"].includes(ext)) return ["HTML", html()];
  if (ext === "css") return ["CSS", css()];
  if (ext === "json") return ["JSON", json()];
  if (["md", "markdown"].includes(ext)) return ["Markdown", markdown()];
  return ["Plain text", []];
}
function showSection(id) {
  for (const section of ["browser", "editor-section", "viewer", "diff-section"]) $(section).hidden = section !== id;
}
function destroyEditor() {
  if (editor) editor.destroy();
  editor = null;
  $("editor").replaceChildren();
}
function breadcrumbs(path) {
  const nav = $("breadcrumbs");
  nav.replaceChildren();
  const root = element("a", "", rootName);
  root.href = routeURL("browse");
  nav.append(root);
  let accumulated = "";
  for (const part of path.split("/").filter(Boolean)) {
    accumulated = joinPath(accumulated, part);
    nav.append(element("span", "", "/"));
    const link = element("a", "", part);
    link.href = routeURL("browse", accumulated);
    nav.append(link);
  }
}
function addDeleteControl(file, row, actions) {
  const toggle = element("button", "delete-toggle", "Delete");
  toggle.setAttribute("aria-label", `Delete ${file.name}`);
  toggle.setAttribute("aria-expanded", "false");
  actions.append(toggle);
  toggle.addEventListener("click", () => {
    toggle.hidden = true;
    toggle.setAttribute("aria-expanded", "true");
    const panel = element("div", "delete-confirmation");
    panel.setAttribute("role", "group");
    panel.setAttribute("aria-label", `Confirm deletion of ${file.name}`);
    const folder = file.kind === "directory";
    const message = element("p", "", folder
      ? `Permanently delete “${file.name}” and everything inside it? This cannot be undone.`
      : `Permanently delete “${file.name}”? This cannot be undone.`);
    const controls = element("div", "delete-confirmation-actions");
    const cancel = element("button", "secondary", "Cancel");
    const confirm = element("button", "danger", folder ? "Delete folder" : "Delete file");
    controls.append(cancel, confirm);
    panel.append(message, controls);
    row.append(panel);
    // Put focus on the safe action, away from the original tap target.
    cancel.focus({ preventScroll: true });
    const close = () => {
      if (cancel.disabled) return;
      panel.remove(); toggle.hidden = false;
      toggle.setAttribute("aria-expanded", "false"); toggle.focus({ preventScroll: true });
    };
    cancel.addEventListener("click", close);
    panel.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); close(); } });
    confirm.addEventListener("click", async () => {
      cancel.disabled = true; confirm.disabled = true; confirm.textContent = "Deleting…";
      try {
        const query = new URLSearchParams({ path: file.path, kind: file.kind, version: file.delete_version });
        await api(`./api/file?${query}`, { method: "DELETE", headers: { "X-Web-Edit": "1" } });
        if (row.isConnected) {
          row.remove();
          if (!$("file-list").children.length) $("file-list").append(element("div", "empty", "This folder is empty. Create a file or upload something to get started."));
          notify(`Deleted ${file.path}.`);
          $("refresh").focus({ preventScroll: true });
        }
      } catch (error) {
        message.textContent = error.message;
        message.setAttribute("role", "alert");
        cancel.disabled = false;
        // A changed item must be refreshed and confirmed again.
        confirm.disabled = error.status === 409 || error.status === 404;
        confirm.textContent = "Retry delete";
      }
    });
  });
}
function renderFiles(data) {
  currentFolder = data.path;
  rootName = data.root_name;
  $("root-label").textContent = rootName;
  breadcrumbs(data.path);
  const list = $("file-list");
  list.replaceChildren();
  if (!data.entries.length) list.append(element("div", "empty", "This folder is empty. Create a file or upload something to get started."));
  discoverGit(data.path, navigation);
  for (const file of data.entries) {
    const row = element("div", "file-row");
    const supported = ["file", "directory"].includes(file.kind);
    const link = element(supported ? "a" : "span", "file-name");
    if (supported) link.href = routeURL(file.kind === "directory" ? "browse" : file.preview ? "view" : "edit", file.path);
    if (file.preview) {
      const thumbnail = element("img", "thumbnail");
      thumbnail.src = apiURL("image", file.path) + `&v=${file.modified}`;
      thumbnail.alt = "";
      thumbnail.loading = "lazy";
      link.append(thumbnail);
    } else {
      const icon = element("span", `file-icon ${file.kind === "directory" ? "folder-icon" : ""}`, file.kind === "directory" ? "▱" : supported ? "≡" : "↗");
      icon.setAttribute("aria-hidden", "true");
      link.append(icon);
    }
    link.append(element("span", "", file.name + (supported ? "" : " (not followed)")));
    row.append(link, element("span", "file-size", file.kind === "directory" ? "—" : size(file.size)), element("span", "file-date", new Date(file.modified * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })));
    const actions = element("div", "file-actions");
    if (file.kind === "file") {
      const download = element("a", "file-download", "Download ↓");
      download.href = apiURL("download", file.path); download.download = file.name;
      download.setAttribute("aria-label", `Download ${file.name}`);
      actions.append(download);
    }
    if (supported) addDeleteControl(file, row, actions);
    row.append(actions);
    list.append(row);
  }
}
function openEditor(path, data, newFile) {
  destroyEditor();
  showSection("editor-section");
  editingPath = path; fileVersion = data.version ?? null; isNew = newFile;
  savedText = normalise(data.content);
  $("new-path-label").hidden = !isNew;
  $("new-path").value = path;
  $("editor-title").textContent = isNew ? "New file" : path;
  $("editor-download").hidden = isNew;
  $("editor-download").href = apiURL("download", path);
  const [name, support] = languageFor(path);
  $("language-label").textContent = name;
  editor = new EditorView({
    doc: savedText,
    parent: $("editor"),
    extensions: [
      keymap.of([{ key: "Mod-s", run: () => { save(); return true; } }]),
      basicSetup, language.of(support), EditorView.lineWrapping,
      EditorView.contentAttributes.of({ "aria-label": "File contents", spellcheck: "false", autocapitalize: "off", autocorrect: "off" }),
      EditorView.updateListener.of((update) => { if (update.docChanged) updateSaveState(); }),
    ],
  });
  updateSaveState();
  if (isNew) $("new-path").focus();
}
function openViewer(path, message, preview) {
  destroyEditor();
  showSection("viewer");
  $("viewer-title").textContent = path;
  $("viewer-download").href = apiURL("download", path);
  $("viewer-content").replaceChildren();
  $("viewer-copy").hidden = !preview || !canCopyImages;
  $("viewer-copy").disabled = true;
  if (preview) {
    const loading = loadingState("Loading image…");
    const image = element("img");
    image.alt = path; image.hidden = true;
    image.addEventListener("load", () => {
      if (!image.isConnected) return;
      loading.remove(); image.hidden = false; $("viewer-copy").disabled = false;
    });
    image.addEventListener("error", () => {
      if (!image.isConnected) return;
      loading.replaceWith(element("p", "", "This image cannot be previewed. You can download it instead."));
      image.remove(); $("viewer-copy").hidden = true;
    });
    $("viewer-content").append(loading, image);
    image.src = apiURL("image", path);
    if (/\.svg$/i.test(path)) {
      const edit = element("a", "button secondary", "Edit SVG source");
      edit.href = routeURL("edit", path);
      $("viewer-content").append(edit);
    }
  } else $("viewer-content").append(element("p", "", message));
}
async function route() {
  const target = new URLSearchParams(location.hash.split("?")[1]);
  if (!saving && diff && location.hash !== currentHash && location.hash.startsWith("#diff?") &&
      target.get("path") === diff.folder && target.get("comparison") === diff.comparison &&
      target.get("file") === entryPath(diff.entry || {}) && ["split", "unified"].includes(target.get("view"))) {
    // Back can return to the visible file while another selection is still loading.
    ++navigation;
    diffAbort?.abort();
    $("diff-section").inert = false;
    await switchDiffLayout(target.get("view"));
    return;
  }
  if (saving || (dirty() && !await confirmAction("Unsaved changes", "Discard your unsaved changes and leave this file?", "Discard changes"))) {
    history.replaceState(null, "", currentHash || routeURL("browse", currentFolder));
    if (diff) { $("diff-comparison").value = diff.comparison; $("diff-layout").value = diff.layout; }
    return;
  }
  const token = ++navigation;
  currentHash = location.hash;
  const [mode, query = ""] = location.hash.slice(1).split("?");
  const params = new URLSearchParams(query);
  const path = params.get("path") || "";
  notify("");
  if (mode === "diff") {
    destroyEditor();
    await loadDiff(path, params, token);
    return;
  }
  diffAbort?.abort();
  clearDiff();
  $("show-diff").disabled = true;
  $("git-context").textContent = "Checking repository…";
  // Prevent edits to the previous document while a new one is loading.
  destroyEditor();
  showSection("browser");
  $("file-list").replaceChildren(loadingState(mode === "edit" ? "Opening file…" : "Loading files…"));
  try {
    if (mode === "new") { openEditor(path, { content: "" }, true); return; }
    if (mode === "view") { currentFolder = parentPath(path); openViewer(path, "", true); return; }
    if (mode === "edit") {
      currentFolder = parentPath(path);
      let data;
      try { data = await api(apiURL("text", path)); }
      catch (error) {
        if (token !== navigation) return;
        if ([413, 415].includes(error.status)) { openViewer(path, error.message, false); return; }
        throw error;
      }
      if (token !== navigation) return;
      openEditor(path, data, false);
    } else {
      const data = await api(apiURL("browse", path));
      if (token !== navigation) return;
      destroyEditor(); showSection("browser"); renderFiles(data);
    }
  } catch (error) {
    if (token === navigation) {
      $("file-list").replaceChildren(element("div", "empty", "Unable to open this path. Use the folder links above or try Refresh."));
      notify(error.message, true);
    }
  }
}
async function save() {
  if (!editor || saving) return;
  const path = isNew ? $("new-path").value : editingPath;
  if (!path || path.endsWith("/")) { notify("Enter a file path including its filename.", true); $("new-path").focus(); return; }
  const contents = editor.state.doc.toString();
  saving = true; $("new-path").disabled = true; updateSaveState(); notify("");
  try {
    const result = await api("./api/text", { method: "PUT", headers: { "Content-Type": "application/json", "X-Web-Edit": "1" }, body: JSON.stringify({ path, content: contents, version: fileVersion }) });
    savedText = contents; fileVersion = result.version; editingPath = path; isNew = false;
    currentFolder = parentPath(path);
    currentHash = routeURL("edit", path); history.replaceState(null, "", currentHash);
    $("editor-title").textContent = path; $("new-path-label").hidden = true;
    $("editor-download").hidden = false; $("editor-download").href = apiURL("download", path);
    notify(`Saved ${path}.`);
  } catch (error) {
    notify(error.status === 409 ? `${error.message} Your edits are still here. ${isNew ? "Choose a different path." : "Copy your edits before reopening the file to load the latest version."}` : error.message, true);
  } finally { saving = false; $("new-path").disabled = false; updateSaveState(); }
}

function diffDirty() { return Boolean(diffEditor && diff?.file?.editable && diffEditor.state.doc.toString() !== diff.baseline); }
function diffRoute(folder, comparison = "all", file = "", view = defaultLayout()) {
  return `#diff?${new URLSearchParams({ path: folder, comparison, file, view })}`;
}
const entryPath = (entry) => entry.new_path || entry.old_path || "";
const gitReasons = {
  not_repository: "Not in a Git repository", git_not_installed: "Git is not installed",
  outside_workspace: "Repository is outside the workspace", unreadable: "Cannot read this repository",
};
async function discoverGit(path, token) {
  contextAbort?.abort();
  contextAbort = new AbortController();
  $("show-diff").disabled = true;
  $("git-context").textContent = "Checking repository…";
  try {
    const context = await api(apiURL("git/context", path), { signal: contextAbort.signal });
    if (token !== navigation || path !== currentFolder || $("browser").hidden) return;
    $("show-diff").disabled = !context.available;
    $("git-context").textContent = context.available ? `Repository: ${context.repo_path || "."}` : (gitReasons[context.reason] || context.reason || "Cannot read this repository");
  } catch (error) {
    if (token !== navigation || error.name === "AbortError") return;
    $("git-context").textContent = "Cannot read this repository";
  }
}
function destroyDiffEditor() {
  if (merge) merge.destroy();
  else diffEditor?.destroy();
  merge = null; diffEditor = null;
  $("diff-editor").replaceChildren();
}
function clearDiff() { destroyDiffEditor(); diff = null; }
function replaceDiffHash() {
  currentHash = diffRoute(diff.folder, diff.comparison, diff.entry ? entryPath(diff.entry) : "", diff.layout);
  history.replaceState(null, "", currentHash);
}
async function loadDiff(folder, params, token, nextPath = "") {
  contextAbort?.abort(); diffAbort?.abort();
  diffAbort = new AbortController();
  const signal = diffAbort.signal;
  const comparison = ["all", "unstaged", "staged"].includes(params.get("comparison")) ? params.get("comparison") : "all";
  const layout = ["split", "unified"].includes(params.get("view")) ? params.get("view") : defaultLayout();
  const hint = params.get("file") || "";
  const previous = diff;
  if (previous?.folder !== folder) {
    clearDiff();
    $("diff-list").replaceChildren(); $("diff-title").textContent = "";
    $("diff-metadata").replaceChildren(); $("diff-labels").replaceChildren();
    $("diff-repo").textContent = "";
    $("diff-editor").append(loadingState("Loading changes…"));
  }
  currentFolder = folder;
  $("diff-files").onclick = () => { location.hash = routeURL("browse", folder); };
  showSection("diff-section");
  updateDiffSaveState();
  $("diff-section").inert = true;
  try {
    const context = await api(apiURL("git/context", folder), { signal });
    if (!context.available) throw new Error(gitReasons[context.reason] || context.reason || "Cannot read this repository");
    const list = await api(`${apiURL("git/changes", folder)}&comparison=${comparison}`, { signal });
    const selected = list.entries.find((entry) => entryPath(entry) === hint) || [nextPath].flat().map((path) => list.entries.find((entry) => entryPath(entry) === path)).find(Boolean) || list.entries[0] || null;
    const file = selected ? await api(`${apiURL("git/file", folder)}&${new URLSearchParams({ comparison, entry: selected.id, snapshot: list.snapshot })}`, { signal }) : null;
    if (token !== navigation) return;
    destroyDiffEditor();
    diff = { folder, comparison, layout, list, entry: selected, file, baseline: file ? normalise(file.new?.content || "") : "" };
    currentFolder = folder;
    renderDiff(); replaceDiffHash();
  } catch (error) {
    if (token !== navigation || error.name === "AbortError") return;
    if (!diff) {
      $("diff-editor").replaceChildren(element("p", "empty", "Unable to load changes. Try Refresh or return to Files."));
      $("diff-files").onclick = () => { location.hash = routeURL("browse", folder); };
    } else {
      $("diff-comparison").value = diff.comparison; $("diff-layout").value = diff.layout;
      replaceDiffHash();
    }
    notify(error.message, true);
  } finally {
    if (token === navigation) $("diff-section").inert = false;
  }
}
function renderDiff() {
  $("diff-section").dataset.layout = diff.layout;
  $("diff-repo").textContent = `Repository: ${diff.list.repo_path || "."}`;
  $("diff-comparison").value = diff.comparison; $("diff-layout").value = diff.layout;
  $("diff-list").replaceChildren();
  $("diff-files").onclick = () => { location.hash = routeURL("browse", diff.folder); };
  for (const entry of diff.list.entries) {
    const link = element("a", "diff-file");
    link.dataset.path = entryPath(entry);
    link.href = diffRoute(diff.folder, diff.comparison, link.dataset.path, diff.layout);
    if (entry.id === diff.entry?.id) link.setAttribute("aria-current", "true");
    link.append(element("span", "", entry.old_path && entry.new_path && entry.old_path !== entry.new_path ? `${entry.old_path} → ${entry.new_path}` : entryPath(entry)), element("small", "", ({ modified: "Modified", added: "Added", deleted: "Deleted", renamed: "Renamed", untracked: "Untracked", unmerged: "Unmerged", copied: "Copied", type_changed: "Type changed" })[entry.status] || entry.status));
    const row = element("div", entry.id === diff.entry?.id ? "diff-file-row selected" : "diff-file-row");
    row.append(link);
    if (["stage", "unstage"].includes(entry.action)) {
      const button = element("button", "diff-action", entry.action === "stage" ? "Stage" : "Unstage");
      button.setAttribute("aria-label", `${button.textContent} ${entryPath(entry)}`);
      button.addEventListener("click", () => changeGitEntry(entry, button));
      row.append(button);
    }
    $("diff-list").append(row);
  }
  $("diff-title").textContent = diff.entry ? entryPath(diff.entry) : "";
  $("diff-metadata").replaceChildren(); $("diff-labels").replaceChildren();
  if (!diff.entry) $("diff-editor").replaceChildren(element("p", "empty", "No changes"));
  else renderDiffFile();
  updateDiffSaveState();
}
function renderDiffFile() {
  const file = diff.file;
  const metadata = $("diff-metadata");
  const note = (message) => metadata.append(element("p", "", message));
  if (diff.entry.old_path && diff.entry.new_path && diff.entry.old_path !== diff.entry.new_path) note(`${diff.entry.old_path} → ${diff.entry.new_path}`);
  if (diff.entry.old_mode !== diff.entry.new_mode) note(`Git mode: ${diff.entry.old_mode || "—"} → ${diff.entry.new_mode || "—"}`);
  for (const source of [file.old, file.new]) {
    if (source?.final_newline === false && source.content) note(`${source.label}: No final newline`);
  }
  if (file.reason) note(file.reason);
  if (file.reason === "Submodule") {
    note(`Old commit: ${file.old_oid || "—"}`); note(`New commit: ${file.new_oid || "—"}`);
  }
  for (const message of file.notices || []) note(message);
  if (file.old?.content != null && file.new?.content != null && !file.reason) {
    createDiffEditor();
  } else $("diff-editor").replaceChildren();
}
function diffExtensions(label, editable, previousState = null) {
  return [basicSetup, languageFor(entryPath(diff.entry))[1], diffWrapping.of(wrapDiff ? EditorView.lineWrapping : []), EditorState.readOnly.of(!editable),
    EditorView.editable.of(editable),
    EditorView.contentAttributes.of({ "aria-label": label, spellcheck: "false", autocapitalize: "off", autocorrect: "off" }),
    keymap.of([{ key: "Mod-s", run: () => { saveDiff(); return true; } }]),
    EditorView.updateListener.of((update) => { if (update.docChanged) updateDiffSaveState(); }),
    previousState ? historyField.init(() => previousState.field(historyField)) : [],
  ];
}
function createDiffEditor(previousState = null) {
  const file = diff.file;
  const b = { doc: previousState?.doc || normalise(file.new.content), selection: previousState?.selection,
    extensions: diffExtensions(file.new.label, file.editable, previousState) };
  $("diff-section").dataset.layout = diff.layout;
  $("diff-labels").className = diff.layout;
  $("diff-labels").replaceChildren(element("span", "", file.old.label), element("span", "diff-new-label", file.new.label));
  if (diff.layout === "split") {
    merge = new MergeView({ a: { doc: normalise(file.old.content), extensions: diffExtensions(file.old.label, false) }, b,
      parent: $("diff-editor"), collapseUnchanged: { margin: 3 }, diffConfig });
    diffEditor = merge.b;
  } else {
    b.extensions.push(unifiedMergeView({ original: normalise(file.old.content), mergeControls: false, allowInlineDiffs: false,
      collapseUnchanged: { margin: 3 }, diffConfig }));
    diffEditor = new EditorView({ ...b, parent: $("diff-editor") });
  }
  makeExpansionAccessible();
}
// CodeMirror's collapsed-line widget uses a mouse handler. Add keyboard access too.
function makeExpansionAccessible() {
  for (const widget of $("diff-editor").querySelectorAll(".cm-collapsedLines")) {
    if (widget.hasAttribute("tabindex")) continue;
    widget.tabIndex = 0; widget.setAttribute("role", "button");
    widget.setAttribute("aria-label", `Expand unchanged lines: ${widget.textContent}`);
    widget.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); widget.click(); }
    });
  }
}
new MutationObserver(makeExpansionAccessible).observe($("diff-editor"), { childList: true, subtree: true });
function updateDiffSaveState() {
  const editable = Boolean(diff?.file?.editable);
  $("diff-save").hidden = !editable; $("diff-copy").hidden = !editable;
  $("diff-save").disabled = saving || !diffDirty();
  $("diff-save-state").textContent = gitAction ? gitAction === "stage" ? "Staging…" : "Unstaging…" : editable ? saving ? "Saving…" : diffDirty() ? "Unsaved changes" : "All changes saved" : "Read-only";
  for (const button of $("diff-list").querySelectorAll(".diff-action")) {
    button.disabled = saving || diffDirty();
    button.title = diffDirty() ? "Save or discard edits before staging." : "";
  }
  if (diffDirty() && $("diff-list").querySelector(".diff-action")) $("diff-save-state").textContent += ". Save or discard edits before staging.";
  $("diff-open").hidden = !diff?.file?.working_path || Boolean(diff?.file?.reason);
  if (diff?.file?.working_path) $("diff-open").href = routeURL("edit", diff.file.working_path);
  for (const id of ["diff-refresh", "diff-files", "diff-comparison", "diff-layout"]) $(id).disabled = saving;
  for (const id of ["diff-next", "diff-previous"]) $(id).disabled = !diffEditor || saving;
  const label = $("diff-labels").querySelector(".diff-new-label");
  if (label) label.textContent = diff.file.new.label + (diffDirty() ? " · Unsaved changes" : "");
}
async function switchDiffLayout(layout) {
  if (!diff || saving) return;
  const state = diffEditor?.state;
  destroyDiffEditor();
  diff.layout = layout; preferredLayout = layout;
  $("diff-section").dataset.layout = layout;
  $("diff-layout").value = layout;
  for (const link of $("diff-list").querySelectorAll("a")) {
    link.href = diffRoute(diff.folder, diff.comparison, link.dataset.path, layout);
  }
  try { localStorage.setItem("web-edit-diff-layout", layout); } catch { /* Keep the session preference. */ }
  if (state) createDiffEditor(state);
  updateDiffSaveState(); replaceDiffHash();
}
async function changeGitEntry(entry, button) {
  if (!diff || saving) return;
  if (diffDirty()) { notify("Save or discard edits before staging.", true); return; }
  const action = entry.action;
  if (!["stage", "unstage"].includes(action)) return;
  const index = diff.list.entries.findIndex((item) => item.id === diff.entry?.id);
  const fallback = [diff.list.entries[index + 1], diff.list.entries[index - 1]].filter(Boolean).map(entryPath);
  let restoreFocus = false;
  saving = true; gitAction = action; updateDiffSaveState();
  notify(action === "stage" ? "Staging…" : "Unstaging…");
  $("diff-section").inert = true;
  $("diff-list").inert = true;
  try {
    await api("./api/git/action", { method: "POST", headers: { "Content-Type": "application/json", "X-Web-Edit": "1" },
      body: JSON.stringify({ path: diff.folder, comparison: diff.comparison, entry: entry.id, snapshot: diff.list.snapshot, action }) });
    notify("");
    await loadDiff(diff.folder, new URLSearchParams(currentHash.split("?")[1]), ++navigation, fallback);
    restoreFocus = !button.isConnected;
    const done = action === "stage" ? "File staged." : "File unstaged.";
    if (!$("notice").hidden) notify(`${done} Change list is stale. ${$("notice").textContent}`, true);
    else notify(done);
  } catch (error) {
    notify(error.message, true);
  } finally {
    saving = false; gitAction = null;
    $("diff-section").inert = false; $("diff-list").inert = false; updateDiffSaveState();
    if (restoreFocus) ($("diff-list").querySelector("a[aria-current]") || $("diff-files")).focus({ preventScroll: true });
  }
}
async function saveDiff() {
  if (!diffEditor || !diff.file.editable || saving || !diffDirty()) return;
  const content = diffEditor.state.doc.toString();
  saving = true; updateDiffSaveState(); notify("");
  $("diff-section").inert = true;
  // Prevent file-list links and breadcrumbs while the versioned write is in flight.
  $("diff-list").inert = true;
  try {
    const result = await api("./api/text", { method: "PUT", headers: { "Content-Type": "application/json", "X-Web-Edit": "1" },
      body: JSON.stringify({ path: diff.file.working_path, content, version: diff.file.version }) });
    diff.baseline = content; diff.file.version = result.version;
    const token = ++navigation;
    const index = diff.list.entries.findIndex((entry) => entry.id === diff.entry.id);
    const nextPath = entryPath(diff.list.entries[index + 1] || {});
    await loadDiff(diff.folder, new URLSearchParams(currentHash.split("?")[1]), token, nextPath);
    if (!$("notice").hidden) notify(`File saved, but the change list is stale. ${$("notice").textContent}`, true);
    else notify("File saved.");
  } catch (error) {
    notify(error.status === 409 ? `${error.message} Your edits are still here. Copy your edits before reopening the file to load the latest version.` : error.message, true);
  } finally {
    saving = false; $("diff-section").inert = false; $("diff-list").inert = false; updateDiffSaveState();
  }
}
$("show-diff").addEventListener("click", () => { location.hash = diffRoute(currentFolder); });
$("diff-refresh").addEventListener("click", route);
$("diff-comparison").addEventListener("change", () => { location.hash = diffRoute(diff?.folder ?? currentFolder, $("diff-comparison").value, "", diff?.layout || defaultLayout()); });
$("diff-layout").addEventListener("change", () => switchDiffLayout($("diff-layout").value));
$("diff-wrap").addEventListener("change", () => {
  wrapDiff = $("diff-wrap").checked;
  try { localStorage.setItem("web-edit-diff-wrap", String(wrapDiff)); } catch { /* Keep the session preference. */ }
  for (const view of merge ? [merge.a, merge.b] : diffEditor ? [diffEditor] : []) {
    view.dispatch({ effects: diffWrapping.reconfigure(wrapDiff ? EditorView.lineWrapping : []) });
  }
});
$("diff-save").addEventListener("click", saveDiff);
$("diff-copy").addEventListener("click", async () => {
  try { await copyText(diffEditor.state.doc.toString()); notify("Copied to the clipboard."); }
  catch { notify("Could not copy to the clipboard.", true); }
});
$("diff-next").addEventListener("click", () => { if (diffEditor) { goToNextChunk(diffEditor); diffEditor.focus(); } });
$("diff-previous").addEventListener("click", () => { if (diffEditor) { goToPreviousChunk(diffEditor); diffEditor.focus(); } });

const queue = [];
let activeUploads = 0;
function updateQueue() {
  const completed = queue.filter((item) => item.state === "done").length;
  const waiting = queue.filter((item) => item.state === "queued").length;
  $("upload-summary").textContent = queue.length ? `${completed} of ${queue.length} uploaded${activeUploads ? ` · ${activeUploads} uploading` : ""}` : "";
  $("start-uploads").disabled = !waiting;
}
function updateItem(item) {
  item.input.disabled = ["uploading", "done", "pending"].includes(item.state);
  item.progress.value = item.percent;
  item.status.textContent = item.message;
  item.controls.replaceChildren();
  if (item.state === "error") {
    const retry = element("button", "secondary", item.conflict ? "Replace existing" : "Retry");
    retry.addEventListener("click", async () => {
      if (item.conflict && !await confirmAction("Replace existing file", `Replace the existing file at ${item.input.value}?`, "Replace file")) return;
      item.version = item.conflict?.version;
      item.state = "pending"; item.message = "Waiting…"; updateItem(item); pump();
    });
    item.controls.append(retry);
  }
  if (!["done", "pending"].includes(item.state)) {
    const remove = element("button", "secondary", item.state === "uploading" ? "Cancel" : "Remove");
    remove.addEventListener("click", () => {
      if (item.state === "uploading") item.xhr.abort();
      else { queue.splice(queue.indexOf(item), 1); item.row.remove(); updateQueue(); }
    });
    item.controls.append(remove);
  }
  updateQueue();
}
function addFiles(files) {
  for (const file of files) {
    const item = { file, state: "queued", percent: 0, message: `${size(file.size)} · Ready to upload`, version: undefined, conflict: null };
    item.row = element("div", "upload-item");
    const label = element("label", "", `Destination for ${file.name}`);
    item.input = element("input"); item.input.value = joinPath($("upload-folder").value, file.name); item.input.spellcheck = false;
    item.input.addEventListener("input", () => { item.version = undefined; item.conflict = null; item.state = "queued"; item.message = `${size(file.size)} · Ready to upload`; updateItem(item); });
    label.append(item.input);
    const info = element("div", "upload-info");
    item.status = element("span", "upload-status"); item.status.setAttribute("role", "status");
    item.controls = element("div", "upload-controls"); info.append(item.status, item.controls);
    item.progress = element("progress"); item.progress.max = 100; item.progress.value = 0; item.progress.setAttribute("aria-label", `Upload progress for ${file.name}`);
    item.row.append(label, info, item.progress); $("upload-list").append(item.row);
    queue.push(item); updateItem(item);
  }
}
function uploadFile(item) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest(); item.xhr = xhr;
    xhr.open("POST", "./api/upload"); xhr.setRequestHeader("X-Web-Edit", "1");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        item.percent = Math.round(event.loaded / event.total * 100);
        item.message = item.percent === 100 ? "Saving on server…" : `${item.percent}% · ${size(item.file.size)}`;
        item.progress.value = item.percent; item.status.textContent = item.message;
      }
    });
    const fail = (message) => { item.state = "error"; item.message = message; };
    xhr.addEventListener("load", () => {
      let data;
      try { data = JSON.parse(xhr.responseText); } catch { data = { error: `Upload failed (${xhr.status}).` }; }
      if (xhr.status >= 200 && xhr.status < 300) { item.state = "done"; item.percent = 100; item.message = `${size(item.file.size)} · Uploaded`; item.conflict = null; }
      else { fail(data.error || "Upload failed."); item.conflict = xhr.status === 409 && data.version ? data : null; }
    });
    xhr.addEventListener("error", () => fail("Connection failed. Retry this file."));
    xhr.addEventListener("abort", () => fail("Upload cancelled. You can retry."));
    xhr.addEventListener("loadend", resolve);
    const form = new FormData(); form.append("file", item.file); form.append("path", item.input.value);
    if (item.version) form.append("version", item.version);
    xhr.send(form);
  });
}
function pump() {
  while (activeUploads < 3) {
    const item = queue.find((candidate) => candidate.state === "pending");
    if (!item) break;
    item.state = "uploading"; item.message = "Uploading…"; item.percent = 0; activeUploads++; updateItem(item);
    uploadFile(item).finally(async () => {
      activeUploads--; updateItem(item); pump();
      if (!activeUploads && !$("browser").hidden) {
        const folder = currentFolder;
        try { const data = await api(apiURL("browse", folder)); if (currentFolder === folder && !$("browser").hidden) renderFiles(data); } catch (error) { notify(error.message, true); }
      }
    });
  }
  updateQueue();
}
$("start-uploads").addEventListener("click", () => { for (const item of queue) if (item.state === "queued") { item.state = "pending"; item.message = "Waiting…"; updateItem(item); } pump(); });
$("show-upload").addEventListener("click", () => { $("uploads").hidden = false; $("upload-folder").value = currentFolder; $("uploads").scrollIntoView({ behavior: "smooth", block: "start" }); });
$("close-uploads").addEventListener("click", () => { $("uploads").hidden = true; });
$("file-picker").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
const drop = $("drop-zone");
for (const name of ["dragenter", "dragover"]) drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add("dragging"); });
for (const name of ["dragleave", "drop"]) drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove("dragging"); });
drop.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
$("new-file").addEventListener("click", () => { location.hash = routeURL("new", currentFolder ? `${currentFolder}/` : ""); });
$("refresh").addEventListener("click", route);
for (const id of ["back", "viewer-back"]) $(id).addEventListener("click", () => { location.hash = routeURL("browse", currentFolder); });
$("save").addEventListener("click", save);
$("editor-copy").addEventListener("click", async () => {
  if (!editor) return;
  try { await copyText(editor.state.doc.toString()); notify("Copied to the clipboard."); }
  catch { notify("Could not copy to the clipboard.", true); }
});
$("viewer-copy").addEventListener("click", copyImage);
$("new-path").addEventListener("input", () => { if (editor) { const [name, support] = languageFor($("new-path").value); $("language-label").textContent = name; editor.dispatch({ effects: language.reconfigure(support) }); } });
window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (event) => { if (dirty() || saving || activeUploads) { event.preventDefault(); event.returnValue = ""; } });
document.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s" && (editor || diffEditor)) { event.preventDefault(); if (diffEditor) saveDiff(); else save(); } });
route();
