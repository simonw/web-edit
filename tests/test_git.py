"""Git integration tests use disposable local repositories, without remote access."""

import hashlib
import os
import shutil
import subprocess

import pytest
from starlette.testclient import TestClient

from web_edit.app import create_app
from web_edit.files import FileError, FileStore
from web_edit.git import CHANGED, GitFailure, GitStore

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def git(root, *args):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.check_output(
        ["git", "-C", str(root), *args], env=env, stderr=subprocess.PIPE
    )


def init(root):
    root.mkdir(exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Tests")
    git(root, "config", "user.email", "tests@example.invalid")
    return root


def commit(root):
    git(root, "add", "--all")
    git(root, "commit", "-qm", "test")


@pytest.fixture
def repo(tmp_path):
    root = init(tmp_path / "repo")
    (root / "a.txt").write_text("original\n")
    commit(root)
    return root


def sources(store, path="", comparison="all", name=None):
    changes = store.changes(path, comparison)
    entry = next(
        (
            entry
            for entry in changes["entries"]
            if name is None or (entry["new_path"] or entry["old_path"]) == name
        ),
        None,
    )
    assert entry is not None, changes
    return store.file(path, comparison, entry["id"], changes["snapshot"])


def test_discovery_per_folder_and_boundary(tmp_path, repo):
    other = init(tmp_path / "other")
    (repo / "nested").mkdir()
    store = GitStore(FileStore(tmp_path))
    assert store.context() == {
        "available": False,
        "reason": "Not in a Git repository",
        "repo_path": None,
    }
    assert store.context("repo/nested") == {
        "available": True,
        "reason": None,
        "repo_path": "repo",
    }
    assert store.context("other")["repo_path"] == "other"
    assert store.changes("repo/nested")["entries"] == []
    assert not store.context()["available"]
    limited = GitStore(FileStore(repo / "nested"))
    assert limited.context()["reason"] == "Repository is outside the workspace"
    with pytest.raises(FileError):
        store.context("../")
    (tmp_path / "link").symlink_to(other, target_is_directory=True)
    with pytest.raises(FileError):
        store.context("link")


def test_linked_worktree_and_nested_repo(tmp_path, repo):
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-qb", "linked", str(linked))
    assert (linked / ".git").is_file()
    init(repo / "nested")
    store = GitStore(FileStore(tmp_path))
    assert store.context("linked")["repo_path"] == "linked"
    assert store.context("repo/nested")["repo_path"] == "repo/nested"
    (linked / "a.txt").write_text("linked\n")
    assert sources(store, "linked")["old"]["content"] == "original\n"


def test_comparisons_and_cancelling_changes(repo):
    store = GitStore(FileStore(repo))
    (repo / "a.txt").write_text("staged\n")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("disk\n")
    for comparison, old, new in [
        ("all", "original\n", "disk\n"),
        ("unstaged", "staged\n", "disk\n"),
        ("staged", "original\n", "staged\n"),
    ]:
        result = sources(store, comparison=comparison)
        assert (result["old"]["content"], result["new"]["content"]) == (old, new)
        assert result["editable"] == (comparison != "staged")
    (repo / "a.txt").write_text("original\n")
    assert store.changes()["entries"] == []
    assert len(store.changes(comparison="staged")["entries"]) == 1
    assert len(store.changes(comparison="unstaged")["entries"]) == 1


def test_unborn_index_and_untracked(tmp_path):
    root = init(tmp_path / "unborn")
    store = GitStore(FileStore(root))
    (root / "staged").write_text("index\n")
    git(root, "add", "staged")
    (root / "staged").write_text("disk\n")
    (root / "untracked").write_text("new\n")
    for comparison in ("all", "staged"):
        result = sources(store, comparison=comparison, name="staged")
        assert result["old"]["content"] == ""
        assert result["new"]["content"] == (
            "index\n" if comparison == "staged" else "disk\n"
        )
    assert (
        sources(store, comparison="unstaged", name="staged")["old"]["content"]
        == "index\n"
    )
    assert sources(store, name="untracked")["old"]["content"] == ""
    assert len(store.changes(comparison="staged")["entries"]) == 1


def test_rename_delete_ignore_and_unusual_names(repo):
    git(repo, "mv", "a.txt", "renamed.txt")
    (repo / ".gitignore").write_text("ignored\n")
    (repo / "ignored").write_text("hidden")
    names = ["-leading", "space name", "tab\tname", "line\nname", "café", ":(glob)*"]
    for name in names:
        (repo / name).write_text(name)
    store = GitStore(FileStore(repo))
    changes = store.changes()
    assert "ignored" not in [entry["new_path"] for entry in changes["entries"]]
    renamed = sources(store, name="renamed.txt")
    assert renamed["entry"]["status"] == "renamed"
    assert renamed["entry"]["old_path"] == "a.txt"
    assert renamed["notices"] == ["No text changes"]
    for name in names:
        assert sources(store, name=name)["new"]["content"] == name
    (repo / "renamed.txt").unlink()
    deleted = sources(store, name="a.txt")
    assert deleted["new"]["content"] == ""
    assert not deleted["editable"]


def test_metadata_binary_large_symlink_and_modes(repo, monkeypatch):
    store = GitStore(FileStore(repo))
    (repo / "a.txt").write_bytes(b"\xef\xbb\xbforiginal\r\n")
    result = sources(store)
    assert result["new"]["bom"]
    assert result["new"]["line_endings"] == {"crlf": 1, "lf": 0, "cr": 0}
    assert result["new"]["final_newline"]
    assert set(result["notices"]) == {"Line endings differ", "UTF-8 BOM differs"}
    (repo / "a.txt").write_bytes(b"binary\x00")
    assert sources(store)["reason"] == "Text diff is not available for this file"
    (repo / "a.txt").write_bytes(b"\xff")
    assert not sources(store)["editable"]
    monkeypatch.setattr("web_edit.git.TEXT_LIMIT", 4)
    (repo / "a.txt").write_bytes(b"12345")
    assert sources(store)["reason"] == "File is too large for text diff"
    monkeypatch.undo()
    (repo / "a.txt").unlink()
    (repo / "a.txt").symlink_to("/etc/passwd")
    result = sources(store)
    assert not result["editable"]
    assert result["old"] is result["new"] is None
    assert result["reason"] == "Symbolic link"
    (repo / "a.txt").unlink()
    (repo / "a.txt").write_text("original\n")
    (repo / "a.txt").chmod(0o755)
    result = sources(store)
    assert result["entry"]["old_mode"] == "100644"
    assert result["entry"]["new_mode"] == "100755"
    assert result["notices"] == ["No text changes"]


def test_snapshot_stale_and_inflight_changes(repo, monkeypatch):
    (repo / "a.txt").write_text("first\n")
    store = GitStore(FileStore(repo))
    listing = store.changes()
    entry = listing["entries"][0]["id"]
    (repo / "a.txt").write_text("second\n")
    with pytest.raises(FileError, match=CHANGED) as error:
        store.file(entry=entry, snapshot=listing["snapshot"])
    assert error.value.status == 409
    listing = store.changes()
    original = store._blob

    def change_during_read(*args):
        (repo / "a.txt").write_text("third\n")
        return original(*args)

    monkeypatch.setattr(store, "_blob", change_during_read)
    with pytest.raises(FileError, match=CHANGED):
        store.file(entry=entry, snapshot=listing["snapshot"])


def test_conflict_has_no_two_source_comparison(repo):
    git(repo, "checkout", "-qb", "side")
    (repo / "a.txt").write_text("side\n")
    commit(repo)
    git(repo, "checkout", "-q", "-")
    (repo / "a.txt").write_text("main\n")
    commit(repo)
    with pytest.raises(subprocess.CalledProcessError):
        git(repo, "merge", "side")
    store = GitStore(FileStore(repo))
    for comparison in ("all", "unstaged", "staged"):
        result = sources(store, comparison=comparison)
        assert result["reason"] == "Unmerged file"
        assert result["old"] is None and not result["editable"]


def test_git_environment_and_external_programs_are_disabled(
    repo, tmp_path, monkeypatch
):
    (repo / "a.txt").write_text("changed\n")
    other = init(tmp_path / "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    marker = repo / "ran"
    script = repo / "external"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    script.chmod(0o755)
    git(repo, "config", "diff.external", str(script))
    git(repo, "config", "core.fsmonitor", str(script))
    git(repo, "config", "diff.test.textconv", str(script))
    git(repo, "config", "filter.test.clean", str(script))
    git(repo, "config", "filter.test.process", str(script))
    git(repo, "config", "filter.test.required", "true")
    (repo / ".gitattributes").write_text("*.txt diff=test filter=test\n")
    result = sources(GitStore(FileStore(repo)), name="a.txt")
    assert result["new"]["content"] == "changed\n"
    assert not marker.exists()


def test_runner_missing_git_output_limit_and_entry_limit(repo, monkeypatch):
    store = GitStore(FileStore(repo))
    with pytest.raises(GitFailure, match="output exceeds"):
        store._run(repo, "rev-parse", "--show-toplevel", limit=1)
    (repo / "a.txt").write_text("changed")
    monkeypatch.setattr("web_edit.git.ENTRY_LIMIT", 0)
    with pytest.raises(FileError, match="Too many changed"):
        store.changes()
    monkeypatch.setenv("PATH", "")
    assert store.context()["reason"] == "Git is not installed"


def test_api_contract_and_save_version(tmp_path, repo):
    (repo / "a.txt").write_bytes(b"\xef\xbb\xbfworking\r\n")
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        context = client.get("/api/git/context", params={"path": "repo"})
        assert context.json()["available"]
        assert context.headers["cache-control"] == "no-store"
        listing = client.get("/api/git/changes", params={"path": "repo"}).json()
        params = {
            "path": "repo",
            "comparison": "all",
            "entry": listing["entries"][0]["id"],
            "snapshot": listing["snapshot"],
        }
        response = client.get("/api/git/file", params=params)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["working_path"] == "repo/a.txt"
        assert (
            result["version"]
            == hashlib.sha256((repo / "a.txt").read_bytes()).hexdigest()
        )
        index = git(repo, "ls-files", "--stage")
        head = git(repo, "rev-parse", "HEAD")
        saved = client.put(
            "/api/text",
            headers={"X-Web-Edit": "1"},
            json={
                "path": result["working_path"],
                "version": result["version"],
                "content": "saved\n",
            },
        )
        assert saved.status_code == 200
        assert (repo / "a.txt").read_bytes() == b"\xef\xbb\xbfsaved\r\n"
        assert git(repo, "ls-files", "--stage") == index
        assert git(repo, "rev-parse", "HEAD") == head
        assert client.get("/api/git/file", params=params).status_code == 409
        params["entry"] = "HEAD:/etc/passwd"
        params["snapshot"] = client.get(
            "/api/git/changes", params={"path": "repo"}
        ).json()["snapshot"]
        assert client.get("/api/git/file", params=params).status_code == 404
        assert client.get("/api/git/changes", params={"path": "../"}).status_code == 403
        assert (
            client.get(
                "/api/git/changes", params={"path": "repo", "comparison": "HEAD"}
            ).status_code
            == 400
        )
        assert (
            client.get("/api/git/context", headers={"Host": "evil.example"}).status_code
            == 400
        )
        assert client.get("/api/browse").status_code == 200
