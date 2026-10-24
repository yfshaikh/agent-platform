"""Shared plumbing for every tool server: bearer-token check, approval-grant check, and startup.

Pattern from the earlier Muse test server: MCP 2.x, streamable HTTP, one token per tool server.
Only agents that list a tool get its token (the operator copies it into their Secret).

The token alone isn't enough for sensitive functions (moving money): every such call must also carry a
grant signed by the platform API for exactly that call, usable once. So an agent holding the token
(or anyone who steals it) still can't skip the human approval.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from mcp.server.transport_security import TransportSecuritySettings

# httpx logs every request URL at INFO, and Nessie/SerpApi put API keys in URLs: never let those reach logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


used_grants = set()  # ponytail: in memory, so a restart forgets them; grants expire after 5 minutes, which bounds replay


def verify_grant(grant):
    """Claims of a platform-signed approval grant, or None if it's forged, malformed or expired (same as runtime/agent.py)."""
    try:
        body, signature = grant.split(".")
        public_key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(os.environ["APPROVAL_PUBLIC_KEY"]))
        public_key.verify(base64.urlsafe_b64decode(signature), body.encode())
        claims = json.loads(base64.urlsafe_b64decode(body))
    except Exception:  # bad base64, bad signature, bad JSON, no key: all just "not a valid grant"
        return None
    return claims if claims.get("exp", 0) > time.time() else None


def arguments_hash(arguments):
    """Fingerprint of a call's arguments; the API puts the same one in the grant."""
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def grant_problem(message, grant, sensitive):
    """Why this JSON-RPC message may not run, or None if it may. Only tools/call of a sensitive function needs a grant."""
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    params = message.get("params") or {}
    if params.get("name") not in sensitive:
        return None
    claims = verify_grant(grant)
    if not claims or claims.get("decision") != "approve":
        return f"{params.get('name')} needs a platform-signed human approval"
    if claims.get("tool", "").rsplit("__", 1)[-1] != params["name"] or claims.get("args_hash") != arguments_hash(params.get("arguments") or {}):
        return "the approval is for a different call"
    if claims["approval_id"] in used_grants:
        return "this approval was already used"
    used_grants.add(claims["approval_id"])
    return None


def guard(app, token, sensitive=frozenset()):
    """Wrap an ASGI app: every HTTP request needs `Authorization: Bearer <token>`; sensitive calls also need a grant."""
    expected_header = f"Bearer {token}".encode()

    async def refuse(send, status, text):
        """Send a plain-text error response."""
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": text.encode()})

    async def guarded(scope, receive, send):
        """Check the token, then (for POSTs) read the body to find tool calls; pass everything else through."""
        if scope["type"] != "http":
            return await app(scope, receive, send)
        headers = dict(scope["headers"])
        if not hmac.compare_digest(headers.get(b"authorization", b""), expected_header):
            return await refuse(send, 401, "missing or wrong bearer token")
        if scope["method"] != "POST" or not sensitive:
            return await app(scope, receive, send)
        body, more = b"", True
        while more:
            chunk = await receive()
            body, more = body + chunk.get("body", b""), chunk.get("more_body", False)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        grant = headers.get(b"x-approval-grant", b"").decode()
        for message in payload if isinstance(payload, list) else [payload]:
            problem = grant_problem(message, grant, sensitive)
            if problem:
                return await refuse(send, 403, problem)
        replayed = False

        async def replay():
            """Hand the already-read body to the app once, then behave like the original receive."""
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await app(scope, replay, send)

    return guarded


def serve(mcp_server, sensitive=frozenset()):
    """Run an MCP server on port 8000 behind the TOOL_TOKEN check (and grant check for `sensitive` functions)."""
    # Agents reach tools by in-cluster DNS names, so the Host-header check is off; the token is the lock.
    app = mcp_server.streamable_http_app(
        stateless_http=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    uvicorn.run(guard(app, os.environ["TOOL_TOKEN"], sensitive), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
