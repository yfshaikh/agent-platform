"""The GPUs page: live readings, jobs and logs from each GPU node, plus incident buttons for demos and drills.

People see the same node agents the gpu-fleet tool uses (through the same SSH tunnel and token). Injecting an
incident or stopping a job here is a person's action, so it needs the right role and goes in the audit log.

Alerts: every 10 s the API reads each node. A job that crashed, or a GPU that stays hot, becomes one alert, sent as a chat
message to the agent subscribed to that node (spec.alerts: true). The agent investigates like it would for a person, so
any fix it proposes still waits for a human approval.
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from main import GROUP, PLATFORM_NAMESPACE, VERSION, agents_with_usage, audit, call_agent, can, db, k8s, kube, user

router = APIRouter(prefix="/api/gpu-nodes")
NODES = json.loads(os.environ.get("NODES", "{}"))  # connected nodes: name -> node agent URL (same as the gpu-fleet tool)
INCIDENTS = {  # what each button starts on the node (jobs from its fixed list)
    "out-of-memory": "a memory hog takes the GPU's memory, then a training job crashes with CUDA out of memory",
    "overheating": "a full-load job pushes power and temperature up",
    "crashing-job": "a training job crashes on its own after a minute",
}
JOB_NAME = re.compile(r"^job-[a-z-]+-[0-9a-f]{6}$")  # same rule as the node agent
running = set()  # incident and alert tasks (kept so they aren't garbage-collected)
ALERT_SECONDS = 10
# T4 on the Brev box: ~68°C idle, ~69°C training, 75-82°C at full load. It slows itself down at ~83°C.
HOT_CELSIUS, HOT_READINGS = 75, 3  # hot for 3 readings in a row (30 s)
STOPPED = {137, 143}  # exit codes of a job that was stopped (killed), not one that crashed
SCHEMA = """
create table if not exists alerts (
  id text primary key, at timestamptz default now(), node text, kind text, title text, agent text, namespace text,
  status text, tool text, arguments jsonb, trace_id text, answer text);
"""


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


def finished_recently(job, minutes=10):
    """True if the job ended in the last few minutes (so old failures don't alert when the API restarts)."""
    if job["finished_at"].startswith("0001"):
        return False
    ended = datetime.strptime(job["finished_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ended).total_seconds() < minutes * 60


def problems(node, jobs, gpus, hot):
    """Alerts in one reading of a node: (alert id, kind, title). The id makes each problem alert once."""
    found = [(f"{node}:{job['name']}", "job-failed", f"job {job['name']} ({job['template']}) failed with exit code {job['exit_code']}")
             for job in jobs if job["status"] == "exited" and job["exit_code"] not in (0, *STOPPED) and finished_recently(job)]
    for gpu in gpus:
        key = f"{node}/{gpu['index']}"
        hot[key] = hot.get(key, 0) + 1 if gpu["temperature.gpu"] >= HOT_CELSIUS else 0
        if hot[key] == HOT_READINGS:  # ponytail: one hot alert per GPU per 15 minutes, however long it stays hot
            found.append((f"{key}:hot:{int(time.time() // 900)}", "hot",
                          f"GPU {gpu['index']} has been at {gpu['temperature.gpu']}°C or more for 30 s (alert at {HOT_CELSIUS}°C)"))
    return found


async def wake(alert_id, node, title):
    """Send an alert to the agent subscribed to its node and record what the agent did."""
    subscribed = [agent for agent in await agents_with_usage() if agent["spec"].get("alerts") and agent["status"].get("phase") == "Running"
                  and any(entry["name"] == "gpu-fleet" and node in entry.get("nodes", []) for entry in agent["spec"].get("tools", []))]
    if not subscribed:
        await db().execute("update alerts set status='no-agent' where id=$1", alert_id)
        return
    agent = subscribed[0]  # ponytail: the first subscribed agent per node; fan out when there are on-call rotations
    await db().execute("update alerts set status='investigating', agent=$2, namespace=$3 where id=$1", alert_id, agent["name"], agent["namespace"])
    await audit("alerts", "alert_sent", f"{agent['namespace']}/{agent['name']}", {"alert_id": alert_id, "title": title})
    try:
        status, reply = await call_agent(agent["namespace"], agent["name"], "/chat",
                                         {"message": f"ALERT from {node}: {title}. Investigate and fix it."})
    except HTTPException as error:
        status, reply = error.status_code, {"answer": error.detail}
    await db().execute("update alerts set status=$2, tool=$3, arguments=$4, trace_id=$5, answer=$6 where id=$1", alert_id,
                       reply.get("status", "error") if status == 200 else "error", reply.get("tool"), reply.get("arguments"),
                       reply.get("trace_id"), (reply.get("answer") or str(reply.get("detail", "")))[:1000])


async def watch():
    """Read every connected node every 10 s and turn new problems into alerts. Started by the API at boot."""
    hot = {}
    while True:
        for node in NODES:
            try:
                jobs, gpus = await asyncio.gather(call_node(node, "GET", "/jobs"), call_node(node, "GET", "/gpus"))
                for alert_id, kind, title in problems(node, jobs, gpus, hot):
                    # The primary key makes each alert fire once, even with several API pods watching.
                    if await db().fetchval("insert into alerts (id, node, kind, title, status) values ($1, $2, $3, $4, 'new') "
                                           "on conflict do nothing returning id", alert_id, node, kind, title):
                        task = asyncio.create_task(wake(alert_id, node, title))
                        running.add(task)
                        task.add_done_callback(running.discard)
            except Exception as error:  # an unreachable node is shown on the GPUs page; the watcher keeps going
                print(f"alert watcher: {node}: {error}", flush=True)
        await asyncio.sleep(ALERT_SECONDS)


@router.get("/alerts")
async def alerts(actor=Depends(user)):
    """Recent alerts and what the agent did about each, newest first."""
    return [dict(row) for row in await db().fetch("select * from alerts order by at desc limit 30")]
