"""Offline check of the approval-grant gate on sensitive tool calls.  Run: uv run python test_grants.py"""
import base64
import json
import os
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import common

platform_key, attacker_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
os.environ["APPROVAL_PUBLIC_KEY"] = base64.urlsafe_b64encode(
    platform_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def sign(claims, key=platform_key):
    """A grant the way the platform API makes one."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return body + "." + base64.urlsafe_b64encode(key.sign(body.encode())).decode()


arguments = {"from_account_id": "a", "to_account_id": "b", "amount": 20.0, "description": "rent"}
call = {"method": "tools/call", "params": {"name": "transfer_money", "arguments": arguments}}
claims = {"approval_id": "ap1", "tool": "nessie__transfer_money", "args_hash": common.arguments_hash(arguments),
          "decision": "approve", "exp": time.time() + 60}
sensitive = {"transfer_money"}

assert common.grant_problem({"method": "tools/call", "params": {"name": "get_balance"}}, "", sensitive) is None
assert "needs a platform-signed" in common.grant_problem(call, "", sensitive)
assert "needs a platform-signed" in common.grant_problem(call, sign(claims, attacker_key), sensitive)
assert "needs a platform-signed" in common.grant_problem(call, sign({**claims, "exp": time.time() - 1}), sensitive)
assert "needs a platform-signed" in common.grant_problem(call, sign({**claims, "decision": "deny"}), sensitive)
changed = {**call, "params": {"name": "transfer_money", "arguments": {**arguments, "amount": 2000.0}}}
assert "different call" in common.grant_problem(changed, sign(claims), sensitive)
assert common.grant_problem(call, sign(claims), sensitive) is None
assert "already used" in common.grant_problem(call, sign(claims), sensitive)
print("grant checks passed")
