"""Create the platform's Kubernetes Secrets from environment variables, without ever printing a value.

Run from agent-platform/:  uv run --no-project --with cryptography --env-file .env python deploy/sync_secrets.py [--context kind-f1]

API keys come from the environment. Tool tokens, database passwords, the console login token and the
approval signing key are generated once and kept on later runs (so agents already holding them keep working).

Who gets what:
  platform-keys  model/search/bank keys, the *agent* database login, the approval public key, the per-agent key master
                 (the operator copies parts to agents; each agent gets only its own derived key, never the master)
  platform-api   the database owner login, the approval private key, the console login token (only the API reads it)
"""
import base64
import json
import os
import secrets
import subprocess
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

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


def signing_keys():
    """The approval signing key pair (base64 raw Ed25519): kept if it exists, else generated."""
    existing = existing_value("platform-api", "APPROVAL_PRIVATE_KEY")
    private_key = (Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(existing)) if existing
                   else Ed25519PrivateKey.generate())
    private_raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.urlsafe_b64encode(private_raw).decode(), base64.urlsafe_b64encode(public_raw).decode()


if __name__ == "__main__":
    kubectl("create", "namespace", NAMESPACE)
    missing = [name for name in API_KEYS if not os.environ.get(name)]
    if missing:
        print(f"warning: not set, tools needing them will fail: {', '.join(missing)}")
    password = stable_token("postgres", "POSTGRES_PASSWORD")
    agent_password = stable_token("postgres", "AGENT_DB_PASSWORD")
    apply_secret("postgres", {"POSTGRES_PASSWORD": password, "AGENT_DB_PASSWORD": agent_password})
    private_key, public_key = signing_keys()
    # Master for per-agent keys: each agent stamps its approval requests with HMAC(master, "<namespace>/<name>"),
    # so one agent can't write approvals that pass as another's. The operator and API hold it; agents only get their own key.
    agent_key_master = stable_token("platform-keys", "AGENT_KEY_MASTER")
    apply_secret("platform-keys", {
        **{name: os.environ[name] for name in API_KEYS if os.environ.get(name)},
        "DATABASE_URL": f"postgresql://agent_runtime:{agent_password}@postgres.platform.svc:5432/platform",
        "APPROVAL_PUBLIC_KEY": public_key,
        "AGENT_KEY_MASTER": agent_key_master,
    })
    apply_secret("platform-api", {
        **({"OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]} if os.environ.get("OPENROUTER_API_KEY") else {}),
        "ADMIN_DATABASE_URL": f"postgresql://postgres:{password}@postgres.platform.svc:5432/platform",
        "AGENT_DB_PASSWORD": agent_password,
        "APPROVAL_PRIVATE_KEY": private_key,
        "AGENT_KEY_MASTER": agent_key_master,
        "PLATFORM_TOKEN": stable_token("platform-api", "PLATFORM_TOKEN"),
        # Scam City: the API opens a Nessie account per town resident (never logged: the key travels in the URL)
        **({"NESSIE_API_KEY": os.environ["NESSIE_API_KEY"]} if os.environ.get("NESSIE_API_KEY") else {}),
    })
    for tool in ["nessie", "serpapi", "nessie-sim"]:
        apply_secret(f"tool-{tool}-token", {"TOOL_TOKEN": stable_token(f"tool-{tool}-token", "TOOL_TOKEN")})
