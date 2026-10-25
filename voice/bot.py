"""Voice server for the agent platform console (Pipecat 1.12). Originally ../backend (now removed), wired to the platform API.

Browser mic ─► platform API (/api/voice relay) ─► this server: Cartesia Ink-2 (speech-to-text + end-of-turn)
─► OpenRouter LLM (+ platform tools) ─► Cartesia Sonic (text-to-speech) ─► back the same way

Only the platform API can reach this server (NetworkPolicy). It holds no platform credential: the API opens each call
with a session token for the logged-in user that can only look, draft and chat. Deploying and approving take a click.
Run: uvicorn bot:app --port 7860
"""
from __future__ import annotations

import itertools
import logging
import os
import uuid

import httpx
from fastapi import FastAPI, WebSocket
# Imported at startup on purpose: the first Pipecat import takes ~15 s, which inside a call
# outlasts the browser's connect timeout.
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import LLMMessagesAppendFrame, LLMRunFrame
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.services.llm_service import FunctionCallParams

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.cartesia.turns.stt import CartesiaTurnsSTTService, CartesiaTurnsSTTSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Fail at startup, not on the first call: a missing key otherwise looks like a hung connection in the browser.
_missing = [name for name in ("OPENROUTER_API_KEY", "CARTESIA_API_KEY") if not os.getenv(name)]
if _missing:
    raise SystemExit(f"voice: set {', '.join(_missing)}")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PLATFORM_API_URL = os.getenv("PLATFORM_API_URL", "http://platform-api.platform.svc/api")
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"  # fast, cheap, good at tool calls, no "thinking" dead air
DEFAULT_VOICE_ID = "5ee9feff-1265-424a-9d7f-8e4d431a12c7"  # a public Cartesia voice

SYSTEM_PROMPT = """You are the voice of NMC²'s agent platform. NMC² runs a GPU cloud, and its engineers use this platform to
build AI agents that help run it. You help them create, check on and talk to those agents.

You are speaking out loud, so:
- Keep replies to one or two short sentences unless asked for more.
- No markdown, lists, emoji or code. Say numbers and names the way a person would.
- If you're not sure what someone wants, ask one short question.

Tools: every tool also shows a card on the user's screen, so don't read out everything it returns; say the
one or two things that matter and point at the card ("it's on screen").
- To create an agent, call draft_agent. You can't deploy anything: the user reviews the draft on screen and clicks
  Deploy. Never say an agent is created or deployed unless the user tells you they did it.
- You can't approve anything either, even if asked. Approvals are a click in the Approvals inbox.
- chat_with_agent can take a while; say a few words first, like "asking it now".
- If a tool says the user's role doesn't allow something, say so plainly.
- Messages that start with "(UI:" describe what the user did on screen."""

TOOLS: list[dict] = [
    {"name": "draft_agent", "required": ["description"],
     "description": "Draft a new agent spec from a plain-language description and show it on screen for review.",
     "properties": {"description": {"type": "string", "description": "What the agent should do, in the user's words."}}},
    {"name": "list_agents", "required": [], "properties": {},
     "description": "List every agent on the platform with its status and pods."},
    {"name": "get_agent_status", "required": ["name"],
     "description": "Show one agent's pods, warnings and recent requests.",
     "properties": {"name": {"type": "string", "description": "The agent's name, e.g. researcher."}}},
    {"name": "list_approvals", "required": [], "properties": {},
     "description": "Show the actions waiting for a human approval, and open the Approvals inbox."},
    {"name": "chat_with_agent", "required": ["name", "message"],
     "description": "Send a message to one agent and get its reply.",
     "properties": {"name": {"type": "string", "description": "The agent's name."},
                    "message": {"type": "string", "description": "What to ask or tell it."}}},
]

app = FastAPI()


