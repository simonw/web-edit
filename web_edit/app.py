"""Starlette application factory used by the CLI and embedders."""

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .files import IMAGE_LIMIT, TEXT_LIMIT, FileError, FileStore
from .git import GitStore
from .security import allowed_hosts_for, is_allowed_host

STATIC = Path(__file__).parent / "static"


class BrowserProtection:
    """Check hosts against DNS rebinding and require a non-simple write header."""

    def __init__(self, app, allowed_hosts):
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if not is_allowed_host(scope["headers"], self.allowed_hosts):
                await JSONResponse(
                    {
                        "error": "Host not allowed. Configure --allowed-host to allow an additional hostname or IP address."
                    },
                    status_code=400,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            headers = dict(scope["headers"])
            if (
                scope["method"] not in {"GET", "HEAD", "OPTIONS"}
                and headers.get(b"x-web-edit") != b"1"
            ):
                await JSONResponse(
                    {"error": "Missing X-Web-Edit header."}, status_code=403
                )(scope, receive, send)
                return

            async def secure_send(message):
                if message["type"] == "http.response.start":
                    message.setdefault("headers", []).extend(
                        [
                            (b"x-content-type-options", b"nosniff"),
                            (b"cache-control", b"no-store"),
                            (b"referrer-policy", b"no-referrer"),
                        ]
                    )
                await send(message)

            await self.app(scope, receive, secure_send)
        else:
            await self.app(scope, receive, send)


async def handle_error(request, error):
    if isinstance(error, FileError):
        return JSONResponse(
            {"error": str(error), **error.details}, status_code=error.status
        )
    if isinstance(error, FileNotFoundError):
        return JSONResponse({"error": "File or folder not found."}, status_code=404)
    if isinstance(error, PermissionError):
        return JSONResponse(
            {"error": "Permission denied for this file or folder."}, status_code=403
        )
    return JSONResponse(
        {"error": "The filesystem operation could not be completed."}, status_code=400
    )


def create_app(directory=".", *, host="127.0.0.1", allowed_hosts=()):
    trusted_hosts = allowed_hosts_for(host, allowed_hosts)
    store = FileStore(directory)
    git_store = GitStore(store)

    def index(request):
        return FileResponse(
            STATIC / "index.html",
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
            },
        )

    def browse(request):
        return JSONResponse(store.browse(request.query_params.get("path", "")))

    def git_context(request):
        return JSONResponse(git_store.context(request.query_params.get("path", "")))

    def git_changes(request):
        return JSONResponse(
            git_store.changes(
                request.query_params.get("path", ""),
                request.query_params.get("comparison", "all"),
            )
        )

    def git_file(request):
        return JSONResponse(
            git_store.file(
                request.query_params.get("path", ""),
                request.query_params.get("comparison", "all"),
                request.query_params.get("entry"),
                request.query_params.get("snapshot"),
            )
        )

    async def git_action(request):
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > 8192:
                raise FileError("Git action body exceeds the limit.", 413)
            chunks.extend(chunk)
        try:
            body = json.loads(chunks)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise FileError("Invalid JSON.")
        fields = {"path", "comparison", "entry", "snapshot", "action"}
        if not isinstance(body, dict) or set(body) != fields:
            raise FileError("Provide path, comparison, entry, snapshot, and action.")
        result = await run_in_threadpool(git_store.action, **body)
        return JSONResponse(result)

    def read_text(request):
        return JSONResponse(store.text(request.query_params.get("path", "")))

    async def save_text(request):
        # Bound JSON before decoding, allowing for JSON's escaped representation.
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > TEXT_LIMIT * 6 + 8192:
                raise FileError("Text editing is limited to 10 MiB.", 413)
        try:
            body = json.loads(chunks)
        except (ValueError, UnicodeDecodeError):
            raise FileError("Invalid JSON.")
        if not isinstance(body, dict) or "path" not in body or "content" not in body:
            raise FileError("Provide path and content.")
        result = await run_in_threadpool(
            store.save_text, body["path"], body["content"], body.get("version")
        )
        return JSONResponse(result)

    async def upload(request):
        async with request.form(max_files=1, max_fields=2) as form:
            file = form.get("file")
            relative = form.get("path")
            expected = form.get("version")
            if not isinstance(file, UploadFile) or not isinstance(relative, str):
                raise FileError("Provide a file and destination path.")
            if expected is not None and not isinstance(expected, str):
                raise FileError("Invalid version.")
            result = await run_in_threadpool(store.write, relative, file.file, expected)
            return JSONResponse(result, status_code=201)

    def download(request):
        path = store.regular_file(request.query_params.get("path", ""))
        return FileResponse(
            path, filename=path.name, media_type="application/octet-stream"
        )

    def delete(request):
        return JSONResponse(
            store.delete(
                request.query_params.get("path", ""),
                request.query_params.get("kind"),
                request.query_params.get("version"),
            )
        )

    def image(request):
        path = store.regular_file(request.query_params.get("path", ""))
        mime = store.image_type(path)
        if not mime or path.stat().st_size >= IMAGE_LIMIT:
            raise FileError(
                "Image previews are available for images smaller than 2 MB.", 415
            )
        return FileResponse(
            path,
            media_type=mime,
            headers={"Content-Security-Policy": "sandbox; default-src 'none'"},
        )

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/browse", browse),
            Route("/api/git/context", git_context),
            Route("/api/git/changes", git_changes),
            Route("/api/git/file", git_file),
            Route("/api/git/action", git_action, methods=["POST"]),
            Route("/api/text", read_text, methods=["GET"]),
            Route("/api/text", save_text, methods=["PUT"]),
            Route("/api/upload", upload, methods=["POST"]),
            Route("/api/download", download),
            Route("/api/file", delete, methods=["DELETE"]),
            Route("/api/image", image),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ],
        exception_handlers={FileError: handle_error, OSError: handle_error},
    )
    app.add_middleware(BrowserProtection, allowed_hosts=trusted_hosts)
    app.state.store = store
    app.state.git_store = git_store
    return app
