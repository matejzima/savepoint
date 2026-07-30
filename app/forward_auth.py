from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse


class ForwardAuthMiddleware(BaseHTTPMiddleware):
    """Requires a configured header to be present and non-empty on every request,
    trusting whatever value a forward-auth proxy (Authentik, Traefik forward-auth, etc.)
    already set there rather than validating it against anything, Savepoint has no user
    database to check a value against.

    Presence-only: this has no signature or cryptographic binding to the proxy's own
    session. Protection depends entirely on the network guaranteeing that only the proxy,
    never a direct request, can reach this port. If direct access is ever possible,
    anyone can set this header themselves and authenticate as anyone. See
    FORWARD_AUTH_HEADER in .env.sample for the same caveat at the point it's configured.
    """

    def __init__(self, app, header_name: str):
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next):
        if not request.headers.get(self.header_name):
            return PlainTextResponse(
                f"missing required '{self.header_name}' header, forward-auth is enabled "
                "but this request wasn't authenticated by the proxy",
                status_code=401,
            )
        return await call_next(request)
