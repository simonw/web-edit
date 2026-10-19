"""Additional Git limit and special-state coverage."""

import shutil
import subprocess
import time

import pytest
from test_git import commit, git, init, sources

from web_edit.app import create_app
from web_edit.files import FileError, FileStore
from web_edit.git import CHANGED, GitFailure, GitStore

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def test_timeout_kills_git(tmp_path, monkeypatch):
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\nexec /bin/sleep 10\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("web_edit.git.GIT_TIMEOUT", 0.03)
    store = GitStore(FileStore(tmp_path))
    start = time.monotonic()
    with pytest.raises(GitFailure, match="timed out"):
        store._run(tmp_path, "rev-parse")
    assert time.monotonic() - start < 2


def test_startup_does_not_invoke_git(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Git ran during startup")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    create_app(tmp_path)


def test_sha256_unborn_repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    try:
        git(root, "init", "-q", "--object-format=sha256")
    except subprocess.CalledProcessError:
        pytest.skip("Git lacks SHA-256 repositories")
    (root / "new").write_text("contents")
    git(root, "add", "new")
    store = GitStore(FileStore(root))
    for comparison in ("all", "staged"):
        result = sources(store, comparison=comparison)
        assert result["old"]["content"] == ""
        assert result["new"]["content"] == "contents"
        assert len(result["old_oid"]) == 64


def test_untracked_links_and_nested_repository_metadata(tmp_path):
    root = init(tmp_path / "root")
    nested = init(root / "nested")
    (nested / "secret").write_text("do not descend")
    (root / "link").symlink_to("/etc/passwd")
    store = GitStore(FileStore(root))
    result = sources(store, name="link")
    assert result["reason"] == "Symbolic link"
    assert not result["editable"] and result["new"] is None
    result = sources(store, name="nested/")
    assert not result["editable"]
    assert result["reason"] == "Text diff is not available for this file"
    assert "secret" not in str(store.changes()["entries"])


def test_sparse_unavailable_file(tmp_path):
    root = init(tmp_path / "root")
    (root / "file").write_text("original")
    commit(root)
    (root / "file").write_text("staged")
    git(root, "add", "file")
    git(root, "update-index", "--skip-worktree", "file")
    (root / "file").unlink()
    store = GitStore(FileStore(root))
    result = sources(store)
    assert result["reason"] == "File content is not available on disk"
    assert not result["editable"]
    result = sources(store, comparison="staged")
    assert result["new"]["content"] == "staged"


def test_submodule_identifiers(tmp_path):
    root = init(tmp_path / "root")
    child = init(tmp_path / "child")
    (child / "file").write_text("first")
    commit(child)
    git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(child),
        "sub",
    )
    commit(root)
    first = git(child, "rev-parse", "HEAD").decode().strip()
    (child / "file").write_text("second")
    commit(child)
    second = git(child, "rev-parse", "HEAD").decode().strip()
    git(root / "sub", "fetch", "-q")
    git(root / "sub", "checkout", "-q", second)
    store = GitStore(FileStore(root))
    result = sources(store)
    assert result["reason"] == "Submodule"
    assert result["old_oid"] == first
    assert result["new_oid"] == second
    assert result["old"] is None and result["new"] is None
    assert not result["editable"]


def test_list_is_lazy_and_index_changes_invalidate_snapshot(tmp_path, monkeypatch):
    root = init(tmp_path / "root")
    (root / "a").write_text("original")
    commit(root)
    (root / "a").write_text("disk")
    store = GitStore(FileStore(root))
    original = store._disk

    def metadata_only(*args, **kwargs):
        assert not kwargs.get("content")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_disk", metadata_only)
    listing = store.changes()
    monkeypatch.setattr(store, "_disk", original)
    git(root, "add", "a")
    with pytest.raises(FileError, match=CHANGED):
        store.file(entry=listing["entries"][0]["id"], snapshot=listing["snapshot"])


def test_bare_and_discovery_failure(tmp_path, monkeypatch):
    root = tmp_path / "bare"
    root.mkdir()
    git(root, "init", "-q", "--bare")
    store = GitStore(FileStore(tmp_path))
    assert store.context("bare")["reason"] == "Not in a Git repository"

    def unsafe(*args, **kwargs):
        raise GitFailure("Cannot read this repository")

    monkeypatch.setattr(store, "_run", unsafe)
    assert store.context("bare") == {
        "available": False,
        "reason": "Cannot read this repository",
        "repo_path": None,
    }


def test_staged_blob_size_and_missing_final_newline(tmp_path, monkeypatch):
    root = init(tmp_path / "root")
    (root / "a").write_text("original\n")
    commit(root)
    (root / "a").write_text("updated")
    git(root, "add", "a")
    store = GitStore(FileStore(root))
    result = sources(store, comparison="staged")
    assert result["old"]["final_newline"]
    assert not result["new"]["final_newline"]
    monkeypatch.setattr("web_edit.git.TEXT_LIMIT", 4)
    original = store._run

    def no_blob(*args, **kwargs):
        assert "blob" not in args
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_run", no_blob)
    result = sources(store, comparison="staged")
    assert result["reason"] == "File is too large for text diff"
    assert result["old"]["byte_size"] == 9
    assert result["new"]["byte_size"] == 7


def test_mixed_line_ending_notice_with_equal_counts(tmp_path):
    root = init(tmp_path / "root")
    (root / "a").write_bytes(b"first\r\nsecond\n")
    commit(root)
    (root / "a").write_bytes(b"first\nsecond\r\n")
    result = sources(GitStore(FileStore(root)))
    assert result["old"]["line_endings"] == result["new"]["line_endings"]
    assert result["notices"] == ["Line endings differ"]
