# Agent Platform (HackTX 2026, NMC² track)

A platform that lets users quickly create AI agents for enterprise-grade use.
Users write a short `Agent` object (model, instructions, tools, replicas, budget);
a Kubernetes operator turns it into an isolated, monitored, permission-limited deployment.

The pitch: the agent proposes, the platform enforces.

Built for NMC²'s GPU cloud: their engineers build agents that help run it, and the platform decides what those agents
may touch. People sign in with their own name and role; a voice assistant can look, draft and chat, but deploying and
approving always take a click.

## What's here

| Folder | What |
|---|---|
| `api/` | platform API (FastAPI): agents CRUD, drafts, chat proxy, approval inbox (signs grants), traces, audit, live events, sign-in, people and roles, the voice relay; serves the console |
| `console/` | web console (React + React Flow): live agent map, build, approvals, traces, audit log, people, voice dock |
| `voice/` | voice server (Pipecat: Cartesia speech, OpenRouter model), reachable only through the API |
| `runtime/` | the agent runtime every agent pod runs: model-and-tool loop, policy, approvals, budgets, traces |
| `operator/` | kopf operator: turns `Agent` objects into ServiceAccount, Secret, Deployment, Service, NetworkPolicy |
| `tools/` | MCP tool servers: Nessie bank, SerpApi search |
| `deploy/crds/` | the `Agent` and `Tool` kinds |
| `deploy/platform/` | Postgres, operator, tool servers, tool registry |
| `deploy/examples/` | a research agent and a bank assistant |

## Run it locally (kind)

```bash
# images, built and loaded into the kind cluster "f1"
docker build -t agent-runtime:dev runtime && docker build -t platform-tools:dev tools && docker build -t agent-operator:dev operator
docker build -t platform-api:dev -f api/Dockerfile .                    # builds the console into the API image
docker build -t platform-voice:dev voice
kind load docker-image agent-runtime:dev platform-tools:dev agent-operator:dev platform-api:dev platform-voice:dev --name f1

kubectl --context kind-f1 apply -f deploy/crds/
uv run --no-project --with cryptography --env-file .env python deploy/sync_secrets.py   # keys -> Secrets, never printed (voice needs CARTESIA_API_KEY)
kubectl --context kind-f1 apply -f deploy/platform/platform.yaml -f deploy/platform/tools.yaml
kubectl --context kind-f1 apply -f deploy/examples/

kubectl --context kind-f1 get agents -A
uv run --project runtime python deploy/e2e_chunk_a.py                  # core platform checks
uv run --project runtime python deploy/e2e_chunk_b.py                  # API, approvals, security checks
uv run --project api python deploy/e2e_chunk_c.py                      # sign-in, roles, voice relay
uv run --project runtime python deploy/e2e_chunk_e.py                  # crash test and deploy gate (~$0.10)
```

Open the console:

```bash
kubectl --context kind-f1 -n platform port-forward svc/platform-api 8080:80
uv run --project api python deploy/login_link.py                       # one-time admin link (first sign-in, or break-glass)
```

Then add people under **People** (viewer, builder, approver, admin); they sign in with a name and password.

Offline checks: `cd runtime && uv run python test_agent.py` · `cd tools && NESSIE_MOCK=1 uv run python test_nessie.py && uv run python test_grants.py`
