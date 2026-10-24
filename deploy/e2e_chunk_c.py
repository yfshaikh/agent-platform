"""End-to-end check of sign-in, roles and the voice relay on a live cluster (Chunk C).

Creates three throwaway users with random passwords, signs in as each, and checks what each role can and can't do.
Then checks the voice relay only lets a signed-in console through, and that agents can't reach the voice server.
Run from agent-platform/:  uv run --project api python deploy/e2e_chunk_c.py
"""
import asyncio
import hashlib
import secrets
import subprocess
import sys

import httpx
import websockets

from e2e_chunk_a import kubectl, record, results
from e2e_chunk_b import PortForward, pod_reaches, secret_value

USERS = {"e2e-viewer": "viewer", "e2e-builder": "builder", "e2e-approver": "approver"}


def psql(sql, user="postgres", password=None):
    """Run SQL in the platform database; returns psql's combined output."""
    postgres = kubectl("get", "pod", "-n", "platform", "-l", "app=postgres", "-o", "jsonpath={.items[0].metadata.name}")
    login = ["env", f"PGPASSWORD={password}", "psql", "-h", "127.0.0.1"] if password else ["psql"]
    completed = subprocess.run(["kubectl", "--context", "kind-f1", "-n", "platform", "exec", postgres, "--", *login,
                                "-U", user, "-d", "platform", "-tAc", sql], capture_output=True, text=True)
    return completed.stdout + completed.stderr


async def voice_handshake(url, cookie=None, origin=None):
    """Open /api/voice; returns 'open' if the relay accepted and stayed up briefly, else the close code."""
    headers = {**({"Cookie": f"session={cookie}"} if cookie else {}), **({"Origin": origin} if origin else {})}
    try:
        async with websockets.connect(url, additional_headers=headers, open_timeout=10) as socket:
            await asyncio.wait_for(socket.recv(), timeout=3)
            return "open"
    except asyncio.TimeoutError:
        return "open"  # accepted and waiting for audio: the call is up
    except websockets.ConnectionClosed as closed:
        return closed.rcvd.code if closed.rcvd else "closed"
    except websockets.InvalidStatus as refused:
        return refused.response.status_code


