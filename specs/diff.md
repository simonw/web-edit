# Git diff view

## Goal

Add a view of local Git changes with direct editing of working-tree text. The default view shows two versions of one file side by side. The application must work when its startup folder is not a Git repository.

This document proposes the first implementation. It permits ordinary file saves and per-file Stage and Unstage actions. It does not change repository history.

## Main decisions

| Item | Decision |
| --- | --- |
| Entry point | A **Diff** button beside **Refresh**, **Upload files**, and **New file** |
| Repository discovery | Find the repository for the current browser folder, not the startup folder |
| Scope | Show changes across that repository, even from a nested folder |
| Default comparison | **All changes**, from `HEAD` to files on disk |
| Default layout | **Side by side** on wide screens, **Unified** on narrow screens |
| Other comparisons | **Unstaged** and **Staged** |
| Editing | Editable working-tree contents in both layouts, with read-only comparison sources |
| Git dependency | The server uses the local `git` executable |
| Browser dependency | Bundle `@codemirror/merge` with the existing frontend assets |

## Repository discovery

The startup folder remains the filesystem boundary. Repository discovery starts from the current browser folder on each navigation and refresh. It does not scan child folders.

For example, web-edit starts at `/workspaces`:

| Current folder | Diff state | Repository scope |
| --- | --- | --- |
| `/workspaces` | Disabled, unless this folder belongs to a permitted repository | None by default |
| `/workspaces/project-a` | Enabled | `project-a` |
| `/workspaces/project-a/src` | Enabled | `project-a`, not only `src` |
| `/workspaces/project-b` | Enabled | `project-b` |
| `/workspaces/notes` without Git | Disabled | None |

A folder belongs to a repository when Git identifies it as part of a working tree. The folder does not need its own `.git` directory. Discovery must support linked worktrees and repositories with a `.git` file.

The repository root must remain inside the startup folder. If Git finds a repository above that boundary, the Diff button stays disabled. The application must not expose sibling files through Git.

A nested repository uses its own repository root. Discovery does not combine repositories. A bare repository does not support this view.

The button stays visible but disabled during discovery. A short explanation appears beside the button or through an accessible description. A disabled-button tooltip alone is not sufficient.

The explanations distinguish these states:

- **Not in a Git repository**
- **Git is not installed**
- **Repository is outside the workspace**
- **Cannot read this repository**.

A clean repository still enables the button. Its diff view shows **No changes**. A Git error must not prevent ordinary file operations.

## Screen layout

The Diff button opens a new section in the existing application. It does not open a modal or a new browser tab.

This section temporarily replaces the all-files list on screen. Its changed-files list serves only the Diff section. The existing file browser remains unchanged. The **Files** button restores that browser and its original folder.

The section contains these controls:

- A **Files** button that returns to the folder that opened the view
- The repository path relative to the startup folder
- A **Changes** selector with **All changes**, **Unstaged**, and **Staged**
- A **View** selector with **Side by side** and **Unified**
- A **Refresh** button
- A **Save** button and save status for an editable file
- A list of changed files
- A comparison area for the selected file.

The changed-file list appears above the comparison area on all screens. Compact rows show a repository-relative path and a text status. A rename entry shows both paths. The list has a maximum height of 160px and scrolls vertically.

The header, navigation, controls, and changed-file list retain a centered container with a maximum width of 1128px. The side-by-side comparison uses the viewport width with 24px gutters. Mobile uses 14px gutters. The unified comparison retains the centered width limit.

The heading states **Repository changes**. The repository path makes the scope explicit after entry from a nested folder.

The first load selects the first changed file. If that entry still exists, refresh retains the selection. Otherwise, refresh selects the first remaining entry. An empty list clears the comparison area.

The view loads file contents only after selection. It must not create an editor for every changed file.

## Comparison sources

Git supplies the repository state and the source contents. CodeMirror compares the two text documents. CodeMirror does not discover repositories or interpret the Git index.

