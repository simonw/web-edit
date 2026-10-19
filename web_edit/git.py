"""Git comparisons and index actions within a FileStore's startup boundary."""

import hashlib
import json
import os
import selectors
import secrets
import signal
import stat
import subprocess
import time
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path, PurePosixPath

from .files import TEXT_LIMIT, FileError, decode_text

GIT_TIMEOUT = 10
OUTPUT_LIMIT = 16 * 1024 * 1024
ENTRY_LIMIT = 10_000
CHANGED = "Repository changed. Refresh the diff"
UNSUPPORTED_PLATFORM = "Git diff is not supported on this platform"
FILTERED_STAGE = "Files with Git filters must be staged outside web-edit."


def digest(data):
    return hashlib.sha256(data).hexdigest()


class GitFailure(FileError):
    pass


def pinned_operation(method):
    """Keep directory descriptors private to one request/thread context."""

    @wraps(method)
    def pinned(self, *args, **kwargs):
        if self._directories.get() is not None:
            return method(self, *args, **kwargs)
        directories = {}
        token = self._directories.set(directories)
        repositories_token = self._repositories.set({})
        try:
            result = method(self, *args, **kwargs)
            for (path, metadata), fd in directories.items():
                self._validate_directory(path, fd, metadata=metadata)
            return result
        finally:
            self._directories.reset(token)
            self._repositories.reset(repositories_token)
            for fd in directories.values():
                os.close(fd)

    return pinned


