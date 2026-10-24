"""End-to-end check of Scam City on a live cluster (Chunk D "done when").

Builds a 5-resident town (real Nessie sandbox accounts, real model calls: well under $1), plays the rounds, and checks:
scams attempted and at least one stopped, a big payment waits for a human and runs once approved, the scoreboard and
balances add up, cost stays under $1, and the simulation bank is unusable outside a town.
Run from agent-platform/:  uv run --project runtime python deploy/e2e_chunk_d.py [rounds, default 2]
"""
import json
import sys
import time

import httpx

from e2e_chunk_a import kubectl, record, results
from e2e_chunk_b import PortForward, pod_reaches, secret_value, wait_until

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def main():
    """Run every check, then exit non-zero if any failed."""
    api = PortForward("platform", "platform-api", 80)
    client = httpx.Client(base_url=f"{api.url}/api", timeout=200,
                          headers={"Authorization": f"Bearer {secret_value('platform', 'platform-api', 'PLATFORM_TOKEN')}"})

    # 1. The simulation bank is fenced off from ordinary namespaces.
    kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "platform.hacktx.dev/v1", "kind": "Agent",
                                                   "metadata": {"name": "sim-sneak", "namespace": "team-demo"},
                                                   "spec": {"model": "fake", "instructions": "x", "tools": [{"name": "nessie-sim"}]}}))
    phase = wait_until(lambda: json.loads(kubectl("get", "agent", "sim-sneak", "-n", "team-demo", "-o", "json")).get("status", {}).get("phase") == "Failed", 30)
    record("operator refuses the simulation bank outside a town", phase)
    kubectl("delete", "agent", "sim-sneak", "-n", "team-demo")
    record("NetworkPolicy: a team-demo agent can't reach the simulation bank",
           not pod_reaches("team-demo", "platform.hacktx.dev/agent=bank-assistant", "tool-nessie-sim.platform.svc", 8000))

    # 2. Build a town: Nessie accounts + 8 agents, all Running.
    created = client.post("/towns", json={"residents": 5, "scammers": 2, "rounds": ROUNDS})
    town = created.json()
    record("build a 5-resident town (Nessie accounts opened)", created.status_code == 200, f"{town.get('namespace')}, estimate ${town.get('estimate_usd')}")
    town_id = town["id"]
    ready = wait_until(lambda: (lambda data: len(data["agents"]) == 8 and all(agent["phase"] == "Running" for agent in data["agents"]))(
        client.get(f"/towns/{town_id}").json()), 300)
    record("all 8 town agents Running", ready)
    record("town namespace is labeled as a simulation",
           kubectl("get", "ns", town["namespace"], "-o", "jsonpath={.metadata.labels.platform\\.hacktx\\.dev/simulation}") == "true")

    # 3. Play the rounds.
    started = client.post(f"/towns/{town_id}/run")
    record("start the rounds", started.status_code == 200, started.text[:100])
    finished = wait_until(lambda: client.get(f"/towns/{town_id}").json()["status"] in ("finished", "failed"), 60 * 4 * ROUNDS)
    data = client.get(f"/towns/{town_id}").json()
    record("rounds finish", finished and data["status"] == "finished", f"status {data['status']} {data.get('error') or ''}")
    score = data["score"]
    print(f"      score: {json.dumps(score)}")
    record("scammers made attempts (messages appear in the feed)", score["scam_attempts"] >= ROUNDS)
    record("at least one scam was stopped", score["scams_blocked"] >= 1, f"{score['scams_blocked']} of {score['scam_attempts']}")
    record("residents made legitimate payments", score["legit_payments"] >= 1, f"{score['legit_payments']}")
    record("cost under $1", score["cost_usd"] < 1, f"${score['cost_usd']} for {score['tokens']} tokens")

    # 4. A big payment waits for a human; approving it runs it and the scoreboard follows.
    pending = data["pending"]
    big = [item for item in pending if float(item["arguments"].get("amount", 0)) > 500]
    record("a payment over $500 waits for human approval", bool(big), f"{[(item['tool'], item['arguments'].get('amount')) for item in pending]}")
    if big:
        before = data["balances"]
        decided = client.post(f"/approvals/{big[0]['id']}", json={"decision": "approve"})
        steps = [step for step in decided.json().get("steps", []) if step.get("decision") == "approve"]
        record("approving it runs the payment", decided.status_code == 200 and steps and '"completed"' in steps[0]["result"],
               steps[0]["result"][:80] if steps else decided.text[:120])
        after = client.get(f"/towns/{town_id}").json()
        moved = round(sum(before.values()) - sum(after["balances"].values()), 2)
        record("balances reflect the approved payment", moved >= float(big[0]["arguments"]["amount"]) - 0.01, f"town total down ${moved}")

    # 5. Delete the town: its namespace and agents go; the score stays.
    client.delete(f"/towns/{town_id}")
    gone = wait_until(lambda: kubectl("get", "ns", town["namespace"], check=False).strip() == "", 120)
    kept = client.get(f"/towns/{town_id}").json()
    record("delete removes the town's namespace; its events and score stay", gone and kept["status"] == "deleted" and kept["events"])
    api.close()

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
