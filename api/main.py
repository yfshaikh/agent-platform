"""The platform API: the one door for people and apps (the web console, voice, crash tests).

It turns requests into Kubernetes Agent objects (the operator does the rest), proxies chat to agent pods,
runs the approval inbox, and reads monitoring data from Postgres. It also:
  - owns the database: creates the tables and the limited `agent_runtime` login that agent pods use
  - signs approvals: deciding an approval produces a grant signed with the platform's private key; the agent
    and the tool server both check it, so nothing but this API (behind a logged-in user) can approve a call
  - writes the audit log for every change and every decision, with the logged-in user as the actor
  - logs people in: named users with a role (viewer, builder, approver, admin), server-side sessions
  - relays the console's voice call to the voice server, with a session that can't deploy or approve

Run in the cluster (deploy/platform/platform.yaml). Routes live under /api; the built console is served at /.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Literal

import asyncpg
import httpx
import kubernetes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import websockets
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from templates import TEMPLATES

logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs at INFO would print the Nessie key (it's in the URL)
logging.getLogger("httpcore").setLevel(logging.WARNING)

GROUP, VERSION = "platform.hacktx.dev", "v1"
PLATFORM_NAMESPACE = "platform"
PLATFORM_TOKEN = os.environ["PLATFORM_TOKEN"]
# What each role may do. Each role can do everything the one before it can.
ROLES = {
    "viewer": {"read"},
    "builder": {"read", "draft", "chat", "build"},
    "approver": {"read", "draft", "chat", "build", "approve"},
    "admin": {"read", "draft", "chat", "build", "approve", "admin"},
}
PERMISSION_TEXT = {"read": "view the platform", "draft": "draft agents", "chat": "chat with agents",
                   "build": "create, change or delete agents", "approve": "approve actions", "admin": "manage users"}
VOICE_PERMISSIONS = {"read", "draft", "chat"}  # voice can look, draft and talk; deploying and approving take a click
VOICE_URL = os.environ.get("VOICE_URL", "ws://voice.platform.svc:7860/ws")
SESSION_HOURS = 12
DRAFT_MODEL = os.environ.get("DRAFT_MODEL", "anthropic/claude-haiku-5.5")
AGENT_URL = os.environ.get("AGENT_URL_TEMPLATE", "http://agent-{name}.{namespace}.svc")
GRANT_SECONDS = 300
USD_PER_MILLION_TOKENS = float(os.environ.get("USD_PER_MILLION_TOKENS", "2.0"))  # ponytail: one blended rate, not per model
NAME_PATTERN = r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$"  # Kubernetes name rules, short enough for "agent-<name>-keys"
MANAGED_LABEL = "platform.hacktx.dev/managed"  # only namespaces the platform created (or labeled) hold agents

SCHEMA = """
create table if not exists traces (
  id text primary key, agent text, namespace text, conversation_id text, message text, answer text,
  status text, tokens int, started_at timestamptz default now(), seconds real, steps jsonb);
alter table traces add column if not exists updated_at timestamptz default now();
create table if not exists approvals (
  id text primary key, agent text, namespace text, tool text, arguments jsonb, state jsonb,
  status text default 'pending', decided_by text, created_at timestamptz default now(), decided_at timestamptz);
alter table approvals add column if not exists payload text;
alter table approvals add column if not exists mac text;
create table if not exists audit (
  id bigserial primary key, at timestamptz default now(), actor text, action text, target text, detail jsonb);
create table if not exists usage (
  agent text, namespace text, day date default current_date, tokens bigint default 0,
  primary key (agent, namespace, day));
create table if not exists users (
  name text primary key, password_hash text not null, role text not null, created_at timestamptz default now());
-- Session cookies are random tokens; only their hashes are stored, so a database leak can't be replayed as a login.
create table if not exists sessions (
  token_hash text primary key, name text not null, permissions text[] not null, expires_at timestamptz not null);
create table if not exists conversations (
  id text primary key, agent text, namespace text, messages jsonb, updated_at timestamptz default now());
do $$ begin
  if not exists (select from pg_roles where rolname = 'agent_runtime') then create role agent_runtime login; end if;