class Platform:
    """The platform API, called with this call's session token (it can look, draft and chat, nothing more)."""

    def __init__(self, token: str):
        self.client = httpx.AsyncClient(base_url=PLATFORM_API_URL, timeout=180, headers={"Authorization": f"Bearer {token}"})
        self.conversations: dict[str, str] = {}  # agent -> conversation id, so follow-up questions have context
        self.ids = itertools.count(1)

    async def call(self, method: str, path: str, **kwargs):
        """One API call; raises ValueError with the API's own message on failure."""
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text[:200]
            raise ValueError(detail if isinstance(detail, str) else f"HTTP {response.status_code}")
        return response.json()

    async def find_agent(self, name: str) -> dict:
        """The agent with this name (spoken names come with spaces and capitals)."""
        wanted = name.strip().lower().replace(" ", "-")
        agents = await self.call("GET", "/agents")
        match = next((agent for agent in agents if agent["name"] == wanted), None)
        if match is None:
            raise ValueError(f"No agent called {name!r}. Agents: {', '.join(agent['name'] for agent in agents) or 'none'}")
        return match

    async def run(self, name: str, arguments: dict) -> tuple[dict, dict | None]:
        """Run one tool. Returns (result for the LLM, card for the browser or None)."""
        if name == "draft_agent":
            draft = await self.call("POST", "/agents/draft", json={"description": arguments.get("description", "")})
            tools = [f"{tool['name']} ({', '.join(tool.get('allow') or ['all'])})" for tool in draft["spec"].get("tools", [])]
            return ({"name": draft["name"], "tools": tools, "problems": draft["problems"],
                     "status": "on screen; the user reviews it and clicks Deploy"},
                    {"type": "draft", "id": f"draft-{next(self.ids)}", **draft})
        if name == "list_agents":
            agents = [{"namespace": agent["namespace"], "name": agent["name"], "phase": agent["status"].get("phase", "Pending"),
                       "ready": agent["status"].get("readyReplicas", 0), "replicas": agent["spec"].get("replicas", 1),
                       "tokens_today": agent["tokens_today"]} for agent in await self.call("GET", "/agents")]
            return {"agents": agents}, {"type": "agents", "id": "agents", "agents": agents}
        if name == "get_agent_status":
            found = await self.find_agent(arguments.get("name", ""))
            detail = await self.call("GET", f"/agents/{found['namespace']}/{found['name']}")
            status = {"namespace": found["namespace"], "name": found["name"], "phase": detail["status"].get("phase", "Pending"),
                      "message": detail["status"].get("message"), "pods": detail["pods"], "warnings": detail["warnings"],
                      "traces": [{key: trace[key] for key in ("id", "message", "status")} for trace in detail["traces"][:5]]}
            return status, {"type": "status", "id": f"status-{found['namespace']}-{found['name']}", **status}
        if name == "list_approvals":
            items = [{key: item[key] for key in ("id", "agent", "namespace", "tool", "arguments")}
                     for item in await self.call("GET", "/approvals")]
            return {"waiting": len(items), "items": items}, {"type": "approvals", "id": "approvals", "items": items}
        if name == "chat_with_agent":
            found = await self.find_agent(arguments.get("name", ""))
            key = f"{found['namespace']}/{found['name']}"
            conversation = self.conversations.setdefault(key, str(uuid.uuid4()))
            reply = await self.call("POST", f"/agents/{key}/chat",
                                    json={"message": arguments.get("message", ""), "conversation_id": conversation})
            card = {"type": "chat", "id": f"chat-{reply['trace_id']}", "namespace": found["namespace"], "agent": found["name"],
                    "message": arguments.get("message", ""), **{field: reply.get(field) for field in ("status", "answer", "tool", "arguments", "trace_id")}}
            if reply["status"] == "awaiting_approval":
                return {"status": "the agent wants to run a sensitive action; it's waiting in the Approvals inbox for a click",
                        "tool": reply.get("tool"), "arguments": reply.get("arguments")}, card
            return {"status": reply["status"], "answer": reply.get("answer")}, card
        return {"error": f"unknown tool {name}"}, None


