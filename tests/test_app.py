import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from starlette.testclient import TestClient

from web_edit.app import create_app
from web_edit.files import (
    IMAGE_LIMIT,
    TEXT_LIMIT,
    FileError,
    FileStore,
    preserve_endings,
)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        yield client


def save(client, path, content, version=None):
    return client.put(
        "/api/text",
        headers={"X-Web-Edit": "1"},
        json={"path": path, "content": content, "version": version},
    )


def upload(client, path, data=b"upload", version=None):
    fields = {"path": path}
    if version is not None:
        fields["version"] = version
    return client.post(
        "/api/upload",
        headers={"X-Web-Edit": "1"},
        data=fields,
        files={"file": ("original.txt", data)},
    )


def test_browse(client, tmp_path):
    (tmp_path / "z-folder").mkdir()
    (tmp_path / ".hidden").write_text("hidden")
    (tmp_path / "Alpha.txt").write_text("hello")
    result = client.get("/api/browse").json()
    assert result["path"] == ""
    assert [entry["name"] for entry in result["entries"]] == [
        "z-folder",
        ".hidden",
        "Alpha.txt",
    ]
    assert (
        client.get("/api/browse", params={"path": "z-folder"}).json()["entries"] == []
    )
    assert client.get("/api/browse", params={"path": "Alpha.txt"}).status_code == 400
    assert client.get("/api/browse", params={"path": "missing"}).status_code == 404


