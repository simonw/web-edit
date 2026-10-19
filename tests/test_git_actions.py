"""Per-file index actions never modify working files or unrelated index entries."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from web_edit.app import create_app
from web_edit.files import FileError, FileStore
from web_edit.git import GitStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(root, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.check_output(
        ["git", "-C", str(root), *args], env=env, stderr=subprocess.PIPE
    )


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Tests")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(f"original {name}\n" * 10)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def store(repo):
    return GitStore(FileStore(repo))


def select(backend, name, comparison="all"):
    changes = backend.changes("", comparison)
    entry = next(e for e in changes["entries"] if name in (e["old_path"], e["new_path"]))
    return dict(path="", comparison=comparison, entry=entry["id"],
                snapshot=changes["snapshot"], action=entry["action"])


def actions(backend, comparison="all"):
    return {e["new_path"] or e["old_path"]: e["action"]
            for e in backend.changes("", comparison)["entries"]}


def test_mixed_all_and_partial_stage(repo):
    backend = store(repo)
    (repo / "a").write_text("staged a\n")
    (repo / "b").write_text("staged b\n")
    git(repo, "add", "a", "b")
    (repo / "a").write_text("partial a\n")
    (repo / "c").write_text("unstaged c\n")
    assert actions(backend) == {"a": "stage", "b": "unstage", "c": "stage"}
    assert actions(backend, "unstaged") == {"a": "stage", "c": "stage"}
    assert actions(backend, "staged") == {"a": "unstage", "b": "unstage"}
    b_index = git(repo, "show", ":b")
    result = backend.action(**select(backend, "a", "unstaged"))
    assert [e["new_path"] for e in result["entries"]] == ["c"]
    assert git(repo, "show", ":a") == b"partial a\n"
    assert git(repo, "show", ":b") == b_index
    result = backend.action(**select(backend, "a", "staged"))
    assert [e["new_path"] for e in result["entries"]] == ["b"]
    assert (repo / "a").read_text() == "partial a\n"
    assert git(repo, "show", ":a") == git(repo, "show", "HEAD:a")


@pytest.mark.parametrize("name", ["a", "new file", "-option", "[literal]*"])
def test_stage_and_unstage_add_modify_delete(repo, name):
    backend = store(repo)
    (repo / name).write_text("new contents\n")
    before = (repo / name).read_bytes()
    result = backend.action(**select(backend, name, "unstaged"))
    assert not result["entries"]
    assert git(repo, "show", f":{name}") == before
    result = backend.action(**select(backend, name, "staged"))
    assert not result["entries"]
    assert (repo / name).read_bytes() == before
    if name == "a":
        (repo / name).unlink()
        backend.action(**select(backend, name))
        assert actions(backend, "staged") == {"a": "unstage"}
        backend.action(**select(backend, name, "staged"))
        assert not (repo / name).exists()
        assert not actions(backend, "staged")


def test_rename_both_paths_and_partial_rename(repo):
    backend = store(repo)
    (repo / "a").rename(repo / "renamed")
    # An unstaged move is represented as deletion plus untracked addition.
    git(repo, "add", "--all", "--", "a", "renamed")
    assert actions(backend) == {"renamed": "unstage"}
    backend.action(**select(backend, "renamed"))
    assert git(repo, "ls-files", "--", "a", "renamed") == b"a\n"
    assert (repo / "renamed").exists() and not (repo / "a").exists()
    git(repo, "add", "--all", "--", "a", "renamed")
    with (repo / "renamed").open("a") as file:
        file.write("partial\n")
    assert actions(backend) == {"renamed": "stage"}
    backend.action(**select(backend, "renamed"))
    assert git(repo, "show", ":renamed").endswith(b"partial\n")
    backend.action(**select(backend, "renamed", "staged"))
    assert git(repo, "ls-files", "--", "a", "renamed") == b"a\n"


def test_linked_worktree_external_metadata_stage_and_unstage(repo):
    linked = repo.with_name(repo.name + "-linked")
    git(repo, "worktree", "add", "-qb", "linked-actions", str(linked))
    main_index = (repo / ".git/index").read_bytes()
    (linked / "a").write_text("linked working bytes\n")
    backend = store(linked)
    assert backend.context()["available"]
    assert backend.changes()["repo_path"] == ""
    metadata = git(linked, "rev-parse", "--path-format=absolute", "--git-path", "index")
    assert not os.fsdecode(metadata).strip().startswith(str(linked) + "/")
    result = backend.action(**select(backend, "a", "unstaged"))
    assert not result["entries"]
    assert git(linked, "show", ":a") == b"linked working bytes\n"
    result = backend.action(**select(backend, "a", "staged"))
    assert not result["entries"]
    assert git(linked, "show", ":a") == git(linked, "show", "HEAD:a")
    assert (linked / "a").read_text() == "linked working bytes\n"
    assert (repo / ".git/index").read_bytes() == main_index
    # The metadata exception does not permit browsing another working tree.
    with pytest.raises(FileError):
        backend.context("../" + repo.name)
    with pytest.raises(FileError):
        backend.store.text("../" + repo.name + "/a")


def test_transient_git_pointer_redirect_cannot_redirect_index(repo, monkeypatch):
    linked = repo.with_name(repo.name + "-bound")
    git(repo, "worktree", "add", "-qb", "bound-metadata", str(linked))
    (linked / "a").write_text("selected linked bytes\n")
    backend = store(linked)
    request = select(backend, "a", "unstaged")
    main_index = (repo / ".git/index").read_bytes()
    original = backend._git_path
    pointer = (linked / ".git").read_bytes()
    attempted = False

    def redirected(root, name):
        nonlocal attempted
        if name != "index":
            return original(root, name)
        attempted = True
        (linked / ".git").write_text(f"gitdir: {repo / '.git'}\n")
        try:
            return original(root, name)
        finally:
            (linked / ".git").write_bytes(pointer)

    monkeypatch.setattr(backend, "_git_path", redirected)
    backend.action(**request)
    assert attempted
    assert (repo / ".git/index").read_bytes() == main_index
    assert git(linked, "show", ":a") == b"selected linked bytes\n"


@pytest.mark.parametrize("target", ["main", "other_worktree"])
def test_forged_external_git_pointer_cannot_change_external_index(repo, target):
    if target == "other_worktree":
        genuine = repo.with_name(repo.name + "-genuine")
        git(repo, "worktree", "add", "-qb", "genuine", str(genuine))
        pointer = (genuine / ".git").read_text()
        raw = git(genuine, "rev-parse", "--path-format=absolute", "--git-path", "index")
        index_path = Path(os.fsdecode(raw).strip())
    else:
        pointer = f"gitdir: {repo / '.git'}\n"
        index_path = repo / ".git/index"
    forged = repo.with_name(repo.name + "-forged")
    forged.mkdir()
    (forged / ".git").write_text(pointer)
    (forged / "a").write_text("untrusted served bytes\n")
    index = index_path.read_bytes()
    backend = store(forged)
    with pytest.raises(FileError) as failure:
        backend.changes()
    assert failure.value.status == 403
    with pytest.raises(FileError) as failure:
        backend.action("", "all", "untrusted", "untrusted", "stage")
    assert failure.value.status == 403
    assert index_path.read_bytes() == index
    assert (forged / "a").read_text() == "untrusted served bytes\n"


def test_unborn(repo):
    empty = repo / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    (empty / "new").write_text("new\n")
    backend = store(empty)
    assert not backend.action(**select(backend, "new", "unstaged"))["entries"]
    assert actions(backend) == {"new": "unstage"}
    assert not backend.action(**select(backend, "new", "staged"))["entries"]
    assert git(empty, "ls-files") == b""
    assert (empty / "new").read_text() == "new\n"


@pytest.mark.parametrize("change", ["disk", "index", "head"])
def test_stale_does_not_mutate_index(repo, change):
    backend = store(repo)
    (repo / "a").write_text("changed\n")
    request = select(backend, "a")
    if change == "disk":
        (repo / "a").write_text("later\n")
    elif change == "index":
        git(repo, "add", "a")
    else:
        git(repo, "commit", "--allow-empty", "-qm", "later")
    index = (repo / ".git/index").read_bytes()
    with pytest.raises(FileError) as error:
        backend.action(**request)
    assert error.value.status == 409
    assert (repo / ".git/index").read_bytes() == index


def test_api_validation_and_csrf(repo):
    (repo / "a").write_text("changed\n")
    client = TestClient(create_app(repo), base_url="http://localhost")
    request = select(client.app.state.git_store, "a")
    assert client.post("/api/git/action", json=request).status_code == 403
    headers = {"X-Web-Edit": "1"}
    for body in ([], {}, {**request, "action": "delete"}, {**request, "entry": "a"},
                 {**request, "path": []}, {**request, "revision": "HEAD"},
                 {**request, "action": "unstage"}):
        assert client.post("/api/git/action", json=body, headers=headers).status_code == 400
    assert client.post("/api/git/action", content=b"x" * 8193, headers=headers).status_code == 413
    assert client.post("/api/git/action", content=b"{", headers=headers).status_code == 400
    assert client.post("/api/git/action", content=b"[" * 1500, headers=headers).status_code == 400
    response = client.post("/api/git/action", json=request, headers=headers)
    assert response.status_code == 200
    assert response.json()["entries"][0]["action"] == "unstage"
    assert client.post("/api/git/action", json=request, headers=headers).status_code == 409


@pytest.mark.parametrize("conversion", ["autocrlf", "attributes", "info_attributes"])
def test_stage_preserves_builtin_line_ending_conversion(repo, conversion):
    if conversion == "autocrlf":
        git(repo, "config", "core.autocrlf", "true")
    elif conversion == "info_attributes":
        (repo / ".git/info/attributes").write_text("a text eol=crlf\n")
    else:
        (repo / ".gitattributes").write_text("a text eol=crlf\n")
        git(repo, "add", ".gitattributes")
        git(repo, "commit", "-qm", "attributes")
    working = b"changed line\r\nsecond line\r\n"
    (repo / "a").write_bytes(working)
    backend = store(repo)
    result = backend.action(**select(backend, "a", "unstaged"))
    assert result["entries"] == []
    assert git(repo, "show", ":a") == b"changed line\nsecond line\n"
    assert (repo / "a").read_bytes() == working


@pytest.mark.parametrize("phase", ["conversion", "validation"])
def test_late_filter_configuration_cannot_execute(repo, monkeypatch, phase):
    (repo / ".gitattributes").write_text("a text eol=crlf\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "attributes")
    (repo / "a").write_bytes(b"changed\r\n")
    backend = store(repo)
    request = select(backend, "a", "unstaged")
    marker = repo / "EXECUTED"
    index = (repo / ".git/index").read_bytes()
    original = subprocess.Popen
    injected = False

    def spawn(args, **kwargs):
        nonlocal injected
        target = (
            "hash-object" in args and "--path=a" in args
            if phase == "conversion" else "diff" in args
        )
        if target and not injected:
            injected = True
            git(repo, "config", "filter.late.clean", f"touch {marker}; cat")
            git(repo, "config", "filter.late.required", "true")
            (repo / ".gitattributes").write_text("a filter=late text eol=crlf\n")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(FileError) as failure:
        backend.action(**request)
    assert failure.value.status == 409
    assert injected
    assert not marker.exists()
    assert (repo / ".git/index").read_bytes() == index
    assert (repo / "a").read_bytes() == b"changed\r\n"


def test_filters_refuse_stage_but_allow_unstage_without_filters_or_hooks(repo):
    (repo / "a").write_text("staged\n")
    git(repo, "add", "a")
    (repo / ".gitattributes").write_text("a filter=unsafe\n")
    marker = repo / "EXECUTED"
    git(repo, "config", "filter.unsafe.clean", f"touch {marker}; cat")
    git(repo, "config", "filter.unsafe.process", f"touch {marker}")
    git(repo, "config", "filter.unsafe.required", "true")
    hook = repo / ".git/hooks/post-index-change"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (repo / "a").write_text("new\n")
    backend = store(repo)
    assert actions(backend)["a"] is None
    assert actions(backend, "unstaged")["a"] is None
    request = select(backend, "a")
    request["action"] = "stage"
    index = (repo / ".git/index").read_bytes()
    with pytest.raises(FileError) as failure:
        backend.action(**request)
    assert failure.value.status == 400
    assert str(failure.value) == "Files with Git filters must be staged outside web-edit."
    assert (repo / ".git/index").read_bytes() == index
    assert actions(backend, "staged")["a"] == "unstage"
    backend.action(**select(backend, "a", "staged"))
    assert (repo / "a").read_text() == "new\n"
    assert not marker.exists()


@pytest.mark.parametrize("action", ["stage", "unstage"])
def test_action_spawn_keeps_repository_pinned(repo, monkeypatch, action):
    (repo / "a").write_text("changed\n")
    if action == "unstage":
        git(repo, "add", "a")
    backend = store(repo)
    request = select(backend, "a")
    external = repo.with_name(repo.name + "-external")
    external.mkdir()
    git(external, "init", "-q")
    (external / "a").write_text("private\n")
    git(external, "add", "a")
    index = (external / ".git/index").read_bytes()
    original = subprocess.Popen
    attempted = False

    def spawn(args, **kwargs):
        nonlocal attempted
        if "update-index" not in args:
            return original(args, **kwargs)
        attempted = True
        parked = repo.with_name(repo.name + "-parked")
        repo.rename(parked)
        repo.symlink_to(external, target_is_directory=True)
        try:
            fd = kwargs["pass_fds"][0]
            assert kwargs["cwd"] in (f"/proc/self/fd/{fd}", f"/dev/fd/{fd}")
            process = original(args, **kwargs)
            process.wait(timeout=3)
            return process
        finally:
            repo.unlink()
            parked.rename(repo)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    backend.action(**request)
    assert attempted
    assert (external / ".git/index").read_bytes() == index
    assert (repo / "a").read_text() == "changed\n"


def test_unstage_directory_file_conflict_preserves_unselected(repo):
    (repo / "a").unlink()
    (repo / "a").mkdir()
    (repo / "a/child").write_text("unselected\n")
    git(repo, "add", "--all", "--", "a")
    backend = store(repo)
    request = select(backend, "a", "staged")
    assert request["action"] == "unstage"
    index = (repo / ".git/index").read_bytes()
    with pytest.raises(FileError) as failure:
        backend.action(**request)
    assert failure.value.status == 409
    assert (repo / ".git/index").read_bytes() == index
    assert git(repo, "show", ":a/child") == b"unselected\n"


def test_stage_directory_swap_at_index_mutation_is_exact(repo, monkeypatch):
    (repo / "a").write_text("selected\n")
    backend = store(repo)
    request = select(backend, "a")
    original = subprocess.Popen
    swapped = False
    before = git(repo, "ls-files", "--stage", "--", "b", "c")

    def spawn(args, **kwargs):
        nonlocal swapped
        if "update-index" in args:
            (repo / "a").unlink()
            (repo / "a").mkdir()
            (repo / "a/unselected").write_text("unselected\n")
            swapped = True
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    backend.action(**request)
    assert swapped
    assert git(repo, "show", ":a") == b"selected\n"
    assert git(repo, "ls-files", "--", "a/unselected") == b""
    assert git(repo, "ls-files", "--stage", "--", "b", "c") == before
    assert (repo / "a/unselected").read_text() == "unselected\n"


def test_index_lock_blocks_concurrent_descendant_stage(repo, monkeypatch):
    (repo / "a").write_text("selected\n")
    backend = store(repo)
    request = select(backend, "a")
    original = subprocess.Popen
    attempted = False

    def spawn(args, **kwargs):
        nonlocal attempted
        if "update-index" in args:
            attempted = True
            (repo / "a").unlink()
            (repo / "a").mkdir()
            (repo / "a/child").write_text("concurrent\n")
            assert (repo / ".git/index.lock").exists()
            with pytest.raises(subprocess.CalledProcessError):
                git(repo, "add", "--all", "--", "a")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    backend.action(**request)
    assert attempted
    assert git(repo, "show", ":a") == b"selected\n"
    assert git(repo, "ls-files", "--", "a/child") == b""
    assert not (repo / ".git/index.lock").exists()
    assert not list((repo / ".git").glob(".web-edit-index-*"))


def test_index_sync_failure_preserves_index_and_cleans_lock(repo, monkeypatch):
    (repo / "a").write_text("selected\n")
    backend = store(repo)
    request = select(backend, "a")
    index = (repo / ".git/index").read_bytes()

    def fail_sync(fd):
        raise OSError("simulated sync failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="simulated sync failure"):
        backend.action(**request)
    assert (repo / ".git/index").read_bytes() == index
    assert not (repo / ".git/index.lock").exists()
    assert not list((repo / ".git").glob(".web-edit-index-*"))


def test_existing_index_lock_is_preserved(repo):
    (repo / "a").write_text("selected\n")
    backend = store(repo)
    request = select(backend, "a")
    index = (repo / ".git/index").read_bytes()
    lock = repo / ".git/index.lock"
    lock.write_bytes(b"another writer")
    with pytest.raises(FileError) as failure:
        backend.action(**request)
    assert failure.value.status == 409
    assert lock.read_bytes() == b"another writer"
    assert (repo / ".git/index").read_bytes() == index


def test_stage_large_file_fails_without_index_mutation(repo):
    from web_edit.files import TEXT_LIMIT

    (repo / "a").write_bytes(b"x" * (TEXT_LIMIT + 1))
    backend = store(repo)
    request = select(backend, "a")
    index = (repo / ".git/index").read_bytes()
    with pytest.raises(FileError) as failure:
        backend.action(**request)
    assert failure.value.status == 413
    assert (repo / ".git/index").read_bytes() == index


def test_stage_large_stdin_completes_without_pipe_deadlock(repo):
    data = b"large input\n" * 100000
    (repo / "a").write_bytes(data)
    backend = store(repo)
    backend.action(**select(backend, "a"))
    assert git(repo, "show", ":a") == data


def test_conflicts_and_symlinks_have_no_action(repo):
    git(repo, "checkout", "-qb", "other")
    (repo / "a").write_text("other\n")
    git(repo, "commit", "-qam", "other")
    git(repo, "checkout", "-q", "-")
    (repo / "a").write_text("main\n")
    git(repo, "commit", "-qam", "main")
    with pytest.raises(subprocess.CalledProcessError):
        git(repo, "merge", "other")
    (repo / "link").symlink_to("b")
    backend = store(repo)
    for comparison in ("all", "unstaged", "staged"):
        assert actions(backend, comparison)["a"] is None
    assert actions(backend)["link"] is None
    request = select(backend, "a")
    request["action"] = "stage"
    index = (repo / ".git/index").read_bytes()
    with pytest.raises(FileError):
        backend.action(**request)
    assert (repo / ".git/index").read_bytes() == index