| Changes selector | Old source | New source | Included files |
| --- | --- | --- | --- |
| **All changes** | `HEAD` | Working tree | Net tracked changes and non-ignored untracked files |
| **Unstaged** | Index | Working tree | Unstaged tracked changes and non-ignored untracked files |
| **Staged** | `HEAD` | Index | Staged changes only |

The default, **All changes**, represents the final disk contents against the last commit. It is not a concatenation of staged and unstaged patches. Opposing staged and unstaged changes can cancel each other in this comparison.

An added file has an empty old source. A deleted file has an empty new source. An untracked file has an empty old source and uses its disk contents as the new source.

Before the first commit, `HEAD` represents an empty tree for these comparisons. An index entry can still supply the old source for **Unstaged**. The implementation must not hard-code a SHA-1 empty-tree identifier.

Ignored untracked files do not appear. Tracked files still appear if ignore rules match them. The initial comparison uses saved files, not unsaved contents from another editor. Edits within Diff update the visible comparison immediately, but they do not change disk contents until Save.

Git rename detection supplies old and new paths for each comparison. A rename does not require a custom rename algorithm. An untracked destination can appear as a deletion and an addition before Git recognizes a rename.

## Layout choices

### Side by side

`MergeView` from `@codemirror/merge` provides two editors and aligns unchanged lines vertically. Source A appears on the left. Source B appears on the right. Each pane has a source label, such as **HEAD** or **Working tree**.

`MergeView` supports separate editor configuration for each pane. Source A always uses read-only editor state. Source B permits edits only for a supported, existing regular working-tree file in **All changes** or **Unstaged**.

Both panes remain read-only in **Staged**. The view omits `revertControls` because the first implementation does not provide chunk restore actions. Text selection and copy remain available. Existing language support supplies syntax colors.

Both layouts expand vertically with their contents. The page provides vertical scrolling without an inline vertical scroll area. Each comparison pane retains its own horizontal scrollbar. Long lines do not wrap by default. A **Wrap lines** checkbox enables CodeMirror line wrapping in both layouts. The preference persists across visits. A wrap change preserves unsaved edits and undo history. Horizontal scrolling remains independent. Cursor movement keeps the edited text visible.

CodeMirror aligns unchanged lines without custom vertical scroll synchronization. Browser tests verify line alignment, page scrolling, and the absence of horizontal overflow of the page.

### Unified

`unifiedMergeView` provides a single editor with deleted text above replacement text. The editor document contains source B. The `original` value contains source A.

This layout resembles a unified diff, but it is not a literal Git patch. CodeMirror computes its own changed chunks. Chunk boundaries can differ from `git diff` output.

The implementation sets `mergeControls: false`. Otherwise, CodeMirror shows accept and reject controls by default. The editor permits the same working-tree edits as source B in the side-by-side layout. The original document remains read-only.

Inline character colors remain available, but `allowInlineDiffs` stays disabled for predictable line-based output.

### Literal Git patch

A literal patch contains Git headers, `@@` markers, file modes, and Git-specific notices. CodeMirror's unified view does not produce this format.

A future **Patch** view can show server-generated `git diff` output as read-only text. It needs separate treatment for untracked files and patch export. The first implementation does not include this third layout.

### View selection

The first visit selects **Side by side** at widths of at least 900 CSS pixels. Smaller widths select **Unified**. This breakpoint is a proposed design value, not a CodeMirror limit.

An explicit selection overrides the screen-width default and persists in local storage. The preference applies across repositories. A storage error falls back to a session preference. Resizing must not override an explicit selection.

Changing the layout uses the same source contents and does not fetch them again. It preserves unsaved text, the saved baseline, the file version, and the undo history. The application destroys the previous CodeMirror instance before it creates another. It preserves the selected file and the nearest visible change where practical.

Both layouts initially collapse long unchanged regions with three context lines. Each collapsed region offers an accessible expansion control. **Previous change** and **Next change** controls move between changed chunks.

## Special file states