end $$;
-- Agents may add and update their own records, but never delete, create or drop anything, and audit is append-only.
grant select, insert, update on traces, usage, conversations to agent_runtime;
-- Approvals: agents add requests and record decisions, but can't rewrite what a pending request says.
revoke update on approvals from agent_runtime;
grant select, insert on approvals to agent_runtime;
grant update (status, decided_by, decided_at) on approvals to agent_runtime;
grant insert on audit to agent_runtime;
grant usage on sequence audit_id_seq to agent_runtime;
"""

kube, state = {}, {}


async def setup_connection(connection):
    """Decode jsonb columns to Python objects (and encode them back)."""
    await connection.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


@asynccontextmanager
async def lifespan(app):
    """Connect to Kubernetes and Postgres; create the schema and the agents' database login."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
    kube["custom"], kube["core"] = kubernetes.client.CustomObjectsApi(), kubernetes.client.CoreV1Api()
    state["db"] = await asyncpg.create_pool(os.environ["ADMIN_DATABASE_URL"], min_size=1, max_size=5, init=setup_connection)
    import crash  # imported here because these modules import helpers from this one
    async with state["db"].acquire() as connection:
        await connection.execute(SCHEMA + crash.SCHEMA)
        password_sql = await connection.fetchval("select format('alter role agent_runtime password %L', $1::text)",
                                                 os.environ["AGENT_DB_PASSWORD"])
        await connection.execute(password_sql)
    state["signing_key"] = Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(os.environ["APPROVAL_PRIVATE_KEY"]))
    yield
    await state["db"].close()


app = FastAPI(title="agent platform API", lifespan=lifespan)
api = APIRouter(prefix="/api")
login_codes = {}  # one-time login link code -> expiry. ponytail: in memory, so an API restart drops unused links
failed_logins = {}  # user name -> times of recent failed attempts. ponytail: in memory, per API pod


def db():
    """The connection pool."""
    return state["db"]


class Actor(str):
    """A logged-in user's name (what the audit log records), carrying what their role allows."""
    permissions: frozenset = frozenset()


def make_actor(name, permissions):
    """An Actor with these permissions."""
    found = Actor(name)
    found.permissions = frozenset(permissions)
    return found


def token_hash(token):
    """What the sessions table stores instead of the token."""
    return hashlib.sha256(token.encode()).hexdigest()


async def actor_for(token, bearer=False):
    """Who a session token belongs to, or None. The platform token acts as admin, but only as a bearer header (scripts),
    never as a cookie."""
    if not token:
        return None
    if bearer and hmac.compare_digest(token.encode(), PLATFORM_TOKEN.encode()):
        return make_actor("admin", ROLES["admin"])
    row = await db().fetchrow("select name, permissions from sessions where token_hash=$1 and expires_at > now()", token_hash(token))
    return make_actor(row["name"], row["permissions"]) if row else None


def can(permission):
    """A dependency: the logged-in user (bearer token or session cookie), if their role allows `permission`."""
    async def check(request: Request):
        """401 if nobody is logged in, 403 if their role doesn't allow it."""
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        found = await actor_for(bearer, bearer=True) if bearer else await actor_for(request.cookies.get("session", ""))
        if found is None:
            raise HTTPException(401, "not logged in")
        if permission not in found.permissions:
            raise HTTPException(403, f"your role can't {PERMISSION_TEXT[permission]}")
        return found
    return check


user = can("read")


async def start_session(name, permissions, hours=SESSION_HOURS):
    """A new session token (the caller hands it out once; only its hash is kept)."""
    token = secrets.token_urlsafe(32)
    await db().execute("delete from sessions where expires_at < now()")
    await db().execute("insert into sessions (token_hash, name, permissions, expires_at) values ($1, $2, $3, now() + make_interval(hours => $4))",
                       token_hash(token), name, sorted(permissions), hours)
    return token


def with_session_cookie(response, token):
    """Set the console's session cookie: JavaScript can't read it, and other sites can't send it.
    ponytail: no Secure flag because the demo runs on http://localhost; set it behind TLS."""
    response.set_cookie("session", token, httponly=True, samesite="strict", max_age=SESSION_HOURS * 3600)
    return response


def hash_password(password, salt=None):
    """scrypt (memory-hard, so guessing is slow even on GPUs), stored as scrypt$<salt>$<hash>."""
    salt = salt or secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()}"


def password_matches(password, stored):
    """Constant-time check of a password against its stored hash."""
    return hmac.compare_digest(hash_password(password, bytes.fromhex(stored.split("$")[1])), stored)


