"""The GPUs page: live readings, jobs and logs from each GPU node, plus incident buttons for demos and drills.

People see the same node agents the gpu-fleet tool uses (through the same SSH tunnel and token). Injecting an
incident or stopping a job here is a person's action, so it needs the right role and goes in the audit log.
"""
import asyncio
import json
import os
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from main import GROUP, PLATFORM_NAMESPACE, VERSION, audit, can, k8s, kube, user

router = APIRouter(prefix="/api/gpu-nodes")
NODES = json.loads(os.environ.get("NODES", "{}"))  # connected nodes: name -> node agent URL (same as the gpu-fleet tool)
INCIDENTS = {  # what each button starts on the node (jobs from its fixed list)
    "out-of-memory": "a memory hog takes the GPU's memory, then a training job crashes with CUDA out of memory",
    "overheating": "a full-load job pushes power and temperature up",
    "crashing-job": "a training job crashes on its own after a minute",
}
JOB_NAME = re.compile(r"^job-[a-z-]+-[0-9a-f]{6}$")  # same rule as the node agent
running = set()  # incident tasks (kept so they aren't garbage-collected)


async def call_node(node, method, path, body=None):
    """One call to a node agent; 404 for unknown nodes, 502 if it can't be reached."""
    if node not in NODES:
        raise HTTPException(404, f"no connected GPU node {node!r}")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, NODES[node] + path, json=body,
                                            headers={"Authorization": f"Bearer {os.environ['NODE_TOKEN']}"})
    except httpx.HTTPError as error:
        raise HTTPException(502, f"GPU node {node} unreachable ({type(error).__name__}); is the tunnel up?") from None
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.json().get("error", "node error"))
    return response.json()


def job_path(name, action):
    """/jobs/<name>/<action> for a real job name only."""
    if not JOB_NAME.fullmatch(name):
        raise HTTPException(400, "not a job name")
    return f"/jobs/{name}/{action}"


@router.get("")
async def list_nodes(actor=Depends(user)):
    """Every node in the gpu-fleet registry, with live readings for the connected ones."""
    tool = await k8s(kube["custom"].get_namespaced_custom_object, GROUP, VERSION, PLATFORM_NAMESPACE, "tools", "gpu-fleet")

    async def describe(entry):
        """One registry entry plus its live state."""
        if entry["name"] not in NODES:
            return {**entry, "connected": False}
        try:
            gpus, status = await asyncio.gather(call_node(entry["name"], "GET", "/gpus"), call_node(entry["name"], "GET", "/node"))
            return {**entry, "connected": True, "reachable": True, "gpus": gpus, "cordoned": status["cordoned"]}
        except HTTPException as error:
            return {**entry, "connected": True, "reachable": False, "error": error.detail}

    return await asyncio.gather(*(describe(entry) for entry in tool["spec"].get("nodes", [])))


@router.get("/{node}/jobs")
async def jobs(node: str, actor=Depends(user)):
    """The node's jobs, newest first."""
    return sorted(await call_node(node, "GET", "/jobs"), key=lambda job: job["started_at"], reverse=True)


@router.get("/{node}/jobs/{name}/logs")
async def logs(node: str, name: str, actor=Depends(user)):
    """The last 40 lines a job printed."""
    return await call_node(node, "GET", job_path(name, "logs"))


@router.post("/{node}/jobs/{name}/stop")
async def stop(node: str, name: str, actor=Depends(can("approve"))):
    """A person stops a job directly (agents need an approval for this; approvers may just do it)."""
    result = await call_node(node, "POST", job_path(name, "stop"))
    await audit(actor, "gpu_job_stopped", f"{node}/{name}", {})
    return result


class Incident(BaseModel):
    """Body of POST /gpu-nodes/{node}/incidents."""
    kind: str


@router.post("/{node}/incidents")
async def inject(node: str, body: Incident, actor=Depends(can("admin"))):
    """Start a real incident on a node, for a demo or a drill. Audited."""
    if body.kind not in INCIDENTS:
        raise HTTPException(400, f"unknown incident; choose from {sorted(INCIDENTS)}")
    if body.kind == "overheating":
        started = [await call_node(node, "POST", "/jobs", {"template": "burn"})]
    elif body.kind == "crashing-job":
        started = [await call_node(node, "POST", "/jobs", {"template": "crashy-train"})]
    else:
        started = [await call_node(node, "POST", "/jobs", {"template": "memory-hog"})]

        async def then_train():
            """Once the hog holds the memory (it takes ~20 s to start), start the job that will crash."""
            for _ in range(60):
                gpus = await call_node(node, "GET", "/gpus")
                if gpus[0]["memory.used"] > 0.9 * gpus[0]["memory.total"]:
                    await call_node(node, "POST", "/jobs", {"template": "train"})
                    return
                await asyncio.sleep(3)

        task = asyncio.create_task(then_train())
        running.add(task)
        task.add_done_callback(running.discard)
    await audit(actor, "incident_injected", node, {"kind": body.kind, "jobs": [job["name"] for job in started]})
    return {"kind": body.kind, "what": INCIDENTS[body.kind], "started": started}
