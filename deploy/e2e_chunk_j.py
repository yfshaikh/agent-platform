"""End-to-end GPU incident worked by a team of agents, on the real Brev GPU. Nobody types anything to an agent.

1. The fixer and the triage agent are deployed (triage crash-tested first; it may message the fixer).
2. An admin presses "Out of memory": a memory hog takes the GPU, a training job crashes with CUDA out of memory.
3. The alert wakes triage. It reads the GPU, the logs and the runbook, hands its diagnosis to the fixer, and proposes
   stopping the hog (paused for a human). The fixer reads the runbook and the node's code and writes a prevention diff.
4. With PAGE=1, the platform phones the on-call engineer (Cartesia). Otherwise paging is off and no call is placed.
5. A human approves; the hog stops.
Needs: the GPU box running, the tunnel open. Costs a few cents of model calls (and one short call with PAGE=1).
Run from agent-platform/:  [PAGE=1] uv run --project runtime python deploy/e2e_chunk_j.py
"""
import os
import subprocess
import sys

import httpx

from e2e_chunk_a import record, results
from e2e_chunk_b import PortForward, secret_value, wait_until
from e2e_chunk_h import NAMESPACE, fleet

PAGE = os.environ.get("PAGE") == "1"


def deploy(client, template_id, crash_test=False):
    """(Re)deploy a template into the demo namespace; with crash_test, through the crash-test gate."""
    template = next(item for item in client.get("/templates").json() if item["id"] == template_id)
    body = {"namespace": NAMESPACE, "name": template["name"], "spec": template["spec"]}
    if crash_test:
        test_id = client.post("/crash-tests", json={"name": template["name"], "spec": template["spec"]}).json()["id"]
        wait_until(lambda: client.get(f"/crash-tests/{test_id}").json()["status"] != "running", 600)
        test = client.get(f"/crash-tests/{test_id}").json()
        print(f"      {template['name']} crash test: score {test['score']}, passed {test['passed']}, "
              f"{sum(result['outcome'] == 'failed' for result in test['results'] or [])} failed")
        body |= {"crash_test_id": test_id, "deploy_anyway": not test["passed"]}
    client.delete(f"/agents/{NAMESPACE}/{template['name']}")
    wait_until(lambda: client.get(f"/agents/{NAMESPACE}/{template['name']}").status_code == 404, 60)
    created = client.post("/agents", json=body)
    ready = wait_until(lambda: client.get(f"/agents/{NAMESPACE}/{template['name']}").json()["status"].get("phase") == "Running", 180)
    return created.status_code == 200 and bool(ready), created.text[:120]


def main():
    """Run every check, then exit non-zero if any failed."""
    api = PortForward("platform", "platform-api", 80)
    client = httpx.Client(base_url=f"{api.url}/api", timeout=240,
                          headers={"Authorization": f"Bearer {secret_value('platform', 'platform-api', 'PLATFORM_TOKEN')}"})
    subprocess.run(["ssh", "nmc-gpu", "docker rm -f $(docker ps -aq --filter label=nmc.job) 2>/dev/null; true"], capture_output=True)
    paging = client.put("/gpu-nodes/paging", json={"on": PAGE}).json()
    record("paging is set up" if PAGE else "paging is off for this run", paging["on"] == PAGE and (paging["ready"] or not PAGE))

    # 1. The team.
    record("fixer deployed and running", *deploy(client, "gpu-fixer"))
    record("triage deployed through the crash-test gate and running", *deploy(client, "gpu-triage", crash_test=True))

    # 2. The incident.
    seen = {alert["id"] for alert in client.get("/gpu-nodes/alerts").json()}
    hog = client.post("/gpu-nodes/nmc-gpu/incidents", json={"kind": "out-of-memory"}).json()["started"][0]["name"]

    # 3. Nobody chats: the alert wakes triage, which brings in the fixer.
    def settled():
        """The first new alert, once triage has answered it and the paging step has run."""
        fresh = [alert for alert in client.get("/gpu-nodes/alerts").json() if alert["id"] not in seen]
        return next((alert for alert in fresh if alert["status"] not in ("new", "investigating") and alert["paged"]), None)
    alert = wait_until(settled, 480)
    record("the crash raised an alert that woke triage", bool(alert) and "failed" in alert["title"] and alert["agent"] == "gpu-triage")
    incident = client.get(f"/gpu-nodes/alerts/{alert['id']}").json() if alert else {"steps": [], "approvals": []}
    steps = {step["agent"]: [tool["name"] for tool in step["tools"]] for step in incident["steps"]}
    print(f"      triage used: {steps.get('gpu-triage')}")
    print(f"      fixer used:  {steps.get('gpu-fixer')}")
    record("triage read the runbooks", any(name.startswith("runbooks__") for name in steps.get("gpu-triage", [])))
    record("triage handed its diagnosis to the fixer", "platform__send_message" in steps.get("gpu-triage", []) and "gpu-fixer" in steps)
    record("the fixer read the runbooks and the node's code",
           any(name.startswith("runbooks__") for name in steps.get("gpu-fixer", [])) and "gpu-fleet__read_source" in steps.get("gpu-fixer", []))
    diff = incident.get("prevention_diff") or ""
    print("      prevention diff:\n" + "\n".join("        " + line for line in diff.splitlines()[:25]))
    record("the fixer wrote a prevention diff against NMC²'s code", "node_agent.py" in diff and any(line.startswith("+") for line in diff.splitlines()))
    fix = (incident["approvals"] or [{}])[0]
    record("triage proposes stopping the memory hog, waiting for a human",
           fix.get("status") == "pending" and fix.get("tool") == "gpu-fleet__stop_job" and fix.get("arguments", {}).get("name") == hog, str(fix)[:160])
    print(f"      on-call: {alert and alert['paged']}")
    record("a call to the on-call engineer was placed" if PAGE else "nobody was phoned with paging off",
           bool(alert) and (alert["paged"].startswith("call placed") if PAGE else "paging is off" in alert["paged"]))

    # 5. A human approves.
    if fix.get("id"):
        client.post(f"/approvals/{fix['id']}", json={"decision": "approve"})
    stopped = wait_until(lambda: next(job for job in fleet("await g.list_jobs('nmc-gpu')") if job["name"] == hog)["status"] == "exited", 60)
    record("after the approval, the memory hog is stopped", bool(stopped))

    client.put("/gpu-nodes/paging", json={"on": False})
    subprocess.run(["ssh", "nmc-gpu", "docker rm -f $(docker ps -aq --filter label=nmc.job) 2>/dev/null; true"], capture_output=True)
    api.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
