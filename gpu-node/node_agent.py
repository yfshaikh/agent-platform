"""The GPU node agent: runs on the GPU machine and is the only thing the platform's gpu-fleet tool talks to.

It reports real readings from nvidia-smi and runs jobs as Docker containers, picked from a fixed list (never a
command someone typed). It listens on 127.0.0.1 only: the platform reaches it through an SSH tunnel, with a token.
Agents never get a way to create or delete machines; a person makes the machine.

Run on the GPU box:  NODE_TOKEN=... python3 node_agent.py      (standard library only)
"""
import hmac
import json
import os
import pathlib
import re
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["NODE_TOKEN"]
PORT = int(os.environ.get("NODE_PORT", "9400"))
IMAGE = os.environ.get("JOB_IMAGE", "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime")
JOBS_DIR = pathlib.Path(__file__).parent / "jobs"
TEMPLATES = {path.stem: path.name for path in JOBS_DIR.glob("*.py")}  # train, memory-hog, crashy-train, burn
MAX_JOBS = 4
JOB_NAME = re.compile(r"^job-[a-z-]+-[0-9a-f]{6}$")
CORDON_FILE = pathlib.Path(__file__).parent / ".cordoned"  # present = take no new jobs
capacity = threading.Lock()  # one start or restart at a time, so two requests can't both slip under MAX_JOBS
GPU_FIELDS = ["index", "name", "temperature.gpu", "utilization.gpu", "memory.used", "memory.total", "power.draw",
              "power.limit", "clocks_throttle_reasons.active", "ecc.errors.uncorrected.volatile.total"]


def run(*command):
    """Run a command; returns (exit code, combined output)."""
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    return result.returncode, (result.stdout + result.stderr).strip()


def number(text):
    """nvidia-smi values as numbers where they are numbers ("[N/A]" stays text)."""
    try:
        return float(text) if "." in text else int(text, 0)
    except ValueError:
        return text


def gpus():
    """Every GPU's live readings."""
    code, output = run("nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits")
    if code != 0:
        raise RuntimeError(f"nvidia-smi failed: {output[:200]}")
    return [dict(zip(GPU_FIELDS, map(number, (value.strip() for value in line.split(","))))) for line in output.splitlines()]


def jobs():
    """Every job container (running or finished) with its state and exit code."""
    _, output = run("docker", "ps", "-a", "--filter", "label=nmc.job", "--format", "{{.Names}}")
    found = []
    for name in output.split():
        _, inspected = run("docker", "inspect", name, "--format",
                           '{{json .State}}|{{index .Config.Labels "nmc.job"}}')
        state, template = inspected.rsplit("|", 1)
        state = json.loads(state)
        found.append({"name": name, "template": template, "status": state["Status"], "exit_code": state["ExitCode"],
                      "oom_killed": state["OOMKilled"], "started_at": state["StartedAt"], "finished_at": state["FinishedAt"]})
    return found


def check_capacity(running_now):
    """Refuse new work on a cordoned or full node (call while holding `capacity`)."""
    if CORDON_FILE.exists():
        raise ValueError("this node is cordoned: it takes no new jobs")
    if running_now >= MAX_JOBS:
        raise ValueError(f"this node already runs {MAX_JOBS} jobs")


def start_job(template):
    """Start one job from the fixed list, unless the node is cordoned or full."""
    if template not in TEMPLATES:
        raise ValueError(f"unknown job {template!r}; choose from {sorted(TEMPLATES)}")
    with capacity:
        check_capacity(sum(job["status"] == "running" for job in jobs()))
        name = f"job-{template}-{secrets.token_hex(3)}"
        code, output = run("docker", "run", "-d", "--gpus", "all", "--name", name, "--label", f"nmc.job={template}",
                           "-v", f"{JOBS_DIR}:/jobs:ro", IMAGE, "python", f"/jobs/{TEMPLATES[template]}")
    if code != 0:
        raise RuntimeError(f"couldn't start the job: {output[-300:]}")
    return {"name": name, "template": template}


def job_action(name, action):
    """stop, restart or logs for one job container."""
    current = {job["name"]: job for job in jobs()}
    if not JOB_NAME.fullmatch(name) or name not in current:
        raise ValueError(f"no job called {name!r}")
    if action == "logs":
        return {"name": name, "logs": run("docker", "logs", "--tail", "40", name)[1]}
    if action == "restart":  # bringing a finished job back is new work: same rules as starting one
        with capacity:
            if current[name]["status"] != "running":
                check_capacity(sum(job["status"] == "running" for job in current.values()))
            code, output = run("docker", "restart", name)
    else:
        code, output = run("docker", action, name)
    if code != 0:
        raise RuntimeError(output[-300:])
    return {"name": name, "done": action}


class Handler(BaseHTTPRequestHandler):
    """GET /gpus, /jobs, /jobs/<name>/logs, /node   POST /jobs {template}, /jobs/<name>/stop|restart, /cordon {on}"""
    timeout = 30  # a client that stops sending can't hold a thread forever

    def respond(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_request(self, method):
        if not hmac.compare_digest(self.headers.get("authorization", "").encode(), f"Bearer {TOKEN}".encode()):
            return self.respond(401, {"error": "bad token"})
        parts = self.path.strip("/").split("/")
        try:
            length = int(self.headers.get("content-length") or 0)
            if not 0 <= length <= 10_000:
                raise ValueError("bad content-length")
            body = json.loads(self.rfile.read(length) or b"{}") if method == "POST" else {}
            if not isinstance(body, dict):
                raise ValueError("the body must be a JSON object")
            if method == "GET" and parts == ["gpus"]:
                return self.respond(200, gpus())
            if method == "GET" and parts == ["jobs"]:
                return self.respond(200, jobs())
            if method == "GET" and parts == ["node"]:
                return self.respond(200, {"cordoned": CORDON_FILE.exists(), "templates": sorted(TEMPLATES), "max_jobs": MAX_JOBS})
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "logs":
                return self.respond(200, job_action(parts[1], "logs"))
            if method == "POST" and parts == ["jobs"]:
                return self.respond(200, start_job(str(body.get("template", ""))))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] in ("stop", "restart"):
                return self.respond(200, job_action(parts[1], parts[2]))
            if method == "POST" and parts == ["cordon"]:
                CORDON_FILE.touch() if body.get("on", True) else CORDON_FILE.unlink(missing_ok=True)
                return self.respond(200, {"cordoned": CORDON_FILE.exists()})
            return self.respond(404, {"error": "not found"})
        except ValueError as error:
            return self.respond(400, {"error": str(error)})
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:  # OSError: nvidia-smi or docker missing
            return self.respond(502, {"error": str(error)})

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
