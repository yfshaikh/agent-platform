"""End-to-end GPU incident on the real Brev GPU (Chunk H "done when").

1. A memory-hog job grabs the GPU's memory; then a training job crashes with a real CUDA out-of-memory error.
2. The GPU triage agent is told "a job failed". It must find the cause and propose stopping the memory hog,
   which pauses for a human approval (stop_job is sensitive).
3. A human approves; the tool server checks the signed grant and the node stops the job; GPU memory is freed.
Needs: the GPU box running, the tunnel open (deploy/gpu_tunnel.sh). Costs a few cents of model calls.
Run from agent-platform/:  uv run --project runtime python deploy/e2e_chunk_h.py
"""
import json
import subprocess
import sys
import time

import httpx

from e2e_chunk_a import kubectl, record, results
from e2e_chunk_b import PortForward, secret_value, wait_until

NAMESPACE = "team-demo"


def fleet(code):
    """Run Python against the gpu-fleet tool inside its pod (the test harness acting as the GPU's other users)."""
    script = f"import asyncio, json, gpu_fleet_server as g\nasync def main():\n    return {code}\nprint(json.dumps(asyncio.run(main())))"
    return json.loads(kubectl("exec", "-n", "platform", "deploy/tool-gpu-fleet", "--", "python", "-c", script))


def main():
    """Run every check, then exit non-zero if any failed."""
    api = PortForward("platform", "platform-api", 80)
    client = httpx.Client(base_url=f"{api.url}/api", timeout=240,
                          headers={"Authorization": f"Bearer {secret_value('platform', 'platform-api', 'PLATFORM_TOKEN')}"})

    # 0. A clean node, and the triage agent deployed (it can stop jobs, so the crash-test gate applies).
    subprocess.run(["ssh", "nmc-gpu", "docker rm -f $(docker ps -aq --filter label=nmc.job) 2>/dev/null; true"], capture_output=True)
    record("the real GPU answers", fleet("await g.list_gpus('nmc-gpu')")["gpus"][0]["name"].startswith(("Tesla", "NVIDIA")))
    triage = next(template for template in client.get("/templates").json() if template["id"] == "gpu-triage")
    if client.get(f"/agents/{NAMESPACE}/gpu-triage").status_code == 404:
        test_id = client.post("/crash-tests", json={"name": "gpu-triage", "spec": triage["spec"]}).json()["id"]
        wait_until(lambda: client.get(f"/crash-tests/{test_id}").json()["status"] != "running", 600)
        test = client.get(f"/crash-tests/{test_id}").json()
        print(f"      crash test: score {test['score']}, passed {test['passed']}")
        created = client.post("/agents", json={"namespace": NAMESPACE, "name": "gpu-triage", "spec": triage["spec"], "crash_test_id": test_id,
                                               "deploy_anyway": not test["passed"]})
        record("triage agent deployed through the crash-test gate", created.status_code == 200, created.text[:120])
    ready = wait_until(lambda: client.get(f"/agents/{NAMESPACE}/gpu-triage").json()["status"].get("readyReplicas"), 180)
    record("triage agent is running", bool(ready))

    # 1. The incident: a memory hog, then a training job that can't get memory.
    hog = fleet("await g.start_job('nmc-gpu', 'memory-hog')")["name"]
    held = wait_until(lambda: fleet("await g.list_gpus('nmc-gpu')")["gpus"][0]["memory.used"] > 12000, 180)
    record("memory hog holds most of the GPU's memory", bool(held))
    train = fleet("await g.start_job('nmc-gpu', 'train')")["name"]
    crashed = wait_until(lambda: next(job for job in fleet("await g.list_jobs('nmc-gpu')") if job["name"] == train)["status"] == "exited", 180)
    logs = fleet(f"await g.job_logs('nmc-gpu', '{train}')")["logs"]
    record("training job crashed with a real CUDA out-of-memory error", bool(crashed) and "out of memory" in logs.lower(), logs[-160:])

    # 2. Triage.
    reply = client.post(f"/agents/{NAMESPACE}/gpu-triage/chat",
                        json={"message": f"Alert from nmc-gpu: job {train} failed. Investigate and fix it."}).json()
    tools_used = [step.get("name") for step in reply.get("steps", []) if step["type"] == "tool"]
    print(f"      triage used: {tools_used}")
    print(f"      triage asked for: {reply.get('tool')} {reply.get('arguments')}")
    record("triage read the GPU, the jobs and the failed job's logs",
           {"gpu-fleet__list_gpus", "gpu-fleet__list_jobs", "gpu-fleet__job_logs"} <= set(tools_used))
    record("triage proposes stopping the memory hog, and it pauses for a human",
           reply.get("status") == "awaiting_approval" and reply.get("tool") == "gpu-fleet__stop_job"
           and (reply.get("arguments") or {}).get("name") == hog, f"{reply.get('status')} {reply.get('answer') or ''}"[:160])
    still_running = next(job for job in fleet("await g.list_jobs('nmc-gpu')") if job["name"] == hog)["status"] == "running"
    record("nothing was stopped before the approval", still_running)

    # 3. A human approves.
    pending = [item for item in client.get("/approvals").json() if item["agent"] == "gpu-triage"]
    if pending:
        decided = client.post(f"/approvals/{pending[0]['id']}", json={"decision": "approve"}).json()
        print(f"      after approval: {decided.get('answer', decided)[:300]}")
    stopped = wait_until(lambda: next(job for job in fleet("await g.list_jobs('nmc-gpu')") if job["name"] == hog)["status"] == "exited", 60)
    record("after the approval, the memory hog is stopped", bool(stopped))
    freed = wait_until(lambda: fleet("await g.list_gpus('nmc-gpu')")["gpus"][0]["memory.used"] < 2000, 60)
    record("GPU memory is free again", bool(freed))
    actions = [(row["action"], (row["detail"] or {}).get("tool")) for row in client.get("/audit").json()[:20]]
    record("the approval is in the audit log", ("approval_approve", "gpu-fleet__stop_job") in actions)

    subprocess.run(["ssh", "nmc-gpu", "docker rm -f $(docker ps -aq --filter label=nmc.job) 2>/dev/null; true"], capture_output=True)
    api.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
