"""Unsupported FD platforms must not disable ordinary filesystem routes."""

import os
import shutil

import pytest
from starlette.testclient import TestClient

from web_edit.app import create_app
from web_edit.files import FileStore
from web_edit.git import UNSUPPORTED_PLATFORM, GitFailure, GitStore


def test_missing_no_follow_support_only_disables_diff(tmp_path, monkeypatch):
    (tmp_path / "text").write_text("ordinary files still work")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        context = client.get("/api/git/context")
        assert context.status_code == 200
        assert context.json() == {
            "available": False,
            "reason": UNSUPPORTED_PLATFORM,
            "repo_path": None,
        }
        assert client.get("/api/browse").status_code == 200
        assert (
            client.get("/api/text", params={"path": "text"}).json()["content"]
            == "ordinary files still work"
        )


@pytest.mark.skipif(
    os.name != "posix" or shutil.which("git") is None, reason="requires POSIX and Git"
)
def test_dev_fd_fallback_runs_git(tmp_path, monkeypatch):
    store = GitStore(FileStore(tmp_path))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original = os.stat

    def without_proc(path, *args, **kwargs):
        if str(path).startswith("/proc/self/fd/"):
            raise FileNotFoundError()
        return original(path, *args, **kwargs)

    try:
        try:
            original(f"/dev/fd/{fd}")
        except OSError:
            pytest.skip("/dev/fd is not mounted")
        monkeypatch.setattr(os, "stat", without_proc)
        assert store._fd_cwd(fd) == f"/dev/fd/{fd}"
        assert store._execute(fd, "--version").startswith(b"git version ")
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name != "posix", reason="requires directory descriptors")
def test_no_fd_namespace_has_explicit_disabled_reason(tmp_path, monkeypatch):
    store = GitStore(FileStore(tmp_path))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original = os.stat

    def without_fd_paths(path, *args, **kwargs):
        if str(path).startswith(("/proc/self/fd/", "/dev/fd/")):
            raise FileNotFoundError()
        return original(path, *args, **kwargs)

    try:
        monkeypatch.setattr(os, "stat", without_fd_paths)
        with pytest.raises(GitFailure, match=UNSUPPORTED_PLATFORM):
            store._fd_cwd(fd)
    finally:
        os.close(fd)
