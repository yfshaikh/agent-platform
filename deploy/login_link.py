"""Print a one-time login link for the web console (works once, for five minutes).

Run from agent-platform/:  uv run --project api python deploy/login_link.py [console port, default 8080]
The console must be reachable on that port, e.g.:  kubectl --context kind-f1 -n platform port-forward svc/platform-api 8080:80
The login token is read from the cluster and used here, never printed.
"""
import base64
import json
import socket
import subprocess
import sys
import time

import httpx

CONTEXT = "kind-f1"
console_port = sys.argv[1] if len(sys.argv) > 1 else "8080"

secret = json.loads(subprocess.run(["kubectl", "--context", CONTEXT, "-n", "platform", "get", "secret", "platform-api", "-o", "json"],
                                   capture_output=True, text=True, check=True).stdout)
token = base64.b64decode(secret["data"]["PLATFORM_TOKEN"]).decode()

with socket.socket() as probe:  # a private port-forward, so this works whether or not the console's is running
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
forward = subprocess.Popen(["kubectl", "--context", CONTEXT, "-n", "platform", "port-forward", "svc/platform-api", f"{port}:80"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            response = httpx.post(f"http://127.0.0.1:{port}/api/login-links", headers={"Authorization": f"Bearer {token}"}, timeout=3)
            break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        sys.exit("platform API not reachable")
    response.raise_for_status()
    print(f"http://localhost:{console_port}{response.json()['path']}")
finally:
    forward.terminate()
