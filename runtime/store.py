"""Postgres storage for the runtime: traces, approvals, audit log, token usage, conversations.

Every agent pod writes here, so pods stay stateless: any replica can resume an approval or
continue a conversation. Tables are created on startup if missing (no migration tool).
"""
import json
import uuid

import asyncpg

SCHEMA = """
create table if not exists traces (
  id text primary key, agent text, namespace text, conversation_id text, message text, answer text,
  status text, tokens int, started_at timestamptz default now(), seconds real, steps jsonb);
create table if not exists approvals (
  id text primary key, agent text, namespace text, tool text, arguments jsonb, state jsonb,
  status text default 'pending', decided_by text, created_at timestamptz default now(), decided_at timestamptz);
create table if not exists audit (
  id bigserial primary key, at timestamptz default now(), actor text, action text, target text, detail jsonb);
create table if not exists usage (
  agent text, namespace text, day date default current_date, tokens bigint default 0,
  primary key (agent, namespace, day));
create table if not exists conversations (
  id text primary key, agent text, namespace text, messages jsonb, updated_at timestamptz default now());
"""


class Store:
    """Thin async wrapper over a connection pool; one instance per runtime process."""

    def __init__(self, pool):
        """Keep the pool; use Store.connect to build one."""
        self.pool = pool

    @classmethod
    async def connect(cls, database_url):
        """Open a pool and make sure the tables exist."""
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
        async with pool.acquire() as connection:
            await connection.execute(SCHEMA)
        return cls(pool)

    async def tokens_today(self, agent, namespace):
        """Tokens this agent has used today."""
        value = await self.pool.fetchval(
            "select tokens from usage where agent=$1 and namespace=$2 and day=current_date", agent, namespace)
        return value or 0

    async def add_tokens(self, agent, namespace, tokens):
        """Add to today's token count for this agent."""
        await self.pool.execute(
            "insert into usage (agent, namespace, tokens) values ($1, $2, $3) "
            "on conflict (agent, namespace, day) do update set tokens = usage.tokens + excluded.tokens",
            agent, namespace, tokens)

    async def save_trace(self, trace):
        """Insert or replace a trace (a resumed request keeps its trace id and grows its steps)."""
        await self.pool.execute(
            "insert into traces (id, agent, namespace, conversation_id, message, answer, status, tokens, seconds, steps) "
            "values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) on conflict (id) do update set answer=excluded.answer, "
            "status=excluded.status, tokens=excluded.tokens, seconds=traces.seconds + excluded.seconds, steps=excluded.steps",
            trace["trace_id"], trace["agent"], trace["namespace"], trace.get("conversation_id"), trace["message"],
            trace["answer"], trace["status"], trace["tokens"], trace["seconds"], json.dumps(trace["steps"]))

    async def create_approval(self, agent, namespace, tool, arguments, state):
        """Save a paused request waiting for a human; returns the approval id."""
        approval_id = uuid.uuid4().hex[:12]
        await self.pool.execute(
            "insert into approvals (id, agent, namespace, tool, arguments, state) values ($1,$2,$3,$4,$5,$6)",
            approval_id, agent, namespace, tool, json.dumps(arguments), json.dumps(state))
        return approval_id

    async def take_approval(self, approval_id, agent, namespace, decision, decided_by):
        """Atomically mark a pending approval decided and return its saved state, or None.

        "Atomically" means two clicks can't both resume it: only the first update matches status='pending'.
        """
        row = await self.pool.fetchrow(
            "update approvals set status=$4, decided_by=$5, decided_at=now() "
            "where id=$1 and agent=$2 and namespace=$3 and status='pending' returning state",
            approval_id, agent, namespace, decision, decided_by)
        return json.loads(row["state"]) if row else None

    async def audit(self, actor, action, target, detail):
        """Append one event to the audit log."""
        await self.pool.execute(
            "insert into audit (actor, action, target, detail) values ($1,$2,$3,$4)", actor, action, target, json.dumps(detail))

    async def load_conversation(self, conversation_id, agent, namespace):
        """Earlier messages of a conversation with this agent (without the system prompt), or []."""
        value = await self.pool.fetchval(
            "select messages from conversations where id=$1 and agent=$2 and namespace=$3", conversation_id, agent, namespace)
        return json.loads(value) if value else []

    async def save_conversation(self, conversation_id, agent, namespace, messages):
        """Store a conversation's messages (without the system prompt)."""
        await self.pool.execute(
            "insert into conversations (id, agent, namespace, messages) values ($1,$2,$3,$4) "
            "on conflict (id) do update set messages=excluded.messages, updated_at=now()",
            conversation_id, agent, namespace, json.dumps(messages))
