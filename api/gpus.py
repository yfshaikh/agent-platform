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
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from main import GROUP, PLATFORM_NAMESPACE, VERSION, agents_with_usage, audit, call_agent, can, db, k8s, kube, user, verified_request

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
alter table alerts add column if not exists paged text;
alter table alerts add column if not exists paged_at timestamptz;
alter table alerts add column if not exists approval_id text;
create table if not exists settings (key text primary key, value jsonb);
"""
CARTESIA_CALLS = "https://api.cartesia.ai/agents/calls"
DIFF = re.compile(r"```diff\n(.*?)```", re.DOTALL)
PAGE_EVERY_SECONDS = 300  # at most one phone call per 5 minutes, however many alerts fire


def wait_starts(seconds):
    """SQL for the moment calls stop holding up the next one: `seconds` ago, or the last timer reset, whichever is later."""
    return (f"greatest(now() - make_interval(secs => {seconds}), "
            "coalesce((select (value #>> '{}')::timestamptz from settings where key='paging_reset_at'), '-infinity'))")


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
    await db().execute("update alerts set status=$2, tool=$3, arguments=$4, trace_id=$5, answer=$6, approval_id=$7 where id=$1", alert_id,
                       reply.get("status", "error") if status == 200 else "error", reply.get("tool"), reply.get("arguments"),
                       reply.get("trace_id"), (reply.get("answer") or str(reply.get("detail", "")))[:1000],
                       reply.get("approval_id") if status == 200 else None)
    if status == 200:
        await page(alert_id, node, title, reply)


async def paging_on():
    """Whether alerts phone the on-call engineer (an admin switch on the GPUs page; off until someone turns it on)."""
    return bool(await db().fetchval("select value from settings where key='paging'"))


def spoken(text, limit=500):
    """Agent-written text made safe to read out: markdown, links and phone-number-like digit runs removed, one line, capped.

    It stays untrusted (it can quote a customer's job log), which is why the phone agent can't do anything but talk.
    """
    text = re.sub(r"https?://\S+|www\.\S+", "[link removed]", text)
    text = re.sub(r"\+?\d[\d\s().-]{6,}\d", "[number removed]", text)
    return re.sub(r"\s+", " ", re.sub(r"[*`#_>|\[\]]+", "", text)).strip()[:limit]


async def claim_page_slot(alert_id):
    """Take the one call allowed per 5 minutes, atomically (alerts can fire together, and several API pods may watch)."""
    async with db().acquire() as connection, connection.transaction():
        await connection.execute("select pg_advisory_xact_lock(4242)")  # one claimer at a time
        return await connection.fetchval(
            "update alerts set paged_at=now(), paged='calling the on-call engineer…' where id=$1 and not exists "
            f"(select 1 from alerts where paged_at > {wait_starts('$2')}) returning id", alert_id, PAGE_EVERY_SECONDS)


async def page(alert_id, node, title, reply):
    """Phone the on-call engineer through Cartesia with what triage found. The phone agent only explains: it has no way
    into the platform, so nothing said on a call can approve or change anything. The number is never logged or shown."""
    phone = os.environ.get("ONCALL_PHONE")
    if not await paging_on():
        result = "not called: paging is off"
    elif not (phone and os.environ.get("CARTESIA_API_KEY") and os.environ.get("CARTESIA_PAGER_AGENT_ID") and os.environ.get("CARTESIA_FROM_NUMBER_ID")):
        result = "not called: paging isn't set up (see the README)"
    elif not await claim_page_slot(alert_id):
        result = "not called: someone was paged in the last 5 minutes"
    else:
        arguments = {key: value for key, value in (reply.get("arguments") or {}).items() if key != "reason"}
        waiting = reply.get("status") == "awaiting_approval"
        variables = {
            "node": node, "alert": spoken(title, 200),
            "diagnosis": "Summary written by the triage agent: " + spoken(
                reply.get("answer") or (reply.get("arguments") or {}).get("reason") or "no diagnosis yet"),
            "proposed_fix": spoken(f"{(reply.get('tool') or '').split('__')[-1].replace('_', ' ')} "
                                   + ", ".join(f"{key} {value}" for key, value in arguments.items()), 200) if waiting else "none",
            "status": "waiting for a human approval in the NMC² console" if waiting else spoken(str(reply.get("status")), 100),
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(CARTESIA_CALLS, json={
                    "from_number_id": os.environ["CARTESIA_FROM_NUMBER_ID"], "agent_id": os.environ["CARTESIA_PAGER_AGENT_ID"],
                    "ringing_timeout_seconds": 45, "outbound_calls": [{"to_number": phone, "dynamic_variables": variables}]},
                    headers={"X-API-Key": os.environ["CARTESIA_API_KEY"], "Cartesia-Version": "2026-08-14"})
            call = (response.json().get("calls") or [{}])[0] if response.status_code < 400 else {}
            # Cartesia's own error text is never stored: it could echo the number back.
            result = (f"call placed: {call['agent_call_id']}" if call.get("agent_call_id")
                      else f"call failed (Cartesia {'HTTP ' + str(response.status_code) if response.status_code >= 400 else 'returned no call'})")
        except Exception as error:  # network, bad JSON, an unexpected shape: record it, never crash the alert
            result = f"call failed: {type(error).__name__}"
    await db().execute("update alerts set paged=$2 where id=$1", alert_id, result)
    await audit("alerts", "oncall_paged" if result.startswith("call placed") else "oncall_not_paged", node, {"alert_id": alert_id, "result": result})


async def call_outcome(alert_id, paged):
    """How a placed call went, asked from Cartesia until it ends, then stored (only the status, never its details)."""
    call_id = paged.removeprefix("call placed: ") if paged and paged.startswith("call placed: ") else None
    if not call_id or not re.fullmatch(r"[A-Za-z0-9_]+", call_id):
        return paged
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            call = (await client.get(f"{CARTESIA_CALLS}/{call_id}", headers={
                "X-API-Key": os.environ["CARTESIA_API_KEY"], "Cartesia-Version": "2026-08-14"})).json()
        status, reason = str(call.get("status")), str(call.get("end_reason") or "")
    except Exception:  # Cartesia unreachable or a surprise shape: show what we know
        return paged
    if not call.get("end_time"):
        return f"{paged} ({re.sub(r'[^a-z_ ]', '', status.lower())})"
    ended = f"call {call_id} ended: {re.sub(r'[^a-z_ ]', '', status.lower())}" + (f" ({re.sub(r'[^a-z_ ]', '', reason.lower())})" if reason else "")
    await db().execute("update alerts set paged=$2 where id=$1", alert_id, ended)
    return ended


class Paging(BaseModel):
    """Body of PUT /gpu-nodes/paging."""
    on: bool


@router.get("/paging")
async def get_paging(actor=Depends(user)):
    """Whether alerts phone the on-call engineer, and whether paging is set up at all."""
    ready = all(os.environ.get(key) for key in ("ONCALL_PHONE", "CARTESIA_API_KEY", "CARTESIA_PAGER_AGENT_ID", "CARTESIA_FROM_NUMBER_ID"))
    last = await db().fetchval("select max(paged_at) from alerts")
    wait = await db().fetchval("select greatest(0, extract(epoch from (max(paged_at) + make_interval(secs => $1) - now())))::int "
                               f"from alerts where paged_at > {wait_starts('$1')}", PAGE_EVERY_SECONDS)
    return {"on": await paging_on(), "ready": ready, "every_seconds": PAGE_EVERY_SECONDS, "last_call_at": last, "next_call_in_seconds": wait or 0}


@router.put("/paging")
async def set_paging(body: Paging, actor=Depends(can("admin"))):
    """Turn phoning the on-call engineer on or off. Audited."""
    await db().execute("insert into settings values ('paging', $1) on conflict (key) do update set value=excluded.value", body.on)
    await audit(actor, "paging_on" if body.on else "paging_off", "on-call", {})
    return await get_paging(actor)


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


@router.get("/alerts/{alert_id:path}")
async def incident(alert_id: str, actor=Depends(user)):
    """One alert as an incident: what each agent did (oldest first), the fix waiting for a human, the prevention diff
    the fixer wrote, and whether the on-call engineer was called."""
    alert = await db().fetchrow("select * from alerts where id=$1", alert_id)
    if alert is None:
        raise HTTPException(404, "no such alert")
    steps, approvals = [], []
    first = await db().fetchrow("select * from traces where id=$1", alert["trace_id"]) if alert["trace_id"] else None
    if first:
        try:
            team = (await k8s(kube["custom"].get_namespaced_custom_object, GROUP, VERSION, alert["namespace"], "agents",
                              alert["agent"]))["spec"].get("talksTo", [])
        except HTTPException:  # the agent was deleted since: show what it did alone
            team = []
        # A trace is saved when its request ends, so started_at is really "finished at"; it began `seconds` earlier.
        # The agents it messaged ran inside that window.
        began = first["started_at"] - timedelta(seconds=first["seconds"] or 0)
        # Only teammates the alert's agent itself messaged (caller), never other conversations in the same window.
        rows = [first, *await db().fetch(
            "select * from traces where namespace=$1 and agent = any($2::text[]) and caller=$5 and started_at between $3 and $4",
            alert["namespace"], team, began, first["started_at"] + timedelta(seconds=5), alert["agent"])]
        steps = sorted(({"agent": row["agent"], "trace_id": row["id"], "started_at": row["started_at"] - timedelta(seconds=row["seconds"] or 0),
                         "seconds": row["seconds"], "status": row["status"], "answer": row["answer"],
                         "tools": [{"name": step["name"], "rule": step.get("rule")} for step in row["steps"] or [] if step["type"] == "tool"]}
                        for row in rows), key=lambda step: step["started_at"])
        for row in await db().fetch("select id, agent, namespace, payload, mac, status, decided_by, decided_at from approvals "
                                    "where id=$1 and namespace=$2 and agent=$3",  # exactly the call the alert's own agent proposed
                                    alert["approval_id"], alert["namespace"], alert["agent"]):
            request = verified_request(row)
            if request:  # unverifiable requests are never shown
                approvals.append({"id": row["id"], "status": row["status"], "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                                  "tool": request["tool"], "arguments": request["arguments"]})
    # The plan and its diff come from the same teammate reply.
    answer = next((step["answer"] for step in steps if step["agent"] != alert["agent"] and step["answer"]), "")
    diffs = DIFF.findall(answer)
    prevention = DIFF.sub("", answer).strip() or None  # the fixer's plan in words
    paged = await call_outcome(alert_id, alert["paged"])
    return dict(alert) | {"paged": paged, "steps": steps, "approvals": approvals, "prevention": prevention,
                          "prevention_diff": diffs[0] if diffs else None}


@router.post("/paging/reset-timer")
async def reset_paging_timer(actor=Depends(can("admin"))):
    """Let the next alert call right away, instead of waiting out the 5 minutes since the last call. Audited."""
    await db().execute("insert into settings values ('paging_reset_at', to_jsonb(now())) "
                       "on conflict (key) do update set value=excluded.value")
    await audit(actor, "paging_timer_reset", "on-call", {})
    return await get_paging(actor)
