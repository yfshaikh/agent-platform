"""MCP tool server for the GPU fleet: real readings and jobs from the GPU node agent (gpu-node/node_agent.py).

Agents can look (readings, jobs, logs) and start jobs from the node's fixed list. Stopping or restarting a job and
cordoning the node are sensitive: each call needs a platform-signed human approval, checked here (common.guard).
No function creates, resizes or deletes machines: people do that.
ponytail: one node (the Brev box); a NODES map of name -> URL when there's more than one.
"""
import os

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from common import serve

NODE_URL = os.environ.get("NODE_URL", "http://host.docker.internal:9400")  # the SSH tunnel to the GPU box
NODE_NAME = os.environ.get("NODE_NAME", "nmc-gpu")
HOT_CELSIUS = 83  # where data-center GPUs start slowing themselves down
# nvidia-smi throttle bit flags worth flagging. Not 0x4 (software power cap): a busy GPU sits at its power limit, that's normal.
THROTTLE_REASONS = {0x8: "hardware slowdown", 0x20: "software thermal slowdown", 0x40: "hardware thermal slowdown",
                    0x80: "hardware power brake"}


async def node(method, path, body=None):
    """One call to the node agent; its error message comes back as the tool's error."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(method, NODE_URL + path, json=body,
                                            headers={"Authorization": f"Bearer {os.environ['NODE_TOKEN']}"})
    except httpx.HTTPError as error:
        raise RuntimeError(f"GPU node {NODE_NAME} unreachable ({type(error).__name__})") from None
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


@mcp.tool(annotations=ToolAnnotations(title="List GPUs and their health", read_only_hint=True))
async def list_gpus() -> dict:
    """Live readings for every GPU on the node (temperature °C, utilization %, memory MiB, power W) and any problems."""
    gpus = await node("GET", "/gpus")
    status = await node("GET", "/node")
    return {"node": NODE_NAME, "cordoned": status["cordoned"],
            "gpus": [{**gpu, "problems": health(gpu)} for gpu in gpus]}


@mcp.tool(annotations=ToolAnnotations(title="List jobs", read_only_hint=True))
async def list_jobs() -> list[dict]:
    """Every job on the node: running or finished, its exit code, and whether it ran out of memory."""
    return await node("GET", "/jobs")


@mcp.tool(annotations=ToolAnnotations(title="Read a job's logs", read_only_hint=True))
async def job_logs(name: str) -> dict:
    """The last 40 lines a job printed (errors such as CUDA out of memory show up here)."""
    return await node("GET", f"/jobs/{name}/logs")


@mcp.tool(annotations=ToolAnnotations(title="Start a job", read_only_hint=False))
async def start_job(template: str) -> dict:
    """Start a job from the node's fixed list: train, memory-hog, crashy-train or burn."""
    return await node("POST", "/jobs", {"template": template})


@mcp.tool(annotations=ToolAnnotations(title="Stop a job", read_only_hint=False, destructive_hint=True))
async def stop_job(name: str, reason: str) -> dict:
    """Stop a running job (it can't be resumed). Needs a human approval."""
    return await node("POST", f"/jobs/{name}/stop")


@mcp.tool(annotations=ToolAnnotations(title="Restart a job", read_only_hint=False, destructive_hint=True))
async def restart_job(name: str, reason: str) -> dict:
    """Restart a job from the beginning. Needs a human approval."""
    return await node("POST", f"/jobs/{name}/restart")


@mcp.tool(annotations=ToolAnnotations(title="Cordon the node", read_only_hint=False, destructive_hint=True))
async def cordon_node(cordoned: bool, reason: str) -> dict:
    """Stop (cordoned=true) or resume (false) sending new jobs to this node; running jobs keep running. Needs a human approval."""
    return await node("POST", "/cordon", {"on": cordoned})


if __name__ == "__main__":
    serve(mcp, sensitive={"stop_job", "restart_job", "cordon_node"})  # must match the Tool object's "sensitive" classes