UNKNOWN_USER_HASH = hash_password(secrets.token_urlsafe())  # unknown names take as long to refuse as wrong passwords


async def k8s(function, *args, **kwargs):
    """Run a (blocking) Kubernetes client call in a thread; turn its errors into HTTP errors with Kubernetes' message."""
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except kubernetes.client.ApiException as error:
        try:
            message = json.loads(error.body).get("message", error.reason)
        except (TypeError, ValueError):
            message = error.reason
        raise HTTPException(error.status, message) from None


async def audit(actor, action, target, detail):
    """Append one event to the audit log."""
    await db().execute("insert into audit (actor, action, target, detail) values ($1, $2, $3, $4)", actor, action, target, detail)


def arguments_hash(arguments):
    """Fingerprint of a tool call's arguments (the tool server computes the same one)."""
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verified_request(row):
    """The approval request's stamped text as {tool, arguments, state}, or None if the stamp isn't this agent's.

    Each agent stamps requests with HMAC(master, "<namespace>/<name>"); recomputing it here means a row written by any
    other agent (or edited afterwards) is refused. Display, grant and resume all use this one text.
    """
    if not row["payload"] or not row["mac"]:
        return None
    key = hmac.new(os.environ["AGENT_KEY_MASTER"].encode(), f"{row['namespace']}/{row['agent']}".encode(), hashlib.sha256).hexdigest()
    expected = hmac.new(key.encode(), f"{row['id']}\n{row['agent']}\n{row['namespace']}\n{row['payload']}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, row["mac"]):
        return None
    request = json.loads(row["payload"])
    paused_call = request["state"]["queue"][0]["function"]
    try:
        paused_arguments = json.loads(paused_call["arguments"] or "{}")
    except ValueError:
        return None  # the model wrote broken JSON; that call can never run, so it's never shown or approved
    # The call that will run must be exactly the one displayed (a tampered agent could write mismatched fields).
    if paused_call["name"] != request["tool"] or paused_arguments != request["arguments"]:
        return None
    return request


def sign_grant(claims):
    """A signed approval grant: base64(claims) + "." + base64(Ed25519 signature)."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return body + "." + base64.urlsafe_b64encode(state["signing_key"].sign(body.encode())).decode()


async def call_agent(namespace, name, path, body):
    """POST to an agent pod with its namespace's caller token; returns (HTTP status, JSON body)."""
    secret = await k8s(kube["core"].read_namespaced_secret, "agent-caller-token", namespace)
    token = base64.b64decode(secret.data["AGENT_CALLER_TOKEN"]).decode()
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(AGENT_URL.format(name=name, namespace=namespace) + path, json=body,
                                         headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as error:
        raise HTTPException(502, f"agent {namespace}/{name} unreachable ({type(error).__name__})") from None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"detail": response.text[:300]}


# ---- login -------------------------------------------------------------------------------------------------------

class Login(BaseModel):
    """Body of POST /login."""
    name: str = Field(max_length=64)
    password: str = Field(max_length=200)


@api.post("/login")
async def login_with_password(body: Login):
    """Log in with a name and password; sets the session cookie. Five failures lock that name for five minutes."""
    recent = [moment for moment in failed_logins.get(body.name, []) if moment > time.time() - 300]
    if len(recent) >= 5:
        raise HTTPException(429, "too many failed attempts; try again in five minutes")
    row = await db().fetchrow("select password_hash, role from users where name=$1", body.name)
    if not password_matches(body.password, row["password_hash"] if row else UNKNOWN_USER_HASH) or row is None:
        failed_logins[body.name] = recent + [time.time()]
        raise HTTPException(401, "wrong name or password")
    failed_logins.pop(body.name, None)
    await audit(body.name, "logged_in", body.name, {"role": row["role"]})
    return with_session_cookie(JSONResponse({"user": body.name, "role": row["role"]}), await start_session(body.name, ROLES[row["role"]]))


@api.post("/logout")
async def logout(request: Request):
    """End this browser's session."""
    await db().execute("delete from sessions where token_hash=$1", token_hash(request.cookies.get("session", "")))
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


@api.post("/login-links")
async def create_login_link(actor=Depends(can("admin"))):
    """A one-time, 5-minute admin login link (deploy/login_link.py): the first login, before any users exist, or break-glass."""
    code = secrets.token_urlsafe(24)
    login_codes[code] = time.time() + 300
    return {"path": f"/api/login?code={code}"}


@api.get("/login")
async def login(code: str):
    """Redeem a login link: set the session cookie and go to the console."""
    if login_codes.pop(code, 0) < time.time():
        raise HTTPException(403, "this login link expired or was already used; make a new one with deploy/login_link.py")
    await audit("admin", "logged_in", "admin", {"via": "login link"})
    return with_session_cookie(RedirectResponse("/", status_code=303), await start_session("admin", ROLES["admin"]))


@api.get("/me")
async def me(actor=Depends(user)):
    """Who is logged in and what they may do (the console hides what they can't)."""
    role = next((name for name, permissions in ROLES.items() if permissions == actor.permissions), None)
    return {"user": actor, "role": role, "permissions": sorted(actor.permissions)}


# ---- users -------------------------------------------------------------------------------------------------------

class NewUser(BaseModel):
    """Body of POST /users: creates the user, or resets their role and password."""
    name: str = Field(pattern=NAME_PATTERN)
    role: Literal["viewer", "builder", "approver", "admin"]
    password: str = Field(min_length=10, max_length=200)


@api.get("/users")
async def list_users(actor=Depends(can("admin"))):
    """Everyone who can log in."""
    return [dict(row) for row in await db().fetch("select name, role, created_at from users order by name")]


@api.post("/users")
async def save_user(body: NewUser, actor=Depends(can("admin"))):
    """Create a user or reset one; their open sessions end, so a new role takes effect at once."""
    await db().execute("insert into users (name, password_hash, role) values ($1, $2, $3) "
                       "on conflict (name) do update set password_hash = excluded.password_hash, role = excluded.role",
                       body.name, hash_password(body.password), body.role)
    await db().execute("delete from sessions where name=$1", body.name)
    await audit(actor, "user_saved", body.name, {"role": body.role})
    return {"name": body.name, "role": body.role}


@api.delete("/users/{name}")
async def delete_user(name: str, actor=Depends(can("admin"))):
    """Remove a user and end their sessions."""
    if name == actor:
        raise HTTPException(400, "you can't delete yourself")
    await db().execute("delete from users where name=$1", name)
    await db().execute("delete from sessions where name=$1", name)
    await audit(actor, "user_deleted", name, {})
    return {"deleted": name}


# ---- agents ------------------------------------------------------------------------------------------------------

class NewAgent(BaseModel):
    """Body of POST /agents."""
    namespace: str = Field(pattern=NAME_PATTERN)
    name: str = Field(pattern=NAME_PATTERN)
    spec: dict
    crash_test_id: str | None = None  # a passing crash test of this exact spec (required if it can move money)
    deploy_anyway: bool = False  # skip that requirement; audited


class AgentSpec(BaseModel):
    """Body of PUT /agents/{ns}/{name}: the full new spec."""
    spec: dict
    crash_test_id: str | None = None
    deploy_anyway: bool = False


def check_deploy_anyway(body, actor):
    """Skipping the crash test is an approver's call."""
    if body.deploy_anyway and "approve" not in actor.permissions:
        raise HTTPException(403, "only approvers can deploy without a passing crash test")


def manifest(namespace, name, spec):
    """An Agent object."""
    return {"apiVersion": f"{GROUP}/{VERSION}", "kind": "Agent", "metadata": {"name": name, "namespace": namespace}, "spec": spec}


async def check_target(namespace, name=None):
    """Agents live only in platform-managed namespaces (never next to system pods or the platform's own secrets),
    and names must be plain Kubernetes names (they become hostnames the API calls)."""
    if not re.fullmatch(NAME_PATTERN, namespace) or (name is not None and not re.fullmatch(NAME_PATTERN, name)):
        raise HTTPException(400, "namespace and name must be lowercase letters, digits and dashes")
    found = await k8s(kube["core"].read_namespace, namespace)
    labels = found.metadata.labels or {}
    if labels.get(MANAGED_LABEL) != "true":
        raise HTTPException(400, f"namespace {namespace!r} isn't managed by the platform")


async def tool_registry():
    """Tool objects by name."""
    items = (await k8s(kube["custom"].list_namespaced_custom_object, GROUP, VERSION, PLATFORM_NAMESPACE, "tools"))["items"]
    return {tool["metadata"]["name"]: tool for tool in items}


async def agents_with_usage():
    """Every Agent with its status and tokens used today."""
    items = (await k8s(kube["custom"].list_cluster_custom_object, GROUP, VERSION, "agents"))["items"]
    rows = await db().fetch("select namespace, agent, tokens from usage where day = current_date")
    tokens = {(row["namespace"], row["agent"]): row["tokens"] for row in rows}
    return [{"namespace": agent["metadata"]["namespace"], "name": agent["metadata"]["name"], "spec": agent["spec"],
             "status": agent.get("status", {}), "tokens_today": tokens.get((agent["metadata"]["namespace"], agent["metadata"]["name"]), 0)}
            for agent in items]


@api.get("/templates")
async def get_templates(actor=Depends(user)):
    """Built-in starting points."""
    return TEMPLATES


@api.get("/tools")
async def get_tools(actor=Depends(user)):
    """The tool registry: each tool's functions and their permission class."""
    return [{"name": name, "description": tool["spec"].get("description", ""), "classes": tool["spec"]["classes"]}
            for name, tool in (await tool_registry()).items()]


@api.get("/agents")
async def list_agents(actor=Depends(user)):
    """All agents, everywhere."""
    return await agents_with_usage()


@api.get("/agents/{namespace}/{name}")
async def get_agent(namespace: str, name: str, actor=Depends(user)):
    """One agent: spec, status, pods, recent warnings, recent traces, pending approvals."""
    await check_target(namespace, name)
    agent = await k8s(kube["custom"].get_namespaced_custom_object, GROUP, VERSION, namespace, "agents", name)
    pods = await k8s(kube["core"].list_namespaced_pod, namespace, label_selector=f"platform.hacktx.dev/agent={name}")
    events = await k8s(kube["core"].list_namespaced_event, namespace, field_selector="type=Warning")
    traces = await db().fetch("select id, message, status, tokens, started_at from traces where namespace=$1 and agent=$2 "
                              "order by started_at desc limit 10", namespace, name)
    return {
        "namespace": namespace, "name": name, "spec": agent["spec"], "status": agent.get("status", {}),
        "pods": [{"name": pod.metadata.name, "phase": pod.status.phase,
                  "ready": all(status.ready for status in pod.status.container_statuses or []),
                  "restarts": sum(status.restart_count for status in pod.status.container_statuses or [])} for pod in pods.items],
        # Only warnings about pods that are currently not ready (startup probe failures linger as events for an hour).
        "warnings": [{"object": event.involved_object.name, "reason": event.reason, "message": event.message}
                     for event in events.items if event.involved_object.name in {
                         pod.metadata.name for pod in pods.items
                         if not all(status.ready for status in pod.status.container_statuses or [])}][-5:],
        "traces": [dict(row) for row in traces],
    }


@api.post("/agents")
async def create_agent(body: NewAgent, actor=Depends(can("build"))):
    """Create an Agent (and its namespace if new). The operator builds its pods within seconds."""
    check_deploy_anyway(body, actor)
    try:
        await asyncio.to_thread(kube["core"].read_namespace, body.namespace)
    except kubernetes.client.ApiException as error:
        if error.status != 404:
            raise
        await k8s(kube["core"].create_namespace, {"metadata": {"name": body.namespace, "labels": {MANAGED_LABEL: "true"}}})
    await check_target(body.namespace, body.name)
    import crash  # imported here because crash.py imports helpers from this module
    crash_test = await crash.gate(body.spec, body.crash_test_id, body.deploy_anyway, actor, f"{body.namespace}/{body.name}")
    created = await k8s(kube["custom"].create_namespaced_custom_object, GROUP, VERSION, body.namespace, "agents",
                        manifest(body.namespace, body.name, body.spec))
    if crash_test:  # show the result on the agent (kubectl get agent -o yaml, and the console)
        await k8s(kube["custom"].patch_namespaced_custom_object_status, GROUP, VERSION, body.namespace, "agents", body.name,
                  {"status": {"crashTest": crash_test}})
    await audit(actor, "agent_created", f"{body.namespace}/{body.name}", {"spec": body.spec, "crash_test": crash_test})
    return {"namespace": body.namespace, "name": body.name, "spec": created["spec"]}


@api.put("/agents/{namespace}/{name}")
async def update_agent(namespace: str, name: str, body: AgentSpec, actor=Depends(can("build"))):
    """Replace an Agent's spec (the console's kill switch sends the same spec with replicas: 0)."""
    check_deploy_anyway(body, actor)
    await check_target(namespace, name)
    agent = await k8s(kube["custom"].get_namespaced_custom_object, GROUP, VERSION, namespace, "agents", name)
    import crash
    if crash.spec_hash(body.spec) != crash.spec_hash(agent["spec"]):  # stopping or scaling doesn't need a new test
        await crash.gate(body.spec, body.crash_test_id, body.deploy_anyway, actor, f"{namespace}/{name}")
    before, agent["spec"] = agent["spec"], body.spec
    await k8s(kube["custom"].replace_namespaced_custom_object, GROUP, VERSION, namespace, "agents", name, agent)
    stopped = body.spec.get("replicas") == 0 and before.get("replicas", 1) != 0
    await audit(actor, "agent_stopped" if stopped else "agent_updated", f"{namespace}/{name}", {"before": before, "after": body.spec})
    return {"namespace": namespace, "name": name, "spec": body.spec}


@api.delete("/agents/{namespace}/{name}")
async def delete_agent(namespace: str, name: str, actor=Depends(can("build"))):
    """Delete an Agent; Kubernetes deletes everything it owned."""
    await check_target(namespace, name)
    await k8s(kube["custom"].delete_namespaced_custom_object, GROUP, VERSION, namespace, "agents", name)
    # Its pending approvals can never run now; take them out of the inbox.
    await db().execute("update approvals set status='expired' where namespace=$1 and agent=$2 and status='pending'", namespace, name)
    await audit(actor, "agent_deleted", f"{namespace}/{name}", {})
    return {"deleted": f"{namespace}/{name}"}


# ---- drafting an agent from plain English ----------------------------------------------------------------------

class DraftRequest(BaseModel):
    """Body of POST /agents/draft."""
    description: str = Field(min_length=3, max_length=2000)


DRAFT_PROMPT = """You design AI agents for an enterprise agent platform. Reply with one JSON object and nothing else:
{"name": "<lowercase-with-dashes, at most 30 characters>", "spec": {"model": "MODEL", "instructions": "<the agent's system prompt, under 120 words>",
 "tools": [{"name": "<tool>", "allow": ["<function>"]}], "replicas": 1, "budget": {"tokensPerDay": 200000, "maxStepsPerRequest": 8}}}
Rules: use only the tools and functions listed below; allow only the functions the job needs (least privilege); use an empty
tools list if none fit. Sensitive functions always wait for a human approval; mention that in the instructions if you use one.
Available tools (function: permission class):
TOOLS"""


def draft_problems(spec, tools):
    """What the operator would reject: unknown tools or functions."""
    problems = []
    for entry in spec.get("tools", []):
        if entry.get("name") not in tools:
            problems.append(f"unknown tool {entry.get('name')!r}")
            continue
        problems += [f"{entry['name']} has no function {function!r}" for function in entry.get("allow", [])
                     if function not in tools[entry["name"]]["spec"]["classes"]]
    return problems


def fake_draft(description):
    """Zero-cost drafting for tests: pick the closest template by keyword."""
    text = description.lower()
    template_id = ("bank-assistant" if any(word in text for word in ("bank", "money", "transfer")) else
                   "shopping-helper" if any(word in text for word in ("price", "shop", "buy")) else "research-assistant")
    return next(template for template in TEMPLATES if template["id"] == template_id)


@api.post("/agents/draft")
async def draft_agent(body: DraftRequest, actor=Depends(can("draft"))):
    """Draft an Agent spec from a description. Checked against the CRD schema (a Kubernetes dry run) but NOT applied."""
    tools = await tool_registry()
    if DRAFT_MODEL == "fake":
        draft = fake_draft(body.description)
    else:
        tool_lines = "\n".join(f"- {name}: {tool['spec'].get('description', '')} " + ", ".join(f"{function}: {kind}" for function, kind in tool["spec"]["classes"].items())
                               for name, tool in tools.items())
        prompt = DRAFT_PROMPT.replace("MODEL", DRAFT_MODEL).replace("TOOLS", tool_lines)
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions",
                                         headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
                                         json={"model": DRAFT_MODEL, "response_format": {"type": "json_object"},
                                               "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": body.description}]})
        if response.status_code != 200:
            raise HTTPException(502, f"the drafting model failed (HTTP {response.status_code})")
        content = response.json()["choices"][0]["message"]["content"] or ""
        try:
            draft = json.loads(content.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
        except ValueError:
            raise HTTPException(502, "the drafting model didn't return JSON; try rephrasing") from None
    name = re.sub(r"[^a-z0-9-]", "-", str(draft.get("name", "new-agent")).lower()).strip("-")[:30] or "new-agent"
    spec = draft.get("spec", {})
    problems = draft_problems(spec, tools)
    try:  # the API server validates against the Agent CRD's schema without creating anything
        await k8s(kube["custom"].create_namespaced_custom_object, GROUP, VERSION, "default", "agents",
                  manifest("default", name, spec), dry_run="All")
    except HTTPException as error:
        problems.append(error.detail)
    return {"name": name, "spec": spec, "problems": problems}


# ---- chat and approvals ----------------------------------------------------------------------------------------

class ChatMessage(BaseModel):
    """Body of POST /agents/{ns}/{name}/chat."""
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None


@api.post("/agents/{namespace}/{name}/chat")
async def chat(namespace: str, name: str, body: ChatMessage, actor=Depends(can("chat"))):
    """Send a message to an agent and return its reply (or its request for approval)."""
    await check_target(namespace, name)
    status, reply = await call_agent(namespace, name, "/chat", body.model_dump())
    return JSONResponse(reply, status_code=status)


@api.get("/approvals")
async def list_approvals(status: str | None = "pending", actor=Depends(user)):
    """The approval inbox (status=pending by default; status=all for history)."""
    rows = await db().fetch("select id, agent, namespace, payload, mac, status, decided_by, created_at, decided_at from approvals "
                            "where $1::text = 'all' or status = $1 order by created_at desc limit 100", status)
    shown = []
    for row in rows:
        request = verified_request(row)
        if request:  # unverifiable rows never reach the inbox
            shown.append({key: row[key] for key in ("id", "agent", "namespace", "status", "decided_by", "created_at", "decided_at")}
                         | {"tool": request["tool"], "arguments": request["arguments"]})
    return shown


class Decision(BaseModel):
    """Body of POST /approvals/{id}."""
    decision: Literal["approve", "deny"]


@api.post("/approvals/{approval_id}")
async def decide(approval_id: str, body: Decision, actor=Depends(can("approve"))):
    """Approve or deny a paused call: sign a grant for exactly that call and hand it to the agent, which resumes."""
    row = await db().fetchrow("select id, agent, namespace, payload, mac from approvals where id=$1 and status='pending'", approval_id)
    if row is None:
        raise HTTPException(404, "no pending approval with that id")
    request = verified_request(row)
    if request is None:
        raise HTTPException(409, "this approval request failed its integrity check and can't be decided")
    arguments = request["arguments"]
    grant = sign_grant({"approval_id": approval_id, "agent": row["agent"], "namespace": row["namespace"], "tool": request["tool"],
                        "args_hash": arguments_hash(arguments), "decision": body.decision, "decided_by": actor,
                        "exp": time.time() + GRANT_SECONDS})
    status, reply = await call_agent(row["namespace"], row["agent"], "/resume", {"grant": grant})
    if status == 200:
        await audit(actor, f"approval_{body.decision}", f"{row['namespace']}/{row['agent']}",
                    {"approval_id": approval_id, "tool": request["tool"], "arguments": arguments})
    return JSONResponse(reply, status_code=status)


# ---- monitoring ------------------------------------------------------------------------------------------------

@api.get("/traces")
async def list_traces(namespace: str | None = None, agent: str | None = None, limit: int = 50, actor=Depends(user)):
    """Recent requests, newest first (without their steps)."""
    rows = await db().fetch(
        "select id, agent, namespace, conversation_id, message, answer, status, tokens, started_at, seconds, "
        "jsonb_array_length(steps) as step_count from traces where ($1::text is null or namespace=$1) and ($2::text is null or agent=$2) "
        "order by started_at desc limit $3", namespace, agent, min(limit, 200))
    return [dict(row) for row in rows]


@api.get("/traces/{trace_id}")
async def get_trace(trace_id: str, actor=Depends(user)):
    """One request with every step: model calls, tool calls, refusals, approvals."""
    row = await db().fetchrow("select * from traces where id=$1", trace_id)
    if row is None:
        raise HTTPException(404, "no such trace")
    return dict(row) | {"cost_usd": round(row["tokens"] * USD_PER_MILLION_TOKENS / 1e6, 5)}


@api.get("/audit")
async def get_audit(limit: int = 200, actor=Depends(user)):
    """The audit log, newest first: who changed what, every approval decision, every refusal."""
    rows = await db().fetch("select * from audit order by id desc limit $1", min(limit, 1000))
    return [dict(row) for row in rows]


@api.get("/usage")
async def get_usage(actor=Depends(user)):
    """Tokens and estimated cost per agent per day."""
    rows = await db().fetch("select namespace, agent, day, tokens from usage order by day desc, tokens desc limit 200")
    return [dict(row) | {"cost_usd": round(row["tokens"] * USD_PER_MILLION_TOKENS / 1e6, 4)} for row in rows]


@api.get("/events")
async def events(request: Request, actor=Depends(user)):
    """Server-Sent Events for the live graph: a snapshot when agents or the inbox change, and each new or updated trace.

    ponytail: polls Kubernetes and Postgres every 2 s per open console; a Kubernetes watch + Postgres LISTEN would push instead.
    """
    async def stream():
        last_snapshot, cursor = None, await db().fetchval("select now()")
        while not await request.is_disconnected():
            snapshot = {"agents": await agents_with_usage(),
                        "pending_approvals": len(await list_approvals("pending", actor))}  # same verified rows the inbox shows
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            rows = await db().fetch("select id, agent, namespace, status, steps, updated_at from traces where updated_at > $1 "
                                    "order by updated_at", cursor)
            for row in rows:
                cursor = row["updated_at"]
                used = [step["name"] for step in row["steps"] or [] if step["type"] == "tool"]
                yield f"event: trace\ndata: {json.dumps({'id': row['id'], 'agent': row['agent'], 'namespace': row['namespace'], 'status': row['status'], 'tools': used})}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.websocket("/api/voice")
async def voice(websocket: WebSocket):
    """Relay the console's voice call to the voice server (which nothing else can reach).

    The voice server holds no platform credential: each call gets its own session for the logged-in user, cut down to
    looking, drafting and chatting (never deploying or approving), and it ends when the call does.
    """
    caller = await actor_for(websocket.cookies.get("session", ""))
    origin = websocket.headers.get("origin", "")
    if caller is None or origin.split("://", 1)[-1] != websocket.headers.get("host"):  # cookie, from our own page
        await websocket.close(code=4401)
        return
    await websocket.accept()
    token = await start_session(f"{caller} (voice)", caller.permissions & VOICE_PERMISSIONS, hours=1)
    try:
        async with websockets.connect(VOICE_URL, additional_headers={"Authorization": f"Bearer {token}"}, max_size=None) as upstream:
            async def browser_to_voice():
                while (message := await websocket.receive())["type"] != "websocket.disconnect":
                    await upstream.send(message.get("bytes") or message.get("text") or b"")

            async def voice_to_browser():
                async for message in upstream:
                    await (websocket.send_bytes(message) if isinstance(message, bytes) else websocket.send_text(message))

            relays = [asyncio.create_task(browser_to_voice()), asyncio.create_task(voice_to_browser())]
            await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)  # either side hung up
            for relay in relays:
                relay.cancel()
    except (OSError, websockets.WebSocketException):
        logging.warning("voice server unreachable or the call dropped")
        closing = (4503, "voice server unavailable")
    else:
        closing = (1000, "")
    finally:
        await db().execute("delete from sessions where token_hash=$1", token_hash(token))
    try:
        await websocket.close(*closing)
    except RuntimeError:
        pass  # already closed


app.include_router(api)

from crash import router as crash_router  # noqa: E402 (these need everything above; they import from this module)

app.include_router(crash_router)


@app.get("/healthz")
async def healthz():
    """Readiness check."""
    return {"ok": True}


if pathlib.Path("static").is_dir():  # the built console (the Docker image puts it here)
    app.mount("/", StaticFiles(directory="static", html=True), name="console")
