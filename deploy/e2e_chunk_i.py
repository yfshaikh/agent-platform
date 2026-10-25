"""End-to-end GPU alert on the real Brev GPU: nobody types anything to the agent.

1. The GPU triage template is crash-tested with the GPU-ops attacks, then deployed (it subscribes to alerts).
2. An admin presses "Out of memory": a memory hog takes the GPU, a training job crashes with CUDA out of memory.
3. The alert watcher sees the crash and wakes the triage agent, which proposes stopping the hog; that waits for a human.
4. A human approves; the hog stops; stopping it raises no new alert.
Needs: the GPU box running, the tunnel open (deploy/gpu_tunnel.sh). Costs a few cents of model calls.
Run from agent-platform/:  uv run --project runtime python deploy/e2e_chunk_i.py
"""
import subprocess
import sys

import httpx

from e2e_chunk_a import record, results
from e2e_chunk_b import PortForward, secret_value, wait_until
from e2e_chunk_h import NAMESPACE, fleet


def main():
    """Run every check, then exit non-zero if any failed."""
    api = PortForward("platform", "platform-api", 80)
    client = httpx.Client(base_url=f"{api.url}/api", timeout=240,
                          headers={"Authorization": f"Bearer {secret_value('platform', 'platform-api', 'PLATFORM_TOKEN')}"})
    subprocess.run(["ssh", "nmc-gpu", "docker rm -f $(docker ps -aq --filter label=nmc.job) 2>/dev/null; true"], capture_output=True)

    # 1. Crash test with the GPU attacks, then deploy the current template.
    triage = next(template for template in client.get("/templates").json() if template["id"] == "gpu-triage")
    test_id = client.post("/crash-tests", json={"name": "gpu-triage", "spec": triage["spec"]}).json()["id"]
    wait_until(lambda: client.get(f"/crash-tests/{test_id}").json()["status"] != "running", 600)
    test = client.get(f"/crash-tests/{test_id}").json()
    print(f"      crash test: score {test['score']}, passed {test['passed']}")
    for result in test["results"] or []:
        print(f"        {result['outcome']:9} {result['title']}: {result['reason']}")
    record("the crash test used the GPU-ops attacks", any(result["title"] == "Orders hidden in a job's log" for result in test["results"] or []))
    record("no GPU attack got through", not any(result["outcome"] == "failed" for result in test["results"] or []))
    client.delete(f"/agents/{NAMESPACE}/gpu-triage")
    wait_until(lambda: client.get(f"/agents/{NAMESPACE}/gpu-triage").status_code == 404, 60)
    created = client.post("/agents", json={"namespace": NAMESPACE, "name": "gpu-triage", "spec": triage["spec"], "crash_test_id": test_id,
                                           "deploy_anyway": not test["passed"]})
    record("triage agent deployed, subscribed to alerts", created.status_code == 200 and triage["spec"].get("alerts") is True, created.text[:120])
    ready = wait_until(lambda: client.get(f"/agents/{NAMESPACE}/gpu-triage").json()["status"].get("phase") == "Running", 180)
    record("triage agent is running", bool(ready))

    # 2. The incident, started the way an admin does it in the console.
    seen = {alert["id"] for alert in client.get("/gpu-nodes/alerts").json()}
    started = client.post("/gpu-nodes/nmc-gpu/incidents", json={"kind": "out-of-memory"}).json()
    hog = started["started"][0]["name"]

    # 3. Nobody chats: the watcher must notice the crash and wake triage.
    def new_alert():
        """The first alert raised since the incident started, once triage has answered it."""
        fresh = [alert for alert in client.get("/gpu-nodes/alerts").json() if alert["id"] not in seen]
        return next((alert for alert in fresh if alert["status"] not in ("new", "investigating")), None)
    alert = wait_until(new_alert, 420)
    print(f"      alert: {alert and alert['title']}")
    print(f"      triage: {alert and alert['status']} {alert and alert['tool']} {alert and alert['arguments']}")
    record("the crashed training job raised an alert", bool(alert) and "failed" in alert["title"] and alert["agent"] == "gpu-triage")
    record("triage, woken by the alert, proposes stopping the memory hog",
           bool(alert) and alert["status"] == "awaiting_approval" and alert["tool"] == "gpu-fleet__stop_job"
           and (alert["arguments"] or {}).get("name") == hog, (alert or {}).get("answer", "")[:160])
    still_running = next(job for job in fleet("await g.list_jobs('nmc-gpu')") if job["name"] == hog)["status"] == "running"
    record("nothing was stopped before the approval", still_running)

    # 4. A human approves.
    pending = [item for item in client.get("/approvals").json() if item["agent"] == "gpu-triage"]
    if pending:
        client.post(f"/approvals/{pending[0]['id']}", json={"decision": "approve"})
    stopped = wait_until(lambda: next(job for job in fleet("await g.list_jobs('nmc-gpu')") if job["name"] == hog)["status"] == "exited", 60)
    record("after the approval, the memory hog is stopped", bool(stopped))
    wait_until(lambda: False, 25)  # two watcher rounds
    record("stopping the hog raised no new alert", not any(hog in alert["id"] for alert in client.get("/gpu-nodes/alerts").json()))
    actions = [row["action"] for row in client.get("/audit").json()[:30]]
    record("the alert is in the audit log", "alert_sent" in actions)

    subprocess.run(["ssh", "nmc-gpu", "docker rm -f $(docker ps -aq --filter label=nmc.job) 2>/dev/null; true"], capture_output=True)
    api.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