| State | Behavior |
| --- | --- |
| Binary or non-UTF-8 file | Show **Text diff is not available for this file** |
| File over the size limit | Show **File is too large for text diff** |
| Mode-only change | Show the old and new Git modes, even if text is identical |
| Rename without text changes | Show both paths and **No text changes** |
| Line-ending-only change | Show a line-ending notice if normalization removes the visible difference |
| BOM-only change | Show an encoding notice if BOM removal hides the visible difference |
| Missing final newline | Show the newline state for each source |
| Symbolic link | Show a status entry without following the link or loading its target |
| Submodule | Show a status entry and commit identifiers without descending into its files |
| Merge conflict | Show **Unmerged file** without a two-source text comparison |
| Unavailable sparse file | Show **File content is not available on disk** |

The first implementation does not provide conflict resolution or three-way comparisons. If repository discovery permits it, a user can enter a submodule folder to inspect its own changes.

## Editing and saving

Both layouts support direct edits to working-tree text through the CodeMirror editor. The application owns Save behavior. The merge package does not write files.

The editable source uses the existing `PUT /api/text` endpoint and its version check. The file response supplies the working-tree content hash as the save version. Save uses a startup-relative file path, not a repository-relative path.

**Save** and **Ctrl/Command+S** save only the selected working-tree file. There is no autosave. Save preserves existing line-ending and BOM behavior. The index and `HEAD` remain unchanged.

An unsaved edit updates the visible changed chunks. It does not update the repository list until Save or Refresh. The source label shows **Working tree · Unsaved changes** until Save succeeds.

After Save, the application refreshes status and retains the file selection if possible. If the file no longer has changes, the view selects the next entry. If status refresh fails after a successful save, the view reports that the file saved but the list is stale.

A save conflict keeps the unsaved text and offers the existing copy-and-reopen recovery path. Save failure never replaces the edited document. Saving disables navigation and refresh until the request finishes.

Selecting another file, comparison, or route requires the existing discard confirmation if the document has unsaved edits. Refresh also requires that confirmation. A layout switch preserves edits without confirmation. The unload warning includes unsaved Diff edits.

Deleted files, symbolic links, submodules, conflicts, unsupported text, and unavailable disk files remain read-only. The view does not recreate deleted files. **Staged** remains read-only even if a corresponding disk file exists.

An **Open in editor** link remains available for a supported working-tree file. This link uses the same unsaved-change guard as other navigation.

## Stage and Unstage

Each changed-file row offers **Stage** or **Unstage** beside its status. These actions affect the selected file, not individual changed chunks.

**Unstaged** offers **Stage**. **Staged** offers **Unstage**. **All changes** offers **Stage** if unstaged changes exist for that entry. Otherwise, it offers **Unstage**. A partially staged file offers **Stage** to include its remaining disk changes.

Stage updates exact index entries from verified disk contents. Stage supports regular files up to 10 MiB, including binary files. Files that use Git filters require an external Git tool for Stage. Unstage remains available for these files. Unstage restores the index entry from `HEAD`. Before the first commit, Unstage removes the index entry. Neither action changes disk contents or creates a commit.

The actions remain disabled while the diff contains unsaved edits. The interface explains that the user must save or discard those edits first. It does not stage unsaved editor contents.

After an action, the application refreshes the current comparison. It retains the selection if the entry remains. Otherwise, it selects the next remaining file. The Unstaged list becomes shorter after each successful Stage action.

The server validates the selected entry and snapshot before an action. It holds the Git index lock during final validation and publication. Exact index updates prevent an action from affecting unselected files. A stale result requires Refresh. A failed action keeps the current comparison visible with an error notice. The application disables concurrent actions during the request.

## API design

