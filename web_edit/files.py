"""Filesystem operations, confined to one root directory."""

import hashlib
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import threading
from bisect import bisect_left
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

IMAGE_LIMIT = 2_000_000
TEXT_LIMIT = 10 * 1024 * 1024
IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/bmp",
    "image/x-icon",
    "image/vnd.microsoft.icon",
    "image/svg+xml",
}
CONTROL_BYTES = bytes([*range(0x09), 0x0B, 0x0C, *range(0x0E, 0x20)])
# Python 3.13+ can skip the URL parsing that guess_type does for every filename.
guess_file_type = getattr(mimetypes, "guess_file_type", mimetypes.guess_type)


class FileError(Exception):
    def __init__(self, message, status=400, **details):
        super().__init__(message)
        self.status = status
        self.details = details


def version(path):
    # Content hashes ignore metadata-only changes from sync tools like Dropbox.
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def delete_version(info):
    """Identify the item shown in a listing without reading file contents."""
    value = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}:{stat.S_IFMT(info.st_mode)}"
    return hashlib.sha256(value.encode()).hexdigest()


def decode_text(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileError(
            "This file is not UTF-8 text. You can download it instead.", 415
        )
    # UTF-8 only uses these bytes for control characters; translate beats a regex.
    if len(data.translate(None, CONTROL_BYTES)) != len(data):
        raise FileError(
            "This looks like a binary file. You can download it instead.", 415
        )
    return text


def matching_blocks(old, new):
    """Yield (old index, new index, size) for runs of matching lines.

    SequenceMatcher alone slows down sharply on long files with scattered edits.
    As in patience diff, lines that occur once in each version anchor the
    comparison, so SequenceMatcher only aligns the gaps between anchors.
    """
    old_counts, new_counts = Counter(old), Counter(new)
    positions = {line: i for i, line in enumerate(old) if old_counts[line] == 1}
    candidates = [
        (positions[line], j)
        for j, line in enumerate(new)
        if new_counts[line] == 1 and line in positions
    ]
    # Keep the longest series of candidates in the same order in both versions.
    tails, ends, links = [], [], []
    for index, (i, _) in enumerate(candidates):
        length = bisect_left(tails, i)
        links.append(ends[length - 1] if length else None)
        if length == len(tails):
            tails.append(i)
            ends.append(index)
        else:
            tails[length] = i
            ends[length] = index
    anchors = [(len(old), len(new), 0)]
    index = ends[-1] if ends else None
    while index is not None:
        anchors.append((*candidates[index], 1))
        index = links[index]
    anchors.append((0, 0, 0))
    a = b = 0
    for i, j, size in reversed(anchors):
        if i < a:
            continue  # Already matched by extending the previous block.
        # Extend each anchor backward, compare the remaining gap, then extend forward.
        start_a, start_b = i, j
        while a < start_a and b < start_b and old[start_a - 1] == new[start_b - 1]:
            start_a, start_b = start_a - 1, start_b - 1
        if a < start_a and b < start_b:
            # SequenceMatcher's autojunk heuristic handles repetitive documents.
            matcher = SequenceMatcher(None, old[a:start_a], new[b:start_b])
            for match in matcher.get_matching_blocks():
                yield a + match.a, b + match.b, match.size
        a, b = i + size, j + size
        while a < len(old) and b < len(new) and old[a] == new[b]:
            a, b = a + 1, b + 1
        yield start_a, start_b, a - start_a


def preserve_endings(original, edited):
    """Keep the endings of unchanged lines, including files with mixed endings."""
    edited = edited.replace("\r\n", "\n").replace("\r", "\n")
    crlf = original.count("\r\n")
    counts = {
        "\r\n": crlf,
        "\r": original.count("\r") - crlf,
        "\n": original.count("\n") - crlf,
    }
    used = [ending for ending, count in counts.items() if count]
    if len(used) < 2:
        # Files with one kind of line ending need no comparison.
        return edited.replace("\n", used[0]) if used else edited
    parts = re.split(r"(\r\n|\r|\n)", original)
    old_lines, old_endings = parts[::2], parts[1::2]
    new_lines = edited.split("\n")
    default = Counter(old_endings).most_common(1)[0][0]
    endings = [default] * (len(new_lines) - 1)
    for old_index, new_index, size in matching_blocks(old_lines, new_lines):
        # The final line of each version has no ending.
        size = min(size, len(old_endings) - old_index, len(endings) - new_index)
        if size > 0:
            endings[new_index : new_index + size] = old_endings[
                old_index : old_index + size
            ]
    return "".join(line + ending for line, ending in zip(new_lines, endings + [""]))


class FileStore:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("The root must be a directory")
        self.lock = threading.RLock()

    def path(self, relative, *, file=False):
        if not isinstance(relative, str) or "\x00" in relative or "\\" in relative:
            raise FileError("Invalid path.")
        try:
            relative.encode("utf-8")
        except UnicodeEncodeError:
            raise FileError("Invalid path.")
        parts = PurePosixPath(relative)
        if parts.is_absolute() or ".." in parts.parts:
            raise FileError("Paths must stay inside the root folder.", 403)
        if file and (not parts.parts or relative.endswith("/")):
            raise FileError("Choose a filename.")
        result = self.root
        for part in parts.parts:
            result = result / part
            if result.is_symlink():
                raise FileError("Symbolic links are not followed.", 403)
        if not result.resolve().is_relative_to(self.root):
            raise FileError("Paths must stay inside the root folder.", 403)
        return result

    def regular_file(self, relative):
        path = self.path(relative, file=True)
        if not stat.S_ISREG(path.stat().st_mode):
            raise FileError("This is not a regular file.")
        return path

    def image_type(self, path):
        mime = guess_file_type(path.name)[0]
        return mime if mime in IMAGE_TYPES else None

    def browse(self, relative):
        folder = self.path(relative)
        if not folder.is_dir():
            if not folder.exists():
                raise FileNotFoundError()
            raise FileError("This is not a folder.")
        path = "" if folder == self.root else folder.relative_to(self.root).as_posix()
        prefix = f"{path}/" if path else ""
        entries = []
        # scandir and plain strings avoid creating Path objects for every entry.
        with os.scandir(folder) as children:
            for child in children:
                if child.name.startswith(".web-edit-"):
                    continue
                try:
                    info = child.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
                if stat.S_ISLNK(info.st_mode):
                    kind = "symlink"
                elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    kind = "special"
                entries.append(
                    {
                        "name": child.name,
                        "path": prefix + child.name,
                        "kind": kind,
                        "size": info.st_size,
                        "modified": info.st_mtime,
                        "delete_version": delete_version(info),
                        "preview": kind == "file"
                        and info.st_size < IMAGE_LIMIT
                        and bool(self.image_type(child)),
                    }
                )
        entries.sort(
            key=lambda item: (
                item["kind"] != "directory",
                item["name"].casefold(),
                item["name"],
            )
        )
        return {
            "path": path,
            "root_name": self.root.name or "/",
            "entries": entries,
        }

    def delete(self, relative, kind, expected):
        with self.lock:
            target = self.path(relative)
            if target == self.root:
                raise FileError("The root folder cannot be deleted.", 403)
            if kind not in {"file", "directory"} or not expected:
                raise FileError(
                    "Provide the item kind and version from the folder listing."
                )
            info = target.lstat()
            actual_kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise FileError("Only regular files and folders can be deleted.", 403)
            if kind != actual_kind or expected != delete_version(info):
                raise FileError(
                    "This item changed since the folder was loaded. Refresh before deleting it.",
                    409,
                )
            if kind == "directory":
                # rmtree removes child symlinks themselves, never their targets.
                shutil.rmtree(target)
            else:
                target.unlink()
            return {"path": relative, "deleted": True}

    def text(self, relative):
        with self.lock:
            path = self.regular_file(relative)
            if path.stat().st_size > TEXT_LIMIT:
                raise FileError(
                    "Text editing is limited to 10 MiB. You can download this file.",
                    413,
                )
            data = path.read_bytes()
            current = hashlib.sha256(data).hexdigest()
            if version(path) != current:
                raise FileError(
                    "The file changed while opening it. Please try again.", 409
                )
            return {"path": relative, "content": decode_text(data), "version": current}

    def check_destination(self, relative, expected):
        path = self.path(relative, file=True)
        if path.exists():
            if not path.is_file():
                raise FileError(
                    "A folder or special file already exists at that path.", 409
                )
            current = version(path)
            if expected != current:
                raise FileError(
                    "This file already exists or has changed on disk.",
                    409,
                    version=current,
                    exists=True,
                )
        elif expected is not None:
            raise FileError(
                "The file was removed or moved on disk. Save your work to a new path.",
                409,
            )
        return path

    def write(self, relative, source, expected=None):
        with self.lock:
            target = self.check_destination(relative, expected)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path(relative, file=True)
            temporary = None
            # Hash while copying, so the version matches what was written without rereading.
            digest = hashlib.sha256()
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=".web-edit-", dir=target.parent, delete=False
                ) as output:
                    temporary = Path(output.name)
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                self.check_destination(relative, expected)
                if expected is None:
                    # link is an atomic create-if-absent; never silently replace a file.
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        raise FileError(
                            "A file appeared at this path during the upload. Please retry.",
                            409,
                        )
                else:
                    os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
                    os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return {"path": relative, "version": digest.hexdigest()}

    def save_text(self, relative, content, expected):
        from io import BytesIO

        if not isinstance(content, str) or (
            expected is not None and not isinstance(expected, str)
        ):
            raise FileError(
                "Content must be text and version must be a string or null."
            )
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            raise FileError("Content must be valid UTF-8 text.")
        if len(encoded) > TEXT_LIMIT:
            raise FileError("Text editing is limited to 10 MiB.", 413)
        decode_text(encoded)
        with self.lock:
            path = self.check_destination(relative, expected)
            if expected is not None:
                if path.stat().st_size > TEXT_LIMIT:
                    raise FileError("Text editing is limited to 10 MiB.", 413)
                original = path.read_bytes()
                encoded = preserve_endings(decode_text(original), content).encode(
                    "utf-8"
                )
                if original.startswith(b"\xef\xbb\xbf"):
                    encoded = b"\xef\xbb\xbf" + encoded
            return self.write(relative, BytesIO(encoded), expected)
