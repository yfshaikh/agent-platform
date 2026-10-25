"""MCP tool server for the GPU fleet: real readings and jobs from the GPU node agent (gpu-node/node_agent.py).

Agents can look (readings, jobs, logs) and start jobs from the node's fixed list. Stopping or restarting a job and
cordoning the node are sensitive: each call needs a platform-signed human approval, checked here (common.guard).
No function creates, resizes or deletes machines: people do that.
Every function names the node it acts on; each agent's runtime refuses nodes the agent wasn't given (tools[].nodes).
"""
import json
import os
import re

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from common import serve

# Connected nodes: name -> URL of its node agent (nmc-gpu: the SSH tunnel to the Brev box). ponytail: one token for all nodes
NODES = json.loads(os.environ.get("NODES", '{"nmc-gpu": "http://host.docker.internal:9400"}'))
JOB_NAME = re.compile(r"^job-[a-z-]+-[0-9a-f]{6}$")  # same rule as the node: a name can't smuggle in another path
SOURCE_FILE = re.compile(r"^(node_agent\.py|jobs/[a-z-]+\.py)$")  # the node's own code, nothing else
HOT_CELSIUS = 83  # where data-center GPUs start slowing themselves down
# nvidia-smi throttle bit flags worth flagging. Not 0x4 (software power cap): a busy GPU sits at its power limit, that's normal.
THROTTLE_REASONS = {0x8: "hardware slowdown", 0x20: "software thermal slowdown", 0x40: "hardware thermal slowdown",
                    0x80: "hardware power brake"}


def job_path(name, action):
    """/jobs/<name>/<action>, only for a real job name (so an approved "stop" can't become a different request)."""
    if not JOB_NAME.fullmatch(name):
        raise ValueError(f"{name!r} isn't a job name (they look like job-train-1a2b3c)")
    return f"/jobs/{name}/{action}"


async def call_node(node, method, path, body=None):
    """One call to a node's agent; its error message comes back as the tool's error."""
    if node not in NODES:
        raise ValueError(f"no connected node {node!r}; nodes: {', '.join(NODES)}")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(method, NODES[node] + path, json=body,
                                            headers={"Authorization": f"Bearer {os.environ['NODE_TOKEN']}"})
    except httpx.HTTPError as error:
        raise RuntimeError(f"GPU node {node} unreachable ({type(error).__name__})") from None
    if response.status_code >= 400:
        raise RuntimeError(response.json().get("error", f"HTTP {response.status_code}"))
    return response.json()


def health(gpu):
    """Plain-language problems in one GPU's readings (empty list = healthy)."""
    problems = []
    if isinstance(gpu["temperature.gpu"], (int, float)) and gpu["temperature.gpu"] >= HOT_CELSIUS:
        problems.append(f"hot: {gpu['temperature.gpu']}°C")
    if isinstance(gpu["memory.used"], (int, float)) and gpu["memory.used"] > 0.95 * gpu["memory.total"]:
        problems.append(f"memory almost full: {gpu['memory.used']} of {gpu['memory.total']} MiB")
    reasons = gpu["clocks_throttle_reasons.active"]
    if isinstance(reasons, int):
        problems += [f"throttling: {text}" for bit, text in THROTTLE_REASONS.items() if reasons & bit]
    if isinstance(gpu["ecc.errors.uncorrected.volatile.total"], int) and gpu["ecc.errors.uncorrected.volatile.total"] > 0:
        problems.append(f"{gpu['ecc.errors.uncorrected.volatile.total']} uncorrected memory errors")
    return problems


mcp = MCPServer("gpu-fleet")


@mcp.tool(annotations=ToolAnnotations(title="List GPU nodes", read_only_hint=True))
async def list_nodes() -> list[str]:
    """The GPU nodes this tool can reach (each function below takes one of these as `node`)."""
    return sorted(NODES)


@mcp.tool(annotations=ToolAnnotations(title="List GPUs and their health", read_only_hint=True))
async def list_gpus(node: str) -> dict:
    """Live readings for every GPU on a node (temperature °C, utilization %, memory MiB, power W) and any problems."""
    gpus = await call_node(node, "GET", "/gpus")
    status = await call_node(node, "GET", "/node")
    return {"node": node, "cordoned": status["cordoned"], "gpus": [{**gpu, "problems": health(gpu)} for gpu in gpus]}


@mcp.tool(annotations=ToolAnnotations(title="List jobs", read_only_hint=True))
async def list_jobs(node: str) -> list[dict]:
    """Every job on a node: running or finished, its exit code, and whether it ran out of memory."""
    return await call_node(node, "GET", "/jobs")


@mcp.tool(annotations=ToolAnnotations(title="Read a job's logs", read_only_hint=True))
async def job_logs(node: str, name: str) -> dict:
    """The last 40 lines a job printed (errors such as CUDA out of memory show up here)."""
    return await call_node(node, "GET", job_path(name, "logs"))


@mcp.tool(annotations=ToolAnnotations(title="Read the node's code", read_only_hint=True))
async def read_source(node: str, file: str) -> dict:
    """The code a node runs: "node_agent.py" (starts and stops jobs) or a job script such as "jobs/train.py"."""
    if not SOURCE_FILE.fullmatch(file):
        raise ValueError('file must be "node_agent.py" or "jobs/<template>.py"')
    return await call_node(node, "GET", f"/source/{file}")


@mcp.tool(annotations=ToolAnnotations(title="Start a job", read_only_hint=False))
async def start_job(node: str, template: str) -> dict:
    """Start a job on a node from its fixed list: train, memory-hog, crashy-train or burn."""
    return await call_node(node, "POST", "/jobs", {"template": template})


@mcp.tool(annotations=ToolAnnotations(title="Stop a job", read_only_hint=False, destructive_hint=True))
async def stop_job(node: str, name: str, reason: str) -> dict:
    """Stop a running job (it can't be resumed). Needs a human approval."""
    return await call_node(node, "POST", job_path(name, "stop"))


@mcp.tool(annotations=ToolAnnotations(title="Restart a job", read_only_hint=False, destructive_hint=True))
async def restart_job(node: str, name: str, reason: str) -> dict:
    """Restart a job from the beginning. Needs a human approval."""
    return await call_node(node, "POST", job_path(name, "restart"))


@mcp.tool(annotations=ToolAnnotations(title="Cordon a node", read_only_hint=False, destructive_hint=True))
async def cordon_node(node: str, cordoned: bool, reason: str) -> dict:
    """Stop (cordoned=true) or resume (false) sending new jobs to a node; running jobs keep running. Needs a human approval."""
    return await call_node(node, "POST", "/cordon", {"on": cordoned})


if __name__ == "__main__":
    serve(mcp, sensitive={"stop_job", "restart_job", "cordon_node"})  # must match the Tool object's "sensitive" classes