def test_browse_nested_paths(client, tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c.txt").write_text("c")
    root = client.get("/api/browse").json()
    assert [entry["path"] for entry in root["entries"]] == ["a"]
    nested = client.get("/api/browse", params={"path": "a//b/"}).json()
    assert nested["path"] == "a/b"
    assert [entry["path"] for entry in nested["entries"]] == ["a/b/c.txt"]


def test_create_read_edit_download(client, tmp_path):
    path = "nested/a & #? café.txt"
    created = save(client, path, "Hello 👋\n")
    assert created.status_code == 200
    text = client.get("/api/text", params={"path": path}).json()
    assert text["content"] == "Hello 👋\n"
    changed = save(client, path, "Updated\n", text["version"])
    assert changed.status_code == 200
    assert changed.json()["version"] != text["version"]
    reread = client.get("/api/text", params={"path": path}).json()
    assert reread["version"] == changed.json()["version"]
    downloaded = client.get("/api/download", params={"path": path})
    assert downloaded.content == b"Updated\n"
    assert "attachment" in downloaded.headers["content-disposition"]
    assert (tmp_path / path).read_text() == "Updated\n"


def test_create_then_repeated_save_uses_returned_version(client):
    initial = save(client, "new.txt", "first").json()
    second = save(client, "new.txt", "second", initial["version"])
    assert second.status_code == 200
    assert save(client, "new.txt", "third", second.json()["version"]).status_code == 200


def test_invalid_unicode(client):
    for body in [
        {"path": "bad.txt", "content": "\ud800"},
        {"path": "\ud800.txt", "content": "content"},
    ]:
        assert (
            client.put(
                "/api/text", headers={"X-Web-Edit": "1"}, content=json.dumps(body)
            ).status_code
            == 400
        )


@pytest.mark.parametrize(
    "original,edited,expected",
    [
        (b"one\r\ntwo\r\n", "one\nchanged\n", b"one\r\nchanged\r\n"),
        (b"one\rtwo\r", "one\nchanged\n", b"one\rchanged\r"),
        (b"one\ntwo\n", "one\nchanged\n", b"one\nchanged\n"),
        (b"one\ntwo\n", "one\r\nchanged\r", b"one\nchanged\n"),
        (b"one\r\ntwo\nthree\r", "one\ntwo\nthree\n", b"one\r\ntwo\nthree\r"),
        (b"a\r\n\r\nb\n\nc\r\n", "a\n\nb\n\nnew\nc\n", b"a\r\n\r\nb\n\nnew\r\nc\r\n"),
        (b"\xef\xbb\xbfone\r\n", "changed\n", b"\xef\xbb\xbfchanged\r\n"),
        (b"one", "two", b"two"),
        (b"", "new\n", b"new\n"),
    ],
)
def test_preserves_line_endings_and_bom(client, tmp_path, original, edited, expected):
    path = tmp_path / "text.txt"
    path.write_bytes(original)
    read = client.get("/api/text", params={"path": path.name}).json()
    assert save(client, path.name, edited, read["version"]).status_code == 200
    assert path.read_bytes() == expected


def test_preserves_mixed_endings_with_scattered_edits():
    lines = [(f"line {i}", "\r\n" if i % 3 else "\n") for i in range(3000)]
    original = "".join(line + ending for line, ending in lines)
    for i in range(0, len(lines), 7):
        lines[i] = (f"changed {i}", "\r\n")
    lines.insert(1500, ("inserted", "\r\n"))
    del lines[10]
    edited = "".join(line + "\n" for line, _ in lines)
    expected = "".join(line + ending for line, ending in lines)
    assert preserve_endings(original, edited) == expected


def test_preserves_permissions(client, tmp_path):
    path = tmp_path / "script.sh"
    path.write_text("echo before\n")
    path.chmod(0o755)
    read = client.get("/api/text", params={"path": path.name}).json()
    assert save(client, path.name, "echo after\n", read["version"]).status_code == 200
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_external_changes_and_deletion_never_overwritten(client, tmp_path):
    created = save(client, "file.txt", "original").json()
    path = tmp_path / "file.txt"
    path.write_text("external")
    assert save(client, path.name, "my changes", created["version"]).status_code == 409
    assert path.read_text() == "external"
    path.unlink()
    assert save(client, path.name, "my changes", created["version"]).status_code == 409
    assert not path.exists()


def test_metadata_changes_do_not_cause_false_conflicts(client, tmp_path):
    created = save(client, "file.txt", "original").json()
    (tmp_path / "file.txt").chmod(0o644)
    os.utime(tmp_path / "file.txt", (100, 100))
    assert save(client, "file.txt", "edited", created["version"]).status_code == 200


def test_create_collision_and_upload_confirmation(client, tmp_path):
    assert save(client, "exists.txt", "keep").status_code == 200
    assert save(client, "exists.txt", "clobber").status_code == 409
    conflict = upload(client, "exists.txt")
    assert conflict.status_code == 409
    assert (tmp_path / "exists.txt").read_text() == "keep"
    assert (
        upload(
            client, "exists.txt", b"replaced", conflict.json()["version"]
        ).status_code
        == 201
    )
    assert (tmp_path / "exists.txt").read_bytes() == b"replaced"
    assert (
        upload(client, "exists.txt", b"stale", conflict.json()["version"]).status_code
        == 409
    )


def test_upload_spooled_file_custom_path_and_binary(client, tmp_path):
    data = b"\x00\xff" * (1024 * 1024)
    response = upload(client, "new/folder/custom.bin", data)
    assert response.status_code == 201
    assert response.json()["version"] == hashlib.sha256(data).hexdigest()
    assert (tmp_path / "new/folder/custom.bin").read_bytes() == data
    assert (
        client.get("/api/download", params={"path": "new/folder/custom.bin"}).content
        == data
    )
    assert not list(tmp_path.rglob(".web-edit-*"))


@pytest.mark.parametrize(
    "path", ["../outside", "/tmp/outside", "folder/../../outside", "a\\b", "a\x00b"]
)
def test_path_confinement(client, path):
    for endpoint in ["browse", "text", "download", "image"]:
        assert client.get(f"/api/{endpoint}", params={"path": path}).status_code in (
            400,
            403,
        )
    assert save(client, path, "bad").status_code in (400, 403)
    assert upload(client, path).status_code in (400, 403)


def test_symlinks_are_not_followed(client, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    (tmp_path / "broken").symlink_to(outside / "missing")
    for name in ["link/private.txt", "broken"]:
        for endpoint in ["text", "download", "image"]:
            assert (
                client.get(f"/api/{endpoint}", params={"path": name}).status_code == 403
            )
        assert save(client, name, "bad").status_code == 403
        assert upload(client, name).status_code == 403
    assert client.get("/api/browse", params={"path": "link"}).status_code == 403
    assert client.get("/api/browse").json()["entries"][0]["kind"] == "symlink"
    assert (outside / "private.txt").read_text() == "private"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Requires POSIX")
def test_special_files_not_opened(client, tmp_path):
    os.mkfifo(tmp_path / "pipe")
    assert client.get("/api/text", params={"path": "pipe"}).status_code == 400
    assert client.get("/api/download", params={"path": "pipe"}).status_code == 400
    assert upload(client, "pipe").status_code == 409


@pytest.mark.parametrize("data", [b"\xff", b"\x00", b"a\x01b"])
def test_non_text_download_only(client, tmp_path, data):
    (tmp_path / "binary").write_bytes(data)
    assert client.get("/api/text", params={"path": "binary"}).status_code == 415
    assert client.get("/api/download", params={"path": "binary"}).content == data


def test_large_text(client, tmp_path):
    (tmp_path / "large.txt").write_bytes(b"x" * (TEXT_LIMIT + 1))
    assert client.get("/api/text", params={"path": "large.txt"}).status_code == 413
    assert save(client, "large-new.txt", "x" * (TEXT_LIMIT + 1)).status_code == 413


@pytest.mark.parametrize(
    "length,preview",
    [
        (0, True),
        (IMAGE_LIMIT - 1, True),
        (IMAGE_LIMIT, False),
        (IMAGE_LIMIT + 1, False),
    ],
)
def test_image_threshold(client, tmp_path, length, preview):
    (tmp_path / "picture.png").write_bytes(b"x" * length)
    assert client.get("/api/browse").json()["entries"][0]["preview"] is preview
    response = client.get("/api/image", params={"path": "picture.png"})
    assert response.status_code == (200 if preview else 415)
    if preview:
        assert response.headers["content-type"] == "image/png"
        assert "sandbox" in response.headers["content-security-policy"]


def test_html_not_served_as_active_content(client, tmp_path):
    (tmp_path / "page.html").write_text("<script>alert(1)</script>")
    assert client.get("/api/image", params={"path": "page.html"}).status_code == 415
    response = client.get("/api/download", params={"path": "page.html"})
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_write_header_required(client):
    assert (
        client.put("/api/text", json={"path": "file", "content": "x"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/upload", files={"file": ("file", b"x")}, data={"path": "file"}
        ).status_code
        == 403
    )
    response = client.options(
        "/api/upload",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Web-Edit",
        },
    )
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"path": "file", "content": 1},
        {"path": 1, "content": "x"},
        {"path": "", "content": "x"},
        {"path": "file", "content": "x", "version": []},
    ],
)
def test_bad_input(client, body):
    assert (
        client.put("/api/text", headers={"X-Web-Edit": "1"}, json=body).status_code
        == 400
    )


