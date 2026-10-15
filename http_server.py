from __future__ import annotations

import os
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from starlette.responses import JSONResponse

from server import server


ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


class BearerAuthMiddleware:
    """Protect /mcp with a fixed bearer token and expose an unauthenticated health check."""

    def __init__(self, app: ASGIApp, access_token: str | None) -> None:
        self.app = app
        self.access_token = access_token

    def is_authorized(self, headers: list[tuple[bytes, bytes]]) -> bool:
        if not self.access_token:
            return True
        normalized = {key.lower(): value for key, value in headers}
        provided = normalized.get(b"authorization", b"").decode(
            "latin-1", errors="ignore"
        )
        expected = f"Bearer {self.access_token}"
        return secrets.compare_digest(provided, expected)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == "/healthz":
            response = JSONResponse({"status": "ok", "service": "cn-ecommerce"})
            await response(scope, receive, send)
            return

        if not self.is_authorized(scope.get("headers", [])):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def create_app(
    access_token: str | None = None,
    *,
    allow_anonymous: bool | None = None,
) -> ASGIApp:
    token = (
        access_token
        if access_token is not None
        else os.environ.get("MCP_ACCESS_TOKEN", "").strip()
    )
    anonymous = (
        _enabled("MCP_ALLOW_ANONYMOUS")
        if allow_anonymous is None
        else allow_anonymous
    )
    if not token and not anonymous:
        raise RuntimeError(
            "MCP_ACCESS_TOKEN is required. Set MCP_ALLOW_ANONYMOUS=1 only if you "
            "intentionally want an unauthenticated public endpoint."
        )
    return BearerAuthMiddleware(server.streamable_http_app(), token or None)


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1").strip()
    port = int(os.environ.get("MCP_PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("MCP_PORT must be between 1 and 65535")
    log_level = os.environ.get("MCP_LOG_LEVEL", "info").strip().lower()
    uvicorn.run(create_app(), host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
