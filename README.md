# Agent Platform (HackTX 2026, NMC² track)

A platform that lets users quickly create AI agents for enterprise-grade use.
Users write a short `Agent` object (model, instructions, tools, replicas, budget);
a Kubernetes operator turns it into an isolated, monitored, permission-limited deployment.

The pitch: the agent proposes, the platform enforces.

Built for NMC²'s GPU cloud: their engineers build agents that help run it, and the platform decides what those agents
may touch. People sign in with their own name and role; a voice assistant can look, draft and chat, but deploying and
approving always take a click. A real NVIDIA T4 is connected: when a job on it crashes, an alert wakes the triage agent,
which proposes a fix that waits for a human.

## What's here

| Folder | What |
|---|---|
| `api/` | platform API (FastAPI): agents CRUD, drafts, chat proxy, approval inbox (signs grants), traces, audit, live events, sign-in, people and roles, the voice relay, crash tests, the GPUs page and GPU alerts; serves the console |
| `console/` | web console (React + React Flow): live agent map, build, GPUs, approvals, traces, audit log, people, voice dock |
| `voice/` | voice server (Pipecat: Cartesia speech, OpenRouter model), reachable only through the API |
| `runtime/` | the agent runtime every agent pod runs: model-and-tool loop, policy, approvals, node scopes, budgets, traces |
| `operator/` | kopf operator: turns `Agent` objects into ServiceAccount, Secret, Deployment, Service, NetworkPolicy |
| `tools/` | MCP tool servers: Nessie bank, SerpApi search, gpu-fleet (the GPU nodes) |
| `gpu-node/` | the node agent that runs on the GPU box: `nvidia-smi` readings and jobs from a fixed list (`gpu-node/jobs/`) |
| `deploy/crds/` | the `Agent` and `Tool` kinds |
| `deploy/platform/` | Postgres, operator, API, voice, tool servers, tool registry |
| `deploy/examples/` | a research agent and a bank assistant |

Needs: Docker Desktop (pods reach the GPU tunnel through `host.docker.internal`), `kind`, `kubectl`, `uv`, Node.js, and
`agent-platform/.env` filled in from `.env.example`.

## 1. Set up the cluster (once)

Run everything from `agent-platform/`.

```bash
kind create cluster --name f1                                           # skip if kind-f1 already exists
kubectl --context kind-f1 apply -f deploy/crds/
uv run --no-project --with cryptography --env-file .env python deploy/sync_secrets.py   # keys -> Secrets, never printed (also makes the GPU node token)
```

## 2. Build and deploy

```bash
docker build -t agent-runtime:dev runtime && docker build -t platform-tools:dev tools && docker build -t agent-operator:dev operator
docker build -t platform-api:dev -f api/Dockerfile .                    # builds the console into the API image
docker build -t platform-voice:dev voice
kind load docker-image agent-runtime:dev platform-tools:dev agent-operator:dev platform-api:dev platform-voice:dev --name f1
kubectl --context kind-f1 apply -f deploy/platform/platform.yaml -f deploy/platform/tools.yaml
kubectl --context kind-f1 apply -f deploy/examples/                     # optional: the research and bank examples
kubectl --context kind-f1 get pods -A                                   # wait until everything is Running
```

After changing code, rebuild just that image, load it, and restart its Deployment. For example, the API (and console):

```bash
docker build -t platform-api:dev -f api/Dockerfile . && kind load docker-image platform-api:dev --name f1
kubectl --context kind-f1 -n platform rollout restart deploy/platform-api
```

The others: `agent-operator`, `tool-gpu-fleet` / `tool-nessie` / `tool-serpapi` (image `platform-tools:dev`), `voice`.
Agent pods pick up a new `agent-runtime:dev` when they restart.

## 3. Start the servers and open the console

The cluster runs everything. Your Mac only needs a port-forward to reach the console; keep it open in its own terminal:

```bash
while true; do kubectl --context kind-f1 -n platform port-forward svc/platform-api 8080:80; sleep 1; done
```

