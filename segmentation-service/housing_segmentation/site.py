"""Read-only built-site hosting and CORS limited to the API namespace."""

from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Match


class ApiCORS:
    def __init__(self, app):
        self.app = app
        self.cors = CORSMiddleware(
            app,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type"],
        )

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        target = (
            self.cors
            if scope["type"] == "http" and (path == "/v1" or path.startswith("/v1/"))
            else self.app
        )
        await target(scope, receive, send)


def mount_site(application, directory):
    class SiteRoute(APIRoute):
        def matches(self, scope):
            path = scope.get("path", "")
            if path == "/v1" or path.startswith("/v1/"):
                return Match.NONE, {}
            return super().matches(scope)

    router = APIRouter(route_class=SiteRoute)

    # Resolve on each request: the separate web worker can create/replace dist
    # while this service stays up. This code never writes in the website tree.
    def resolve(path):
        if (
            path == "v1"
            or path.startswith("v1/")
            or "\\" in path
            or "\x00" in path
            or ".." in PurePosixPath(path).parts
        ):
            raise HTTPException(404, "Not found")
        root = Path(directory).resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(404, "Not found")
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            candidate = root / "index.html"
        # Also block symlinks escaping through index.html itself.
        if not candidate.resolve().is_relative_to(root) or not candidate.is_file():
            raise HTTPException(404, "Website has not been built")
        return candidate

    @router.api_route(
        "/{site_path:path}", methods=["GET", "HEAD"], include_in_schema=False
    )
    async def built_site(site_path: str):
        path = await run_in_threadpool(resolve, site_path)
        return FileResponse(
            path,
            headers={"Cache-Control": "no-cache"}
            if path.name == "index.html"
            else None,
        )

    application.include_router(router)