def main():
    """Run every check, then exit non-zero if any failed."""
    api = PortForward("platform", "platform-api", 80)
    admin = httpx.Client(base_url=f"{api.url}/api", timeout=60,
                         headers={"Authorization": f"Bearer {secret_value('platform', 'platform-api', 'PLATFORM_TOKEN')}"})
    passwords = {name: secrets.token_urlsafe(16) for name in USERS}
    for name, role in USERS.items():
        admin.post("/users", json={"name": name, "role": role, "password": passwords[name]}).raise_for_status()

    def sign_in(name, password=None):
        """A client holding that user's session cookie (or the failed response)."""
        client = httpx.Client(base_url=f"{api.url}/api", timeout=60)
        response = client.post("/login", json={"name": name, "password": password or passwords[name]})
        return client, response

    # 1. Signing in.
    _, wrong = sign_in("e2e-viewer", "not-the-password")
    record("a wrong password is refused", wrong.status_code == 401)
    _, nobody = sign_in("nobody-here", "whatever-password")
    record("an unknown name gets the same answer", nobody.status_code == 401 and nobody.json() == wrong.json())
    viewer, ok = sign_in("e2e-viewer")
    cookie = viewer.cookies.get("session")
    record("the right password signs in", ok.status_code == 200 and viewer.get("/me").json()["role"] == "viewer")
    record("the session cookie is HttpOnly and SameSite=Strict",
           "httponly" in ok.headers["set-cookie"].lower() and "samesite=strict" in ok.headers["set-cookie"].lower())
    record("the cookie is a fresh random token, not the platform token", cookie and cookie != secret_value("platform", "platform-api", "PLATFORM_TOKEN"))
    platform_token = secret_value("platform", "platform-api", "PLATFORM_TOKEN")
    record("the platform token doesn't work as a cookie", httpx.get(f"{api.url}/api/me", cookies={"session": platform_token}).status_code == 401)
    stored = psql(f"select count(*) from sessions where token_hash = '{hashlib.sha256(cookie.encode()).hexdigest()}'").strip()
    raw = psql(f"select count(*) from sessions where token_hash = '{cookie}'").strip()
    record("only the session's hash is stored", stored == "1" and raw == "0", f"hash rows {stored}, raw rows {raw}")

    # 2. Roles.
    template = admin.get("/templates").json()[0]
    record("viewer can look", viewer.get("/agents").status_code == 200)
    record("viewer can't draft", viewer.post("/agents/draft", json={"description": "x"}).status_code == 403)
    record("viewer can't deploy", viewer.post("/agents", json={"namespace": "team-demo", "name": "nope", "spec": template["spec"]}).status_code == 403)
    builder, _ = sign_in("e2e-builder")
    record("builder can draft (passes the role check)", builder.post("/agents/draft", json={"description": "x"}).status_code == 422)
    record("builder can't approve", builder.post("/approvals/anything", json={"decision": "approve"}).status_code == 403)
    record("builder can't 'deploy anyway'", builder.post("/agents", json={"namespace": "team-demo", "name": "nope", "spec": template["spec"],
                                                                          "deploy_anyway": True}).status_code == 403)
    record("builder can't manage people", builder.get("/users").status_code == 403)
    approver, _ = sign_in("e2e-approver")
    record("approver gets past the role check to decide", approver.post("/approvals/anything", json={"decision": "approve"}).status_code == 404)

    # 3. Sessions end.
    admin.post("/users", json={"name": "e2e-builder", "role": "viewer", "password": passwords["e2e-builder"]})
    record("changing someone's role signs them out", builder.get("/me").status_code == 401)
    approver.post("/logout")
    record("signing out ends the session", httpx.get(f"{api.url}/api/me", cookies={"session": approver.cookies.get("session") or "x"}).status_code == 401)
    for _ in range(5):
        sign_in("e2e-approver", "wrong-password")
    _, locked = sign_in("e2e-approver")
    record("five wrong passwords lock the name for a while", locked.status_code == 429)
    agent_password = secret_value("platform", "postgres", "AGENT_DB_PASSWORD")
    record("agents' database login can't read sessions or users",
           "permission denied" in psql("select * from sessions", "agent_runtime", agent_password)
           and "permission denied" in psql("select * from users", "agent_runtime", agent_password))
    actions = {(row["actor"], row["action"]) for row in admin.get("/audit").json()}
    record("sign-ins and user changes are audited", {("e2e-viewer", "logged_in"), ("admin", "user_saved")} <= actions)

    # 4. The voice relay.
    voice_url = api.url.replace("http", "ws") + "/api/voice"
    host = api.url.removeprefix("http://")
    record("voice relay refuses a call without a session", asyncio.run(voice_handshake(voice_url, origin=f"http://{host}")) in (4401, 403))
    record("voice relay refuses another site's page", asyncio.run(voice_handshake(voice_url, cookie, origin="http://evil.example")) in (4401, 403))
    voice_ready = kubectl("get", "deploy", "voice", "-n", "platform", "-o", "jsonpath={.status.readyReplicas}", check=False).strip() == "1"
    if voice_ready:
        record("voice relay connects a signed-in console", asyncio.run(voice_handshake(voice_url, cookie, origin=f"http://{host}")) == "open")
        record("agents can't reach the voice server directly",
               not pod_reaches("team-demo", "platform.hacktx.dev/agent=researcher", "voice.platform.svc", 7860))
    else:
        print("      skipped: the voice server isn't running (its Secret needs CARTESIA_API_KEY)")

    for name in USERS:
        admin.delete(f"/users/{name}")
    api.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
