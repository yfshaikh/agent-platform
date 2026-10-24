"""Shared plumbing for every tool server: bearer-token check and startup.

Pattern from the earlier Muse test server: MCP 2.x, streamable HTTP, one token per tool server.
Only agents that list a tool get its token (the operator copies it into their Secret).
"""
import hmac
import logging
import os

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

# httpx logs every request URL at INFO, and Nessie/SerpApi put API keys in URLs: never let those reach logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def require_bearer_token(app, token):
    """Wrap an ASGI app so every HTTP request must carry `Authorization: Bearer <token>`."""
    expected_header = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        """Reject HTTP requests without the right token; pass everything else (like startup events) through."""
        if scope["type"] == "http" and not hmac.compare_digest(dict(scope["headers"]).get(b"authorization", b""), expected_header):
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"missing or wrong bearer token"})
            return
        await app(scope, receive, send)

    return guarded


def serve(mcp_server):
    """Run an MCP server on port 8000 behind the TOOL_TOKEN check."""
    # Agents reach tools by in-cluster DNS names, so the Host-header check is off; the token is the lock.
    app = mcp_server.streamable_http_app(
        stateless_http=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    uvicorn.run(require_bearer_token(app, os.environ["TOOL_TOKEN"]), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