A new `GitStore` in `web_edit/git.py` owns discovery, status, and source retrieval. It uses the existing `FileStore` boundary checks. `web_edit/app.py` exposes three read-only endpoints and one endpoint that updates the index.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/git/context?path=...` | Find the repository for a browser folder |
| `GET /api/git/changes?path=...&comparison=all` | List entries for `all`, `unstaged`, or `staged` |
| `GET /api/git/file?path=...&comparison=all&entry=...&snapshot=...` | Read the selected pair of sources |
| `POST /api/git/action` | Stage or unstage one entry with its comparison and snapshot |

The `path` parameter always uses a path relative to the startup folder. Entry paths in the result use paths relative to the repository root. Separate field names must prevent confusion between these path types.

The context response contains `available`, `reason`, and `repo_path`. Expected disabled states use `available: false`. Invalid paths and unexpected server errors use the existing JSON error format.

The changes response contains the repository path, comparison, snapshot token, and entries. Each entry contains an opaque identifier, status, old path, new path, Git modes, and its available action. The server creates the identifier. It does not accept arbitrary Git revision expressions from the client.

The file response contains both source labels, content, byte sizes, and content hashes. It also contains file modes, newline metadata, and a reason if a text comparison is unavailable. Metadata-only entries do not require text content.

The snapshot token identifies the relevant commit, index state, and listed file state. The server checks this token before source retrieval. It also checks source stability during retrieval. An external change produces HTTP 409 with **Repository changed. Refresh the diff**.

Git reads do not form an atomic filesystem snapshot. Consistency checks must reject detected changes rather than imply a transaction. Refresh replaces the file list and both source documents together. A refresh error leaves the previous result visible with an error notice.

## Git operations and security

Repository discovery uses `git -C <folder> rev-parse` to identify the working tree and its root. Status uses machine-readable, NUL-delimited output, such as `status --porcelain=v2 -z`. Comparison-specific commands determine net entries and rename pairs.

Source retrieval uses verified object identifiers for committed and index blobs. A bounded blob reader, such as `cat-file`, retrieves those objects. Working-tree reads use the existing regular-file and symlink checks. User paths must never become revision expressions.

The Git runner applies these constraints:

- It passes arguments as an array without a shell.
- It clears inherited Git variables that can redirect repository discovery or object access.
- It prevents discovery above the startup boundary.
- It uses `--` and literal path handling for commands that accept file paths.
- It parses NUL-delimited names without line-based splitting.
- It disables external diff drivers, text conversion, pagers, optional index writes, and interactive prompts.
- It does not run hooks, fetch objects, contact remotes, or initialize submodules.
- It does not add `safe.directory` exceptions or change Git configuration.
- It bounds runtime and output before it buffers complete results.
- It escapes filenames through DOM text nodes, not HTML.
- It preserves the existing host protection and no-store response headers.

Linked worktrees can keep Git metadata outside the startup boundary. The server can read Git metadata through Git for a permitted working tree. This exception does not permit browsing external working-tree files or serving arbitrary metadata files.

The implementation permits per-file Stage and Unstage actions, but no working-tree restore, commit, or Git discard actions. Ordinary Save writes only the selected working-tree file. Git remains optional. No Git subprocess runs during application startup.

## Platform support

The implementation pins directory descriptors during Git reads to prevent path replacement from exposing files outside the startup boundary. This protection requires POSIX directory operations and an accessible descriptor filesystem.

Linux uses `/proc/self/fd`. A verified `/dev/fd` path provides a fallback. Native macOS behavior still requires verification. Windows disables Diff with **Git diff is not supported on this platform**. Ordinary file operations remain available.

## Limits and refresh behavior

Each source uses the existing 10 MiB text limit. The Git reader enforces this limit for blobs as well as disk files. The combined text response contains at most two permitted source documents.

Proposed initial limits are a ten-second Git timeout and 10,000 changed entries. Status output also has an independent byte limit. A result above a limit shows an explicit error, not a silently incomplete list.

CodeMirror receives a finite diff timeout and scan limit. Its bounded algorithm can produce coarse changed chunks for difficult files. The view must remain responsive and must not describe those chunks as exact Git patch boundaries.

Refresh reruns discovery, status, and the selected comparison. The first implementation has no polling or filesystem watcher. A late response from a previous folder, comparison, or file must not replace the current result.

The frontend uses the existing navigation token pattern and cancels obsolete fetches where possible. A repository switch clears the previous entries and source contents immediately.

## Navigation and accessibility

A hash route records the browser folder, comparison, selected entry, and layout. A proposed form is `#diff?path=project-a/src&comparison=all&file=src/main.py&view=split`.

