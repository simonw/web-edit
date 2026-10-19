"""Repository path replacements must never redirect a validated Git read."""

import os
import shutil
import subprocess

import pytest
from test_git import commit, git, init

from web_edit.files import FileError, FileStore
from web_edit.git import CHANGED, GitStore

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


@pytest.fixture
def repositories(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    permitted = init(workspace / "repo")
    (permitted / "public").write_text("public baseline\n")
    commit(permitted)
    (permitted / "public").write_text("public staged\n")
    git(permitted, "add", "public")
    external = init(tmp_path / "external")
    (external / "secret").write_text("private baseline\n")
    commit(external)
    (external / "secret").write_text("private staged\n")
    git(external, "add", "secret")
    return workspace, permitted, external


def swap(permitted, external):
    parked = permitted.with_name("parked")
    permitted.rename(parked)
    permitted.symlink_to(external, target_is_directory=True)
    return parked


def restore(permitted, parked):
    permitted.unlink()
    parked.rename(permitted)


@pytest.mark.parametrize("phase", ["state", "blob"])
def test_replacement_after_discovery_is_rejected(repositories, monkeypatch, phase):
    workspace, permitted, external = repositories
    store = GitStore(FileStore(workspace))
    listing = store.changes("repo", "staged")
    method = "_state" if phase == "state" else "_blob"
    original = getattr(store, method)
    parked = None

    def replaced(*args, **kwargs):
        nonlocal parked
        if parked is None:
            parked = swap(permitted, external)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, method, replaced)
    try:
        with pytest.raises(FileError, match=CHANGED) as failure:
            if phase == "state":
                store.changes("repo", "staged")
            else:
                store.file(
                    "repo", "staged", listing["entries"][0]["id"], listing["snapshot"]
                )
        assert failure.value.status == 409
    finally:
        if parked is not None:
            restore(permitted, parked)
    # Failed requests release their descriptor context and do not poison refresh.
    assert store.changes("repo", "staged")["entries"][0]["new_path"] == "public"


@pytest.mark.parametrize("command", ["rev-parse", "ls-files", "cat-file"])
def test_swap_at_spawn_uses_pinned_directory(repositories, monkeypatch, command):
    workspace, permitted, external = repositories
    store = GitStore(FileStore(workspace))
    listing = store.changes("repo", "staged")
    original = subprocess.Popen
    attempted = False
    identity = permitted.stat()

    def spawn(args, **kwargs):
        nonlocal attempted
        if command not in args or attempted:
            return original(args, **kwargs)
        attempted = True
        parked = swap(permitted, external)
        try:
            # This is after validation but before the child starts. A plain -C
            # pathname would now expose the external repository's index/blobs.
            (fd,) = kwargs["pass_fds"]
            assert kwargs["cwd"] in (f"/proc/self/fd/{fd}", f"/dev/fd/{fd}")
            info = os.fstat(fd)
            assert (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)
            process = original(args, **kwargs)
            process.wait(timeout=3)
            return process
        finally:
            # Restore before post-read validation: safety must come from the
            # descriptor, not merely noticing the temporary symlink afterwards.
            restore(permitted, parked)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    if command == "rev-parse":
        assert store.context("repo") == {
            "available": True,
            "reason": None,
            "repo_path": "repo",
        }
    elif command == "ls-files":
        result = store.changes("repo", "staged")
        assert [entry["new_path"] for entry in result["entries"]] == ["public"]
        assert "secret" not in str(result)
    else:
        result = store.file(
            "repo", "staged", listing["entries"][0]["id"], listing["snapshot"]
        )
        assert result["old"]["content"] == "public baseline\n"
        assert result["new"]["content"] == "public staged\n"
        assert "private" not in str(result)
    assert attempted


def test_directory_replacement_without_symlink_changes_identity(
    repositories, monkeypatch
):
    workspace, permitted, external = repositories
    store = GitStore(FileStore(workspace))
    original = store._state
    parked = permitted.with_name("parked")

    def replaced(*args, **kwargs):
        permitted.rename(parked)
        external.rename(permitted)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_state", replaced)
    try:
        with pytest.raises(FileError, match=CHANGED) as failure:
            store.changes("repo", "staged")
        assert failure.value.status == 409
    finally:
        permitted.rename(external)
        parked.rename(permitted)


def test_ancestor_symlink_replacement_is_rejected(repositories, monkeypatch):
    workspace, _permitted, external = repositories
    store = GitStore(FileStore(workspace))
    original = store._state
    parked = workspace.with_name("parked-workspace")

    def replaced(*args, **kwargs):
        workspace.rename(parked)
        workspace.symlink_to(external.parent, target_is_directory=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_state", replaced)
    try:
        with pytest.raises(FileError, match=CHANGED):
            store.changes("repo", "staged")
    finally:
        workspace.unlink()
        parked.rename(workspace)
