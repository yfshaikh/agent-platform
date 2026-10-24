"""Create the platform's Kubernetes Secrets from environment variables, without ever printing a value.

Run from agent-platform/:  uv run --env-file .env python deploy/sync_secrets.py [--context kind-f1]

API keys come from the environment. Tool tokens and the database password are generated once
and kept on later runs (so running this again doesn't break agents that already hold them).
"""
import base64
import json
import os
import secrets
import subprocess
import sys

CONTEXT = sys.argv[sys.argv.index("--context") + 1] if "--context" in sys.argv else "kind-f1"
NAMESPACE = "platform"
API_KEYS = ["OPENROUTER_API_KEY", "SERPAPI_API_KEY", "NESSIE_API_KEY"]


def kubectl(*arguments, stdin=None):
    """Run kubectl against CONTEXT; return stdout. Output is captured, never echoed."""
    result = subprocess.run(["kubectl", "--context", CONTEXT, *arguments], input=stdin, capture_output=True, text=True)
    return result.returncode, result.stdout


def existing_value(secret_name, key):
    """Read one value of an existing Secret (to keep generated values stable), or None if absent."""
    code, output = kubectl("get", "secret", secret_name, "-n", NAMESPACE, "-o", "json")
    if code != 0:
        return None
    encoded = json.loads(output).get("data", {}).get(key)
    return base64.b64decode(encoded).decode() if encoded else None


def apply_secret(secret_name, values):
    """Create or update a Secret through kubectl's stdin, so values never appear in arguments or output."""
    manifest = {
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": secret_name, "namespace": NAMESPACE},
        "data": {key: base64.b64encode(value.encode()).decode() for key, value in values.items()},
    }
    code, _ = kubectl("apply", "-f", "-", stdin=json.dumps(manifest))
    print(f"{'applied' if code == 0 else 'FAILED '} secret {secret_name} ({', '.join(values)})")


def stable_token(secret_name, key):
    """Keep an existing generated token, or make a new random one."""
    return existing_value(secret_name, key) or secrets.token_urlsafe(32)


if __name__ == "__main__":
    kubectl("create", "namespace", NAMESPACE)
    missing = [name for name in API_KEYS if not os.environ.get(name)]
    if missing:
        print(f"warning: not set, tools needing them will fail: {', '.join(missing)}")
    password = stable_token("postgres", "POSTGRES_PASSWORD")
    apply_secret("postgres", {"POSTGRES_PASSWORD": password})
    apply_secret("platform-keys", {
        **{name: os.environ[name] for name in API_KEYS if os.environ.get(name)},
        "DATABASE_URL": f"postgresql://postgres:{password}@postgres.platform.svc:5432/platform",
    })
    for tool in ["nessie", "serpapi"]:
        apply_secret(f"tool-{tool}-token", {"TOOL_TOKEN": stable_token(f"tool-{tool}-token", "TOOL_TOKEN")})