The `file` value is a selection hint, not authority to read a file. The server must resolve it through the permitted change list. Refresh, direct links, and browser history must restore the view after fresh repository discovery.

The existing unsaved-change guard applies before navigation from an editor. Entering or leaving Diff never saves or discards editor contents implicitly. Returning to Files restores the folder that opened Diff, not always the repository root.

All selectors and panes have accessible labels. File status uses text, not color alone. Loading and error notices use the existing status region. Keyboard users can select files, switch layouts, expand context, and move between changes.

## Implementation sequence

1. Add the bounded Git runner and repository discovery tests.
2. Add comparison-specific entries and source retrieval tests.
3. Add the read-only API routes and boundary tests.
4. Add the Diff section, route, file list, and guarded file saves.
5. Add both CodeMirror layouts and the view preference.
6. Add browser tests for navigation, refresh, and accessibility.
7. Rebuild the frontend assets and verify the wheel.

The implementation changes `frontend/app.js`, `web_edit/static/index.html`, and `web_edit/static/app.css`. It also adds `@codemirror/merge` to `package.json` and updates `package-lock.json`. The existing build script bundles the dependency and collects its license.

After frontend changes, the implementation must run these commands:

```bash
npm ci
npm run build
uv run pytest
uv build --clear
uv run scripts/check_wheel.py dist/*.whl
```

The implementation must commit the regenerated assets and license file. The wheel must work without Node.js or a CDN. Only the Git feature requires a local Git executable.

## Acceptance tests

The test suite covers these cases:

- Startup at a non-repository parent folder leaves file operations available.
- Entry into either child repository enables Diff for that repository alone.
- Entry into a nested folder retains repository-wide scope.
- Navigation to the parent folder disables Diff again.
- A repository above the startup boundary cannot expose files.
- A linked worktree works without a `.git` directory in its root.
- A clean repository shows **No changes**.
- Both layouts compare identical source contents.
- Staged, unstaged, and opposing changes obey the comparison table.
- A repository without commits supports staged and untracked files.
- Added, deleted, renamed, ignored, and untracked files obey the inclusion rules.
- Binary files, large files, modes, line endings, and final newlines receive explicit treatment.
- Conflicts, submodules, sparse files, and symbolic links do not trigger unsafe reads.
- Spaces, tabs, newlines, Unicode, and leading hyphens in filenames remain safe.
- External changes produce a refresh notice instead of mixed source versions.
- Missing Git, unsafe ownership, timeouts, and output limits produce useful errors.
- External diff and text-conversion configuration cannot run programs.
- Unsaved editor contents survive cancelled navigation to Diff.
- Rapid navigation cannot show contents from the previous repository.
- Narrow screens select Unified until the user chooses a layout.
- Both layouts permit working-tree edits and keep comparison sources read-only.
- Staged comparisons and unsupported file states reject edits.
- Both layouts omit merge controls.
- Save preserves line endings and rejects external file changes without losing edits.
- Layout switches preserve unsaved text and undo history.
- File switches, Refresh, and navigation protect unsaved Diff edits.
- Saving changes only disk contents, not the index or `HEAD`.
- Stage and Unstage affect only the selected file and preserve disk contents.
- Stage and Unstage reject stale snapshots and requests without the write header.
- Successful actions refresh the list and select the next remaining file.
- Unsaved Diff edits disable Stage and Unstage.
- Files restores the existing all-files browser and the original folder.
- The installed wheel serves all diff assets without Node.js or network access.

## Sources

- [CodeMirror merge API](https://codemirror.net/docs/ref/#merge)
- [CodeMirror side-by-side view](https://codemirror.net/docs/ref/#merge.MergeView)
- [CodeMirror unified view](https://codemirror.net/docs/ref/#merge.unifiedMergeView)
