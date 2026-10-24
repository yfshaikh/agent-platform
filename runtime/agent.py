"""The agent runtime: the one program every agent pod runs.

Its settings (environment variables) decide which agent it is: which model, what
instructions, which tool servers. The controller sets them from the Agent object.

The loop, per request:
  1. send the conversation + tool list to the model
  2. if the model asks for tools, call them (MCP servers) and add the results
  3. repeat until the model answers, or MAX_STEPS is hit
Every model call and tool call is recorded as a step in a trace.
"""
import json
import os
import time
import uuid

import httpx
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from pydantic import BaseModel

AGENT_NAME = os.environ.get("AGENT_NAME", "local-agent")
MODEL = os.environ.get("MODEL", "anthropic/claude-haiku-5.5")
INSTRUCTIONS = os.environ.get("INSTRUCTIONS", "You are a helpful assistant.")
# Each server: {"name": "f1", "url": "http://.../mcp", "token_env": "F1_TOOL_TOKEN"}.
# token_env names an environment variable, so the config never holds a secret itself.
TOOL_SERVERS = json.loads(os.environ.get("TOOL_SERVERS", "[]"))
MAX_STEPS = int(os.environ.get("MAX_STEPS", "10"))
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def server_headers(server):
    """Build the HTTP headers for one tool server, reading its token from the environment if it has one."""
    token_env = server.get("token_env")
    return {"Authorization": f"Bearer {os.environ[token_env]}"} if token_env else {}


async def list_tools(server):
    """Ask one MCP server for its tools; return them in the model's tool format, tagged with their server."""
    async with create_mcp_http_client(headers=server_headers(server)) as http_client:
        async with streamable_http_client(server["url"], http_client=http_client) as (read_stream, write_stream, *_):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
    return [
        {
            "server": server,
            "read_only": bool(tool.annotations and tool.annotations.read_only_hint),
            "schema": {"type": "function", "function": {
                "name": tool.name, "description": tool.description or "", "parameters": tool.input_schema,
            }},
        }
        for tool in tools
    ]


async def call_tool(tool, arguments):
    """Run one tool on its MCP server and return its text result."""
    async with create_mcp_http_client(headers=server_headers(tool["server"])) as http_client:
        async with streamable_http_client(tool["server"]["url"], http_client=http_client) as (read_stream, write_stream, *_):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool["schema"]["function"]["name"], arguments)
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def call_model(messages, tool_schemas):
    """Send the conversation to the model through OpenRouter; return (assistant message, tokens used)."""
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json={"model": MODEL, "messages": messages, **({"tools": tool_schemas} if tool_schemas else {})},
        )
    response.raise_for_status()
    body = response.json()
    return body["choices"][0]["message"], body.get("usage", {}).get("total_tokens", 0)


async def run_agent(message, tools, model=call_model, tool_runner=call_tool):
    """Run the model-and-tools loop for one user message. Returns (answer, trace steps).

    `model` and `tool_runner` are parameters so the self-check can swap in fakes.
    """
    tools_by_name = {tool["schema"]["function"]["name"]: tool for tool in tools}
    messages = [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": message}]
    steps = []
    for _ in range(MAX_STEPS):
        started = time.monotonic()
        reply, tokens = await model(messages, [tool["schema"] for tool in tools])
        steps.append({"type": "model", "model": MODEL, "tokens": tokens, "seconds": round(time.monotonic() - started, 2)})
        messages.append(reply)
        if not reply.get("tool_calls"):
            return reply.get("content") or "", steps
        for tool_call in reply["tool_calls"]:
            name = tool_call["function"]["name"]
            arguments = json.loads(tool_call["function"]["arguments"] or "{}")
            started = time.monotonic()
            if name in tools_by_name:
                try:
                    result = await tool_runner(tools_by_name[name], arguments)
                except Exception as error:  # a broken tool is reported to the model, not a crash
                    result = f"tool error: {error}"
            else:
                result = f"unknown tool: {name}"
            steps.append({
                "type": "tool", "name": name, "arguments": arguments, "result": result[:2000],
                "read_only": tools_by_name.get(name, {}).get("read_only"), "seconds": round(time.monotonic() - started, 2),
            })
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": result})
    return f"Stopped: reached the limit of {MAX_STEPS} steps.", steps


app = FastAPI(title=f"agent {AGENT_NAME}")


class ChatRequest(BaseModel):
    """Body of POST /chat."""
    message: str


@app.post("/chat")
async def chat(request: ChatRequest):
    """Answer one message. Tools are re-listed each request, so tool servers can change without a restart."""
    # ponytail: re-lists tools per request; cache them if the extra round trip ever matters
    tools = [tool for server in TOOL_SERVERS for tool in await list_tools(server)]
    answer, steps = await run_agent(request.message, tools)
    trace = {"trace_id": uuid.uuid4().hex[:12], "agent": AGENT_NAME, "message": request.message, "answer": answer, "steps": steps}
    print(json.dumps(trace), flush=True)  # one JSON line per request; becomes `kubectl logs` in a pod
    return trace


@app.get("/healthz")
def healthz():
    """Liveness check for Kubernetes: the process is up."""
    return {"ok": True, "agent": AGENT_NAME}
