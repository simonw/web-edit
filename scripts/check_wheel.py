"""Install a built wheel away from the checkout and exercise its packaged app.

Usage: uv run scripts/check_wheel.py dist/*.whl
"""

import argparse
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("wheel", type=Path, help="Path to the single wheel to verify")
wheel = parser.parse_args().wheel.resolve()
print(f"Verifying {wheel.name}", flush=True)
required = {
    "web_edit/static/index.html",
    "web_edit/static/app.css",
    "web_edit/static/app.js",
    "web_edit/static/THIRD_PARTY_LICENSES.txt",
}
with zipfile.ZipFile(wheel) as archive:
    assert required <= set(archive.namelist()), "Missing frontend assets in wheel"
    assert b"cm-editor" in archive.read("web_edit/static/app.js"), (
        "CodeMirror missing from bundle"
    )
    licenses = archive.read("web_edit/static/THIRD_PARTY_LICENSES.txt")
    assert b"@codemirror/view" in licenses
    assert b"@codemirror/merge" in licenses, "Merge dependency missing from wheel"
    assert b"cm-mergeView" in archive.read("web_edit/static/app.js"), (
        "Merge view missing from bundle"
    )

with tempfile.TemporaryDirectory(prefix="web-edit-wheel-") as temporary:
    directory = Path(temporary)
    environment = directory / ".venv"
    subprocess.run(["uv", "venv", str(environment)], check=True)
    python = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), f"{wheel}[test]"], check=True
    )
    # -I removes both the checkout and PYTHONPATH from the import search path.
    subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--python",
            str(python),
            "python",
            "-I",
            "-c",
            r"""
from importlib.resources import files
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from starlette.testclient import TestClient
from click.testing import CliRunner
import web_edit
from web_edit.app import create_app
from web_edit.cli import cli

assert "site-packages" in web_edit.__file__, web_edit.__file__
assert files("web_edit").joinpath("static/app.js").is_file()
with TemporaryDirectory() as folder, TestClient(create_app(folder), base_url="http://localhost") as client:
    assert client.get("/").status_code == 200
    assert "cm-editor" in client.get("/static/app.js").text
    assert client.get("/static/app.css").status_code == 200
    assert "@codemirror/merge" in client.get("/static/THIRD_PARTY_LICENSES.txt").text
    assert client.get("/api/git/context").status_code == 200
    assert client.get("/api/git/context").json()["available"] is False
    assert client.get("/api/browse", headers={"Host": "untrusted.example"}).status_code == 400
    assert client.put("/api/text", headers={"Host": "untrusted.example", "X-Web-Edit": "1"}, json={"path": "blocked.txt", "content": "blocked"}).status_code == 400
    assert not Path(folder, "blocked.txt").exists()
    created = client.put("/api/text", headers={"X-Web-Edit": "1"}, json={"path": "nested/test.txt", "content": "wheel works"})
    assert created.status_code == 200, created.text
    updated = client.put("/api/text", headers={"X-Web-Edit": "1"}, json={"path": "nested/test.txt", "content": "saved again", "version": created.json()["version"]})
    assert updated.status_code == 200, updated.text
    assert client.get("/api/download", params={"path": "nested/test.txt"}).content == b"saved again"
    uploaded = client.post("/api/upload", headers={"X-Web-Edit": "1"}, data={"path": "upload.bin"}, files={"file": ("original.bin", b"\x00\xff")})
    assert uploaded.status_code == 201, uploaded.text
    assert Path(folder, "upload.bin").read_bytes() == b"\x00\xff"
if shutil.which("git"):
    with TemporaryDirectory() as folder:
        repository = Path(folder, "project")
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        Path(repository, "new.txt").write_text("wheel diff works\n")
        with TestClient(create_app(folder), base_url="http://localhost") as client:
            assert client.get("/api/git/context").json()["available"] is False
            assert client.get("/api/git/context", params={"path": "project"}).json()["available"] is True
            changes = client.get("/api/git/changes", params={"path": "project"}).json()
            pair = client.get("/api/git/file", params={"path": "project", "entry": changes["entries"][0]["id"], "snapshot": changes["snapshot"]})
            assert pair.status_code == 200, pair.text
            assert pair.json()["new"]["content"] == "wheel diff works\n"
            assert pair.json()["editable"] is True
            unstaged = client.get("/api/git/changes", params={"path": "project", "comparison": "unstaged"}).json()
            staged = client.post("/api/git/action", headers={"X-Web-Edit": "1"}, json={"path": "project", "comparison": "unstaged", "entry": unstaged["entries"][0]["id"], "snapshot": unstaged["snapshot"], "action": "stage"})
            assert staged.status_code == 200, staged.text
            assert client.get("/api/git/changes", params={"path": "project", "comparison": "unstaged"}).json()["entries"] == []
            staged_list = client.get("/api/git/changes", params={"path": "project", "comparison": "staged"}).json()
            unstaged_result = client.post("/api/git/action", headers={"X-Web-Edit": "1"}, json={"path": "project", "comparison": "staged", "entry": staged_list["entries"][0]["id"], "snapshot": staged_list["snapshot"], "action": "unstage"})
            assert unstaged_result.status_code == 200, unstaged_result.text
            assert Path(repository, "new.txt").read_text() == "wheel diff works\n"
assert CliRunner().invoke(cli, ["--help"]).exit_code == 0
print("Wheel verified: isolated installation, bundled CodeMirror, assets, CLI, file operations, and Git actions when Git is available.")
""",
        ],
        cwd=directory,
        check=True,
    )