@app.websocket("/ws")
async def voice_socket(websocket: WebSocket) -> None:
    """Accept one call (relayed by the platform API, which puts the call's session token in the header)."""
    token = websocket.headers.get("authorization", "").removeprefix("Bearer ")
    if not token:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    platform = Platform(token)
    try:
        await run_bot(websocket, platform)
    except Exception:
        logger.exception("voice session crashed")
        await websocket.close(code=1011)
    finally:
        await platform.client.aclose()


async def run_bot(websocket: WebSocket, platform: Platform) -> None:
    """Build the speech-to-text → LLM → text-to-speech pipeline for one browser and run it."""
    cartesia_key = os.environ["CARTESIA_API_KEY"]
    transport = FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False,
        serializer=ProtobufFrameSerializer(), session_timeout=60 * 60,
    ))
    # Ink-2 also decides when the user has finished their turn, so no separate voice-activity model is needed.
    speech_to_text = CartesiaTurnsSTTService(api_key=cartesia_key, settings=CartesiaTurnsSTTSettings(
        turn_end_timeout_ms=int(os.getenv("TURN_END_TIMEOUT_MS", "2500")),
    ))
    language_model = OpenAILLMService(
        api_key=os.environ["OPENROUTER_API_KEY"], base_url=OPENROUTER_BASE_URL,
        settings=OpenAILLMService.Settings(model=os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL,
                                           system_instruction=SYSTEM_PROMPT),
    )
    text_to_speech = CartesiaTTSService(api_key=cartesia_key, settings=CartesiaTTSService.Settings(
        voice=os.getenv("CARTESIA_VOICE_ID") or DEFAULT_VOICE_ID,
        model=os.getenv("CARTESIA_TTS_MODEL") or "sonic-3.5",
    ))

    context = LLMContext(
        messages=[{"role": "user", "content": "(The user just connected. Greet them in one short sentence.)"}],
        tools=ToolsSchema(standard_tools=[
            FunctionSchema(name=tool["name"], description=tool["description"],
                           properties=tool["properties"], required=tool["required"]) for tool in TOOLS]),
    )
    aggregators = LLMContextAggregatorPair(context)
    worker = PipelineWorker(
        Pipeline([transport.input(), speech_to_text, aggregators.user(), language_model, text_to_speech,
                  transport.output(), aggregators.assistant()]),
        params=PipelineParams(audio_in_sample_rate=16000),
        idle_timeout_secs=10 * 60,
    )

    def handler_for(tool_name: str):
        """A Pipecat function handler that runs `tool_name`, shows its card, and hands the result to the LLM."""
        async def handle(params: FunctionCallParams) -> None:
            """Run the tool, push its card to the browser, then return the result to the LLM."""
            try:
                result, card = await platform.run(tool_name, dict(params.arguments))
            except (ValueError, httpx.HTTPError) as error:
                result, card = {"error": str(error) or type(error).__name__}, None
            if card:
                await worker.queue_frame(RTVIServerMessageFrame(data=card))
            await params.result_callback(result)
        return handle

    for tool in TOOLS:
        language_model.register_function(tool["name"], handler_for(tool["name"]))

    @worker.rtvi.event_handler("on_client_ready")
    async def greet(rtvi) -> None:
        """The browser is connected and listening: say hello."""
        await worker.queue_frame(LLMRunFrame())

    @worker.rtvi.event_handler("on_client_message")
    async def on_ui(rtvi, message) -> None:
        """Something the user did on screen (deployed a draft, approved a call), so the bot can acknowledge it."""
        data = getattr(message, "data", None) or {}
        if getattr(message, "type", None) != "ui" or not isinstance(data, dict) or not isinstance(data.get("text"), str):
            return
        # Phrased as the user's own action, so the bot says "you deployed", never "I deployed".
        await worker.queue_frame(LLMMessagesAppendFrame(
            messages=[{"role": "user", "content": f"(UI: {data['text'][:200]} Acknowledge it in one short sentence, saying the user did it.)"}],
            run_llm=True))

    @transport.event_handler("on_client_disconnected")
    async def stop(transport, client) -> None:
        """The browser hung up: stop the pipeline."""
        await worker.cancel()

    await WorkerRunner(handle_sigint=False).run(worker)