The loop reconnects after the API restarts. Open http://localhost:8080. The first time, sign in with a one-time admin link:

```bash
uv run --project api python deploy/login_link.py
```

Then add people under **People** (viewer, builder, approver, admin); they sign in with a name and password.

To work on the console with hot reload, keep the port-forward running and start Vite, which proxies `/api` to it:

```bash
npm --prefix console install && npm --prefix console run dev           # http://localhost:5173
```

In the Claude desktop app, `.claude/launch.json` in the folder above this one has the same commands as `agent-console`
(the port-forward), `console-dev` (Vite) and `gpu-tunnel` (see below).

To stop: Ctrl-C the port-forward. The cluster keeps running inside Docker; quit Docker Desktop to pause all of it.

## 4. The GPU (NVIDIA Brev)

The GPUs page and the gpu-fleet tool use a real T4 on a Brev instance called `nmc-gpu`. Nothing on the box is open to
the internet: the node agent listens only on the box's `127.0.0.1:9400`, and the cluster reaches it through an SSH
tunnel from this Mac.

**Set up (once).** Install the Brev CLI and sign in. Create a T4 instance named `nmc-gpu` in the Brev console (we used
`n1-standard-1` with one `nvidia-tesla-t4` on GCP). Then install the node agent. This needs the cluster from step 1,
because it copies the node token from a cluster Secret.

```bash
brev login
brev refresh                                                            # adds the "nmc-gpu" SSH host
bash deploy/gpu_node_setup.sh                                           # copies gpu-node/, installs the gpu-node service, pulls the job image
```

Run `gpu_node_setup.sh` again after changing anything in `gpu-node/`.

**Start** (each session):

```bash
brev start nmc-gpu                                                      # takes a few minutes; check with: brev ls
brev refresh                                                            # the SSH address can change after a restart
while true; do bash deploy/gpu_tunnel.sh; sleep 2; done                 # keep open: the cluster's only path to the GPU
```

The node agent starts by itself when the box boots (systemd service `gpu-node`). Check that it's up:

```bash
ssh nmc-gpu nvidia-smi
ssh nmc-gpu 'systemctl is-active gpu-node'
```

The GPUs page should now show `nmc-gpu` online with live readings.

**Stop** (when you're done: it bills while it runs). Ctrl-C the tunnel, then:

```bash
ssh nmc-gpu 'docker rm -f $(docker ps -aq --filter label=nmc.job)'     # optional: clear old jobs
brev stop nmc-gpu
brev ls                                                                 # should say STOPPED
```

**See that it's real:** run `ssh nmc-gpu 'watch -n1 nvidia-smi'` while you press an incident button, and
`ssh nmc-gpu 'docker ps -a --filter label=nmc.job'` to list the jobs. Every call the platform makes to the box is in
`ssh nmc-gpu 'journalctl -u gpu-node -n 30 --no-pager'`.

If the GPUs page says "unreachable", check in this order: the instance (`brev ls`), the tunnel, then the service.

## 5. Checks

```bash
uv run --project runtime python deploy/e2e_chunk_a.py                  # core platform (needs deploy/examples/)
uv run --project runtime python deploy/e2e_chunk_b.py                  # API, approvals, security (needs deploy/examples/)
uv run --project api python deploy/e2e_chunk_c.py                      # sign-in, roles, voice relay
uv run --project runtime python deploy/e2e_chunk_e.py                  # crash test and deploy gate (~$0.10)
uv run --project runtime python deploy/e2e_chunk_h.py                  # GPU incident, triage asked in chat (needs the GPU)
uv run --project runtime python deploy/e2e_chunk_i.py                  # GPU attacks, then an alert wakes triage on its own (needs the GPU)
```

Offline checks: `cd runtime && uv run python test_agent.py` · `cd tools && NESSIE_MOCK=1 uv run python test_nessie.py && uv run python test_grants.py && uv run python test_gpu_fleet.py`