def test_malformed_json_and_form(client):
    assert (
        client.put("/api/text", headers={"X-Web-Edit": "1"}, content=b"{").status_code
        == 400
    )
    assert (
        client.post(
            "/api/upload", headers={"X-Web-Edit": "1"}, data={"path": "foo"}
        ).status_code
        == 400
    )


def test_concurrent_create_never_clobbers(tmp_path):
    store = FileStore(tmp_path)

    def create(value):
        try:
            store.write("race.txt", BytesIO(value))
            return value
        except FileError as error:
            assert error.status == 409

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, [b"first", b"second"]))
    winners = [value for value in results if value]
    assert len(winners) == 1
    assert (tmp_path / "race.txt").read_bytes() == winners[0]


def test_failed_write_cleans_up_and_preserves_original(tmp_path):
    store = FileStore(tmp_path)
    initial = store.save_text("file", "original", None)

    class BrokenStream:
        def read(self, size):
            raise OSError("simulated disk or stream failure")

    with pytest.raises(OSError):
        store.write("file", BrokenStream(), initial["version"])
    assert (tmp_path / "file").read_text() == "original"
    assert not list(tmp_path.glob(".web-edit-*"))


def test_frontend_assets(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "./static/app.js" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    bundle = client.get("/static/app.js")
    assert bundle.status_code == 200
    assert "cm-editor" in bundle.text
    assert client.get("/static/app.css").status_code == 200
    assert "@codemirror/view" in client.get("/static/THIRD_PARTY_LICENSES.txt").text


def delete_entry(client, path, **overrides):
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    entry = next(
        item
        for item in client.get("/api/browse", params={"path": parent}).json()["entries"]
        if item["path"] == path
    )
    params = {
        "path": path,
        "kind": entry["kind"],
        "version": entry["delete_version"],
        **overrides,
    }
    return client.delete("/api/file", headers={"X-Web-Edit": "1"}, params=params)


def test_delete_file(client, tmp_path):
    path = "a & #? café.txt"
    (tmp_path / path).write_text("delete this")
    (tmp_path / "keep.txt").write_text("keep")
    response = delete_entry(client, path)
    assert response.status_code == 200
    assert response.json() == {"path": path, "deleted": True}
    assert not (tmp_path / path).exists()
    assert (tmp_path / "keep.txt").read_text() == "keep"
    assert client.get("/api/download", params={"path": path}).status_code == 404


@pytest.mark.parametrize("populated", [False, True])
def test_delete_folder(client, tmp_path, populated):
    folder = tmp_path / "folder"
    folder.mkdir()
    if populated:
        (folder / "nested").mkdir()
        (folder / "nested" / "file.txt").write_text("inside")
        (folder / ".hidden").write_text("inside")
    assert delete_entry(client, "folder").status_code == 200
    assert not folder.exists()
    assert tmp_path.is_dir()


@pytest.mark.parametrize(
    "path", ["", ".", "./", "../outside", "/tmp/outside", "folder/../../outside"]
)
def test_delete_cannot_remove_root_or_escape(client, tmp_path, path):
    (tmp_path / "keep.txt").write_text("keep")
    response = client.delete(
        "/api/file",
        headers={"X-Web-Edit": "1"},
        params={"path": path, "kind": "directory", "version": "anything"},
    )
    assert response.status_code == 403
    assert (tmp_path / "keep.txt").read_text() == "keep"


def test_delete_requires_header_kind_and_version(client, tmp_path):
    (tmp_path / "keep.txt").write_text("keep")
    assert client.delete("/api/file", params={"path": "keep.txt"}).status_code == 403
    assert (
        client.delete(
            "/api/file", headers={"X-Web-Edit": "1"}, params={"path": "keep.txt"}
        ).status_code
        == 400
    )
    assert (tmp_path / "keep.txt").exists()


def test_delete_rejects_changed_item(client, tmp_path):
    (tmp_path / "file.txt").write_text("before")
    before = client.get("/api/browse").json()["entries"][0]
    (tmp_path / "file.txt").write_text("changed externally")
    assert (
        delete_entry(client, "file.txt", version=before["delete_version"]).status_code
        == 409
    )
    assert (tmp_path / "file.txt").read_text() == "changed externally"


def test_delete_rejects_changed_kind(client, tmp_path):
    (tmp_path / "folder").mkdir()
    assert delete_entry(client, "folder", kind="file").status_code == 409
    assert (tmp_path / "folder").exists()


def test_delete_missing_file(client):
    response = client.delete(
        "/api/file",
        headers={"X-Web-Edit": "1"},
        params={"path": "gone", "kind": "file", "version": "anything"},
    )
    assert response.status_code == 404


def test_delete_folder_does_not_follow_child_symlinks(client, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder" / "link").symlink_to(outside, target_is_directory=True)
    (tmp_path / "direct-link").symlink_to(outside, target_is_directory=True)
    response = client.delete(
        "/api/file",
        headers={"X-Web-Edit": "1"},
        params={"path": "direct-link", "kind": "directory", "version": "anything"},
    )
    assert response.status_code == 403
    assert delete_entry(client, "folder").status_code == 200
    assert not (tmp_path / "folder").exists()
    assert (outside / "keep.txt").read_text() == "keep"
    assert (tmp_path / "direct-link").is_symlink()