class GitStore:
    def __init__(self, store):
        self.store = store
        info = store.root.stat()
        self._startup_identity = (info.st_dev, info.st_ino)
        self._directories = ContextVar("git_directories", default=None)
        self._repositories = ContextVar("git_repositories", default=None)

    def _open_directory(self, folder, *, metadata=False):
        """Walk without links; only Git-derived metadata can be outside root."""
        if (
            os.name != "posix"
            or not all(
                hasattr(os, flag)
                for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
            )
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
        ):
            raise GitFailure(UNSUPPORTED_PLATFORM, 503)
        folder = Path(folder)
        if (
            not folder.is_absolute()
            or ".." in folder.parts
            or (not metadata and not folder.is_relative_to(self.store.root))
        ):
            raise FileError("Paths must stay inside the root folder.", 403)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open("/", flags)
        current = Path("/")
        try:
            if self.store.root == current:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != self._startup_identity:
                    raise FileError(CHANGED, 409)
            for part in folder.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                current /= part
                if current == self.store.root:
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) != self._startup_identity:
                        raise FileError(CHANGED, 409)
            return fd
        except OSError:
            os.close(fd)
            raise FileError(CHANGED, 409)
        except BaseException:
            os.close(fd)
            raise

    def _validate_directory(self, folder, expected, *, metadata=False):
        actual = self._open_directory(folder, metadata=metadata)
        try:
            before, after = os.fstat(expected), os.fstat(actual)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise FileError(CHANGED, 409)
        finally:
            os.close(actual)

    def _pin_directory(self, folder, *, metadata=False):
        folder = Path(folder)
        directories = self._directories.get()
        key = (folder, metadata)
        if key not in directories:
            directories[key] = self._open_directory(folder, metadata=metadata)
        fd = directories[key]
        self._validate_directory(folder, fd, metadata=metadata)
        return fd

    @pinned_operation
    def _run(self, folder, *args, limit=OUTPUT_LIMIT, input=None, index_file=None, conversion=None, objects_fd=None):
        fd = self._pin_directory(folder)
        options = {} if index_file is None else {"index_file": index_file}
        if conversion is not None:
            options["conversion"] = conversion
        if objects_fd is not None:
            options["objects_fd"] = objects_fd
        if input is None:
            result = self._execute(fd, *args, limit=limit, **options)
        else:
            if len(input) > OUTPUT_LIMIT:
                raise FileError("Git action input exceeds the limit.", 413)
            # A bounded temporary file avoids stdin/stdout pipe deadlocks.
            with tempfile.TemporaryFile() as source:
                source.write(input)
                source.seek(0)
                result = self._execute(fd, *args, limit=limit, source=source, **options)
        self._validate_directory(folder, fd)
        return result

    @staticmethod
    def _fd_cwd(fd):
        expected = os.fstat(fd)
        for prefix in ("/proc/self/fd", "/dev/fd"):
            path = f"{prefix}/{fd}"
            try:
                actual = os.stat(path)
            except OSError:
                continue
            if stat.S_ISDIR(actual.st_mode) and (actual.st_dev, actual.st_ino) == (
                expected.st_dev,
                expected.st_ino,
            ):
                return path
        raise GitFailure(UNSUPPORTED_PLATFORM, 503)

    def _execute(self, fd, *args, limit=OUTPUT_LIMIT, source=None, index_file=None, conversion=None, objects_fd=None):
        # Never inherit repository, object, config-injection, or tracing overrides.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            GIT_LITERAL_PATHSPECS="1",
            GIT_NO_LAZY_FETCH="1",
            GIT_ALLOW_PROTOCOL="",
            GIT_PROTOCOL_FROM_USER="0",
            GIT_PAGER="cat",
            LC_ALL="C",
        )
        inherited_fds = (fd,)
        if index_file is not None:
            index_fd, name = index_file
            env["GIT_INDEX_FILE"] = f"{self._fd_cwd(index_fd)}/{name}"
            inherited_fds += (index_fd,)
        if objects_fd is not None:
            env["GIT_OBJECT_DIRECTORY"] = self._fd_cwd(objects_fd)
            inherited_fds += (objects_fd,)
        if conversion is not None:
            # Conversion gets no live repository/system/global configuration.
            # Only explicitly copied built-in settings can affect this process.
            env.update(
                GIT_DIR=str(conversion),
                GIT_WORK_TREE=self._fd_cwd(fd),
                GIT_CONFIG_NOSYSTEM="1",
                GIT_CONFIG_SYSTEM=os.devnull,
                GIT_CONFIG_GLOBAL=os.devnull,
            )
        command = [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "submodule.recurse=false",
            *args,
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=source if source is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self._fd_cwd(fd),
                pass_fds=inherited_fds,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise GitFailure("Git is not installed", 503)
        output, errors = bytearray(), bytearray()
        deadline = time.monotonic() + GIT_TIMEOUT
        try:
            with selectors.DefaultSelector() as selector:
                for stream, buffer in (
                    (process.stdout, output),
                    (process.stderr, errors),
                ):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, buffer)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GitFailure("Git read timed out. Try again.", 503)
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        bound = limit if key.data is output else 65536
                        if len(key.data) + len(chunk) > bound:
                            raise GitFailure("Git output exceeds the read limit.", 413)
                        key.data.extend(chunk)
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise GitFailure("Git read timed out. Try again.", 503)
            if process.returncode:
                # Do not return stderr: it can disclose paths outside the workspace.
                message = "Cannot read this repository"
                if b"not a git repository" in errors:
                    message = "Not in a Git repository"
                raise GitFailure(message, 400)
            return bytes(output)
        finally:
            # Kill the group even if Git exited but a helper holds a pipe open.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()

    @pinned_operation
    def _discover(self, relative):
        folder = self.store.path(relative)
        if not folder.is_dir():
            if not folder.exists():
                raise FileNotFoundError()
            raise FileError("This is not a folder.")
        try:
            inside = self._run(folder, "rev-parse", "--is-inside-work-tree").strip()
            if inside != b"true":
                return None, "Not in a Git repository"
            raw = self._run(folder, "rev-parse", "--show-toplevel")
            root = Path(os.fsdecode(raw.removesuffix(b"\n"))).resolve()
            if not root.is_relative_to(self.store.root):
                return None, "Repository is outside the workspace"
            # Apply the same no-symlink rule as ordinary filesystem operations.
            self.store.path(root.relative_to(self.store.root).as_posix())
            self._pin_directory(root)
            self._authorize_metadata(root)
            return root, None
        except GitFailure as error:
            return None, str(error)

    @pinned_operation
    def context(self, path=""):
        root, reason = self._discover(path)
        return {
            "available": root is not None,
            "reason": reason,
            "repo_path": self._relative(root) if root else None,
        }

    def _relative(self, path):
        relative = path.relative_to(self.store.root).as_posix()
        return "" if relative == "." else relative

    def _root(self, path):
        root, reason = self._discover(path)
        if root is None:
            raise FileError(reason, 503 if reason == "Git is not installed" else 400)
        return root

    @staticmethod
    def _name(raw):
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise FileError("Git filenames must use UTF-8.", 415)
        if not name or "\x00" in name:
            raise FileError("Invalid Git filename.")
        return name

    @pinned_operation
    def _disk(self, root, name, *, content=False):
        """Read regular files through no-follow directory descriptors, never links."""
        relative = self._relative(root / name)
        parts = PurePosixPath(name).parts
        if not parts or ".." in parts or PurePosixPath(name).is_absolute():
            raise FileError("Paths must stay inside the root folder.", 403)
        # FileStore validates encoding and path rules. Links are allowed only as
        # metadata entries; descriptor traversal below never follows them.
        try:
            self.store.path(relative)
        except FileError as error:
            if str(error) != "Symbolic links are not followed.":
                raise
        parent = os.dup(self._pin_directory(root))
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent,
                    )
                except FileNotFoundError:
                    return {"kind": "missing"}, None
                except OSError:
                    return {"kind": "unavailable"}, None
                os.close(parent)
                parent = child
            try:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return {"kind": "missing"}, None
            kind = "regular" if stat.S_ISREG(info.st_mode) else "special"
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
            metadata = {
                "kind": kind,
                "stat": [
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ],
            }
            if not content or kind != "regular" or info.st_size > TEXT_LIMIT:
                return metadata, None
            try:
                fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent,
                )
            except OSError:
                raise FileError(CHANGED, 409)
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (info.st_dev, info.st_ino):
                    raise FileError(CHANGED, 409)
                data = source.read(TEXT_LIMIT + 1)
                after = os.fstat(source.fileno())
            if len(data) > TEXT_LIMIT or (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) != (info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                raise FileError(CHANGED, 409)
            metadata["hash"] = digest(data)
            return metadata, data
        finally:
            os.close(parent)

    @pinned_operation
    def _state(self, root, comparison, *, actions=True):
        if comparison not in {"all", "unstaged", "staged"}:
            raise FileError("Choose all, unstaged, or staged changes.")
        # --verify --quiet also distinguishes an unborn branch without parsing stderr.
        head = (
            self._run(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
            if self._has_head(root)
            else None
        )
        # hash-object does not write an object. Git recognizes its empty tree for
        # both SHA-1 and SHA-256 repositories, including before the first commit.
        base = (
            head.strip().decode("ascii")
            if head
            else self._run(root, "hash-object", "-t", "tree", "--stdin")
            .strip()
            .decode("ascii")
        )
        index = self._run(root, "ls-files", "--stage", "-z")
        conflicts = set()
        for item in index.split(b"\0"):
            if item:
                fields, name = item.split(b"\t", 1)
                if fields.split()[2] != b"0":
                    conflicts.add(self._name(name))
        # Even raw diff performs clean conversion. Configuration isolation
        # below prevents external filters, including concurrently added drivers.
        args = [
            "diff",
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            "--ignore-submodules=none",
        ]
        if comparison == "staged":
            args += ["--cached", base]
        elif comparison == "all":
            args += [base]
        # Diff also performs clean conversion. Use the same immutable safe
        # configuration as staging, but read the real pinned index/objects.
        with self._conversion_repository(root, len(base) == 64) as (folder, options):
            index_path = self._git_path(root, "index")
            objects_path = self._git_path(root, "objects")
            raw = self._run(
                root, *options, *args, "--", conversion=folder,
                index_file=(self._pin_directory(index_path.parent, metadata=True), index_path.name),
                objects_fd=self._pin_directory(objects_path, metadata=True),
            )
        parts = raw.split(b"\0")
        entries = []
        i = 0
        while i < len(parts) and parts[i]:
            header = parts[i].split()
            if len(header) != 5 or not header[0].startswith(b":"):
                raise FileError("Cannot parse Git changes.")
            old_mode, new_mode, old_oid, new_oid, status = header
            code = status[:1].decode("ascii")
            old_path = new_path = self._name(parts[i + 1])
            i += 2
            if code in {"R", "C"}:
                new_path = self._name(parts[i])
                i += 1
            entries.append(
                {
                    "status": "unmerged"
                    if new_path in conflicts or code == "U"
                    else {
                        "A": "added",
                        "D": "deleted",
                        "M": "modified",
                        "R": "renamed",
                        "C": "copied",
                        "T": "type_changed",
                    }.get(code, "modified"),
                    "old_path": None if code == "A" else old_path,
                    "new_path": None if code == "D" else new_path,
                    "old_mode": old_mode[1:].decode("ascii"),
                    "new_mode": new_mode.decode("ascii"),
                    "_old_oid": old_oid.decode("ascii"),
                    "_new_oid": new_oid.decode("ascii"),
                }
            )
            if len(entries) > ENTRY_LIMIT:
                raise FileError("Too many changed files. The limit is 10000.", 413)
        if comparison != "staged":
            for name in self._run(
                root, "ls-files", "--others", "--exclude-standard", "-z", "--"
            ).split(b"\0"):
                if name:
                    entries.append(
                        {
                            "status": "untracked",
                            "old_path": None,
                            "new_path": self._name(name),
                            "old_mode": "000000",
                            "new_mode": "100644",
                            "_old_oid": "",
                            "_new_oid": "",
                        }
                    )
                    if len(entries) > ENTRY_LIMIT:
                        raise FileError(
                            "Too many changed files. The limit is 10000.", 413
                        )
        if len(entries) > ENTRY_LIMIT:
            raise FileError("Too many changed files. The limit is 10000.", 413)
        # Raw diff can emit multiple records for an unmerged path.
        unique = {}
        for entry in entries:
            key = entry["new_path"] or entry["old_path"]
            if key in conflicts:
                entry["status"] = "unmerged"
            unique[key] = entry
        entries = [unique[key] for key in sorted(unique)]
        unstaged_paths = set()
        if actions and comparison == "all":
            unstaged, _ = self._state(root, "unstaged", actions=False)
            unstaged_paths = {
                name
                for item in unstaged
                for name in (item["old_path"], item["new_path"])
                if name is not None
            }
        filtered = set()
        if actions and comparison != "staged":
            filtered = self._filtered_paths(
                root, [item["new_path"] for item in entries if item["new_path"]],
            )
        disk = []
        for entry in entries:
            if comparison != "staged":
                state, _ = self._disk(root, entry["new_path"] or entry["old_path"])
                disk.append(state)
                if entry["new_mode"] == "160000" and state["kind"] == "special":
                    submodule_path = self._relative(root / entry["new_path"])
                    submodule, _ = self._discover(submodule_path)
                    if submodule == root / entry["new_path"] and self._has_head(
                        submodule
                    ):
                        entry["_new_oid"] = (
                            self._run(submodule, "rev-parse", "--verify", "HEAD")
                            .strip()
                            .decode("ascii")
                        )
                if entry["status"] == "untracked":
                    if state["kind"] == "symlink":
                        entry["new_mode"] = "120000"
                    elif state["kind"] == "regular":
                        entry["new_mode"] = (
                            "100755" if state["stat"][2] & 0o111 else "100644"
                        )
            if actions:
                paths = {entry["old_path"], entry["new_path"]} - {None}
                supported = (
                    entry["status"] != "unmerged"
                    and not paths.intersection(conflicts)
                    and {entry["old_mode"], entry["new_mode"]}
                    <= {"000000", "100644", "100755"}
                    and (comparison == "staged" or state["kind"] in {"regular", "missing"})
                )
                entry["action"] = None
                if supported:
                    entry["action"] = (
                        "stage"
                        if comparison == "unstaged"
                        or (comparison == "all" and paths.intersection(unstaged_paths))
                        else "unstage"
                    )
                    if entry["action"] == "stage" and entry["new_path"] in filtered:
                        entry["action"] = None
            public = {
                key: value for key, value in entry.items() if not key.startswith("_")
            }
            entry["id"] = digest(json.dumps(public, sort_keys=True).encode())
        token = (
            digest(head or b"")
            + digest(index)
            + digest(raw)
            + digest(
                json.dumps(
                    [
                        str(root),
                        self._directory_identity(root),
                        self._metadata_identity(root),
                        comparison,
                        entries,
                        disk,
                    ],
                    sort_keys=True,
                ).encode()
            )
        )
        return entries, digest(token.encode())

    def _directory_identity(self, root):
        info = os.fstat(self._pin_directory(root))
        return [info.st_dev, info.st_ino]

    def _has_head(self, root):
        # --revs-only returns no revision for an unborn HEAD; detached HEAD works too.
        refs = self._run(root, "rev-parse", "--revs-only", "HEAD")
        return bool(refs.strip())

    @pinned_operation
    def changes(self, path="", comparison="all"):
        root = self._root(path)
        entries, snapshot = self._state(root, comparison)
        return {
            "repo_path": self._relative(root),
            "comparison": comparison,
            "snapshot": snapshot,
            "entries": [
                {key: value for key, value in entry.items() if not key.startswith("_")}
                for entry in entries
            ],
        }

    def _metadata_text(self, folder, name):
        fd = self._pin_directory(folder, metadata=True)
        source = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd,
        )
        with os.fdopen(source, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise FileError("Invalid worktree registration.", 403)
            data = stream.read(8193)
        if len(data) > 8192 or b"\0" in data:
            raise FileError("Invalid worktree registration.", 403)
        return os.fsdecode(data.removesuffix(b"\n"))

    def _authorize_metadata(self, root):
        gitdir = Path(os.fsdecode(
            self._run(root, "rev-parse", "--absolute-git-dir").removesuffix(b"\n")
        ))
        common = Path(os.fsdecode(
            self._run(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .removesuffix(b"\n")
        ))
        repositories = self._repositories.get()
        identity = (gitdir, common)
        if root in repositories and repositories[root] != identity:
            raise FileError(CHANGED, 409)
        if all(path.is_relative_to(self.store.root) for path in identity):
            self._pin_directory(gitdir, metadata=True)
            self._pin_directory(common, metadata=True)
            repositories[root] = identity
            return
        reason = "External Git metadata is not registered to this worktree."
        # A .git file alone is not authorization to mutate another repository.
        # External metadata must be an actual linked-worktree registration in
        # the common repository, with a backlink to this exact worktree.
        if gitdir.parent != common / "worktrees" or gitdir.name in {"", ".", ".."}:
            raise FileError(reason, 403)
        try:
            backlink = Path(self._metadata_text(gitdir, "gitdir"))
            registered_common = self._metadata_text(gitdir, "commondir")
            common_path = Path(os.path.normpath(gitdir / registered_common))
            pointer = self._metadata_text(root, ".git")
        except OSError:
            raise FileError(reason, 403)
        if not pointer.startswith("gitdir: "):
            raise FileError(reason, 403)
        target = Path(pointer[len("gitdir: "):])
        if not target.is_absolute():
            target = root / target
        if (
            backlink != root / ".git"
            or common_path != common
            or Path(os.path.normpath(target)) != gitdir
        ):
            raise FileError(reason, 403)
        self._pin_directory(common, metadata=True)
        repositories[root] = identity

    def _metadata_identity(self, root):
        result = []
        for path in self._repositories.get()[root]:
            info = os.fstat(self._pin_directory(path, metadata=True))
            result.append([str(path), info.st_dev, info.st_ino])
        return result

    def _git_path(self, root, name):
        # Never rediscover a mutation target through a live .git pointer. Every
        # path comes from the metadata identity authorized for this request.
        gitdir, common = self._repositories.get()[root]
        if name == "index":
            return gitdir / "index"
        if name in {"objects", "info/attributes"}:
            return common / name
        raise FileError("Invalid Git metadata path.")

    @contextmanager
    def _conversion_repository(self, root, sha256=False):
        # Enumerating and disabling filter drivers is insufficient here: a new
        # driver could be configured between enumeration and process startup.
        # A private repository has only built-in conversion settings, so even
        # concurrently changed attributes cannot select an external command.
        allowed = {
            b"core.autocrlf", b"core.eol", b"core.safecrlf",
            b"core.checkroundtripencoding", b"core.attributesfile",
            b"core.filemode", b"core.symlinks", b"core.ignorecase",
            b"core.trustctime", b"core.checkstat", b"core.ignorestat",
        }
        options = []
        for item in self._run(root, "config", "--null", "--list").split(b"\0"):
            key, separator, value = item.partition(b"\n")
            if key.lower() in allowed:
                options += ["-c", os.fsdecode(key) + "=" + os.fsdecode(value if separator else b"true")]
        with tempfile.TemporaryDirectory(prefix="web-edit-conversion-") as temporary:
            folder = Path(temporary)
            (folder / "objects").mkdir()
            (folder / "refs").mkdir()
            (folder / "info").mkdir()
            (folder / "HEAD").write_text("ref: refs/heads/main\n")
            config = (
                "[core]\nrepositoryformatversion = 1\n[extensions]\nobjectformat = sha256\n"
                if sha256 else "[core]\nrepositoryformatversion = 0\n"
            )
            (folder / "config").write_text(config)
            attributes = self._git_path(root, "info/attributes")
            try:
                if not attributes.parent.exists():
                    raise FileNotFoundError()
                parent = self._pin_directory(attributes.parent, metadata=True)
                source = os.open(
                    attributes.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent,
                )
            except FileNotFoundError:
                source = None
            if source is not None:
                with os.fdopen(source, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > TEXT_LIMIT:
                        raise FileError("Git attributes exceed the action limit.", 413)
                    contents = stream.read(TEXT_LIMIT + 1)
                if len(contents) > TEXT_LIMIT:
                    raise FileError("Git attributes exceed the action limit.", 413)
                (folder / "info/attributes").write_bytes(contents)
            yield folder, options

    def _filtered_paths(self, root, names, *, conversion=None, options=()):
        if not names:
            return set()
        if conversion is None:
            with self._conversion_repository(root) as (folder, settings):
                return self._filtered_paths(root, names, conversion=folder, options=settings)
        raw = self._run(
            root, *options, "check-attr", "-z", "--stdin", "filter",
            input=b"".join(name.encode("utf-8") + b"\0" for name in names),
            conversion=conversion,
        ).split(b"\0")
        return {
            self._name(raw[i])
            for i in range(0, len(raw) - 2, 3)
            if raw[i + 2] not in {b"unspecified", b"unset"}
        }

    def _stage_blob(self, root, name, data):
        with self._conversion_repository(root) as (folder, options):
            if self._filtered_paths(root, [name], conversion=folder, options=options):
                raise FileError(FILTERED_STAGE)
            oid = self._run(
                root, *options, "hash-object", "-w", f"--path={name}", "--stdin",
                input=data, limit=128, conversion=folder,
            ).strip().decode("ascii")
            converted = self._run(
                root, "cat-file", "blob", oid, limit=TEXT_LIMIT, conversion=folder,
            )
        return self._run(
            root, "hash-object", "-w", "--no-filters", "--stdin",
            input=converted, limit=128,
        ).strip()

    @contextmanager
    def _index_transaction(self, root):
        index = self._git_path(root, "index")
        # As with repository directories, never reopen metadata through links.
        folder = index.parent
        fd = self._pin_directory(folder, metadata=True)
        lock_name = index.name + ".lock"
        try:
            lock = os.open(
                lock_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=fd,
            )
        except FileExistsError:
            raise FileError("Index is locked. Try again.", 409)
        temporary = ".web-edit-index-" + secrets.token_hex(16)
        try:
            try:
                source = os.open(
                    index.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd,
                )
            except FileNotFoundError:
                source = None
            if source is not None:
                with os.fdopen(source, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > OUTPUT_LIMIT:
                        raise FileError("Index exceeds the action limit.", 413)
                    data = stream.read(OUTPUT_LIMIT + 1)
                if len(data) > OUTPUT_LIMIT:
                    raise FileError("Index exceeds the action limit.", 413)
                target = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    stat.S_IMODE(info.st_mode), dir_fd=fd,
                )
                with os.fdopen(target, "wb") as stream:
                    stream.write(data)
            yield fd, temporary
            result = os.open(
                temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd,
            )
            try:
                if not stat.S_ISREG(os.fstat(result).st_mode):
                    raise FileError(CHANGED, 409)
                os.fsync(result)
            finally:
                os.close(result)
            self._validate_directory(folder, fd, metadata=True)
            os.replace(temporary, index.name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(lock)
            for name in (temporary, temporary + ".lock", lock_name):
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass

    @pinned_operation
    def action(self, path, comparison, entry, snapshot, action):
        if (
            not all(isinstance(value, str) for value in (path, comparison, entry, snapshot, action))
            or comparison not in {"all", "unstaged", "staged"}
            or action not in {"stage", "unstage"}
        ):
            raise FileError("Invalid Git action.")
        root = self._root(path)
        entries, current = self._state(root, comparison)
        if not snapshot or snapshot != current:
            raise FileError(CHANGED, 409)
        selected = next((item for item in entries if item["id"] == entry), None)
        if selected is None:
            raise FileError("Invalid Git action.")
        if selected["action"] != action:
            if (
                action == "stage"
                and selected["new_path"]
                and self._filtered_paths(root, [selected["new_path"]])
            ):
                raise FileError(FILTERED_STAGE)
            raise FileError("Invalid Git action.")
        paths = sorted({selected["old_path"], selected["new_path"]} - {None})
        replacements = {}
        if action == "stage":
            for name in paths:
                disk, data = self._disk(root, name, content=True)
                if disk["kind"] == "missing":
                    continue
                if disk["kind"] != "regular":
                    raise FileError("This file does not support staging.")
                if data is None:
                    raise FileError("Staging is limited to 10 MiB per file.", 413)
                oid = self._stage_blob(root, name, data)
                mode = b"100755" if disk["stat"][2] & 0o111 else b"100644"
                replacements[name] = (mode, oid)
        elif self._has_head(root):
            base = self._run(root, "rev-parse", "--verify", "HEAD^{commit}").strip().decode("ascii")
            for record in self._run(root, "ls-tree", "-r", "-z", base).split(b"\0"):
                if record:
                    fields, raw_name = record.split(b"\t", 1)
                    name = self._name(raw_name)
                    if name in paths:
                        mode, _, oid = fields.split()
                        replacements[name] = (mode, oid)
        # update-index addresses exact names, but adding a file over an
        # unrelated directory entry (or vice versa) must still be refused.
        tracked = {
            self._name(name)
            for name in self._run(root, "ls-files", "-z").split(b"\0")
            if name
        }
        for name in replacements:
            if any(
                other.startswith(name + "/") or name.startswith(other + "/")
                for other in tracked.difference(paths)
            ):
                raise FileError("Index path conflict. Refresh the diff.", 409)
        zero = b"0" * len(self._run(root, "hash-object", "--stdin").strip())
        records = bytearray()
        for name in paths:
            records.extend(b"0 " + zero + b"\t" + name.encode("utf-8") + b"\0")
        for name, (mode, oid) in replacements.items():
            records.extend(mode + b" " + oid + b"\t" + name.encode("utf-8") + b"\0")
        # Hold the real index lock from final validation through publication.
        # Git updates a private index so it cannot remove our real lock.
        with self._index_transaction(root) as index_file:
            if self._root(path) != root or self._state(root, comparison)[1] != snapshot:
                raise FileError(CHANGED, 409)
            self._run(
                root, "update-index", "-z", "--index-info",
                input=records, index_file=index_file,
            )
        return self.changes(path, comparison)

    @pinned_operation
    def _blob(self, root, oid):
        if not oid or not oid.strip("0"):
            return b"", 0
        if len(oid) not in {40, 64} or any(c not in "0123456789abcdef" for c in oid):
            raise FileError("Invalid Git object identifier.")
        size = int(self._run(root, "cat-file", "-s", oid, limit=128).strip())
        if size > TEXT_LIMIT:
            return None, size
        return self._run(root, "cat-file", "blob", oid, limit=TEXT_LIMIT), size

    @staticmethod
    def _source(label, data, size):
        source = {
            "label": label,
            "content": None,
            "byte_size": size,
            "hash": None,
            "bom": None,
            "line_endings": None,
            "final_newline": None,
        }
        if data is None:
            return source, "File is too large for text diff"
        source["hash"] = digest(data)
        try:
            text = decode_text(data)
        except FileError:
            return source, "Text diff is not available for this file"
        crlf = text.count("\r\n")
        source.update(
            content=text,
            bom=data.startswith(b"\xef\xbb\xbf"),
            line_endings={
                "crlf": crlf,
                "lf": text.count("\n") - crlf,
                "cr": text.count("\r") - crlf,
            },
            final_newline=text.endswith(("\r", "\n")),
        )
        return source, None

    @pinned_operation
    def file(self, path="", comparison="all", entry=None, snapshot=None):
        root = self._root(path)
        entries, current = self._state(root, comparison)
        if not snapshot or current != snapshot:
            raise FileError(CHANGED, 409)
        selected = next((item for item in entries if item["id"] == entry), None)
        if selected is None:
            raise FileError("Choose an entry from the change list.", 404)
        old_mode, new_mode = selected["old_mode"], selected["new_mode"]
        reason = None
        modes = {old_mode, new_mode}
        if selected["status"] == "unmerged":
            reason = "Unmerged file"
        elif "120000" in modes:
            reason = "Symbolic link"
        elif "160000" in modes:
            reason = "Submodule"
        old_label = "Index" if comparison == "unstaged" else "HEAD"
        new_label = "Index" if comparison == "staged" else "Working tree"
        old = new = None
        working_path = None
        version = None
        if reason is None:
            old_data, old_size = self._blob(root, selected["_old_oid"])
            if comparison == "staged":
                new_data, new_size = self._blob(root, selected["_new_oid"])
            elif selected["new_path"] is None:
                new_data, new_size = b"", 0
            else:
                disk, new_data = self._disk(root, selected["new_path"], content=True)
                new_size = disk.get("stat", [0, 0, 0, 0])[3]
                if disk["kind"] == "missing":
                    reason = "File content is not available on disk"
                elif disk["kind"] != "regular":
                    reason = "Text diff is not available for this file"
                working_path = self._relative(root / selected["new_path"])
                version = disk.get("hash")
            old, old_reason = self._source(old_label, old_data, old_size)
            new, new_reason = self._source(new_label, new_data, new_size)
            reason = reason or old_reason or new_reason
        if self._root(path) != root or self._state(root, comparison)[1] != snapshot:
            raise FileError(CHANGED, 409)
        notices = []
        if old and new and not reason:
            normalized = lambda value: value.replace("\r\n", "\n").replace("\r", "\n")
            if normalized(old["content"]) == normalized(new["content"]):
                if old["content"] != new["content"]:
                    notices.append("Line endings differ")
                if old["bom"] != new["bom"]:
                    notices.append("UTF-8 BOM differs")
                if not notices:
                    notices.append("No text changes")
        return {
            "repo_path": self._relative(root),
            "comparison": comparison,
            "snapshot": snapshot,
            "entry": {
                key: value for key, value in selected.items() if not key.startswith("_")
            },
            "old": old,
            "new": new,
            "reason": reason,
            "notices": notices,
            "editable": not reason
            and comparison != "staged"
            and working_path is not None,
            "working_path": working_path,
            "version": version,
            "old_oid": selected["_old_oid"] or None,
            "new_oid": selected["_new_oid"] or None,
        }
