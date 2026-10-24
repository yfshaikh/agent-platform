# Agent Platform (HackTX 2026, NMC² track)

A platform that lets users quickly create AI agents for enterprise-grade use.
Users write a short `Agent` object (model, instructions, tools, replicas, budget);
a Kubernetes operator turns it into an isolated, monitored, permission-limited deployment.

The pitch: the agent proposes, the platform enforces.

Showcase app on top: **Scam City**, a simulated town where AI scammers attack an AI bank assistant
(Capital One Nessie for accounts, SerpApi for real prices).

## What's here

| Folder | What |
|---|---|
| `api/` | platform API (FastAPI): agents CRUD, drafts, chat proxy, approval inbox (signs grants), traces, audit, live events, Scam City towns; serves the console |
| `console/` | web console (React + React Flow): live agent graph, create, approvals, traces, audit log |
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
kind load docker-image agent-runtime:dev platform-tools:dev agent-operator:dev platform-api:dev --name f1

kubectl --context kind-f1 apply -f deploy/crds/
uv run --no-project --with cryptography --env-file .env python deploy/sync_secrets.py   # keys -> Secrets, never printed
kubectl --context kind-f1 apply -f deploy/platform/platform.yaml -f deploy/platform/tools.yaml
kubectl --context kind-f1 apply -f deploy/examples/

kubectl --context kind-f1 get agents -A
uv run --project runtime python deploy/e2e_chunk_a.py                  # core platform checks
uv run --project runtime python deploy/e2e_chunk_b.py                  # API, approvals, security checks
uv run --project runtime python deploy/e2e_chunk_d.py                  # Scam City (real town, ~$0.30)
```

Open the console:

```bash
kubectl --context kind-f1 -n platform port-forward svc/platform-api 8080:80
uv run --project api python deploy/login_link.py                       # prints a one-time login link
```

Offline checks: `cd runtime && uv run python test_agent.py` · `cd tools && NESSIE_MOCK=1 uv run python test_nessie.py && uv run python test_grants.py`
