"""End-to-end check of the platform API, approvals and the Chunk B security fixes on a live cluster.

Done-when: create an agent from a template, see it in the live feed, chat with it, approve a transfer from the inbox,
see the trace and audit entries, stop it. Plus: forged/old-style approvals refused, tool server refuses sensitive calls
without a grant, agents' database login is limited, Postgres and tools are firewalled, the operator can't read Secrets.
Run from agent-platform/:  uv run --project runtime python deploy/e2e_chunk_b.py
"""
import base64
import json
import subprocess
import sys
import threading
import time

import httpx

from e2e_chunk_a import Agent, caller_token, free_port, kubectl, record, results

API_NAMESPACE = "platform"
TEAM = "team-b"


def secret_value(namespace, name, key):
    """Read one Secret value for use in this test; never printed."""
    data = json.loads(kubectl("get", "secret", name, "-n", namespace, "-o", "json"))["data"]
    return base64.b64decode(data[key]).decode()


def pod_reaches(namespace, selector, host, port):
    """From the first pod matching `selector`, try a TCP connection (that pod's NetworkPolicy applies). True if it connects."""
    pod = kubectl("get", "pod", "-n", namespace, "-l", selector, "-o", "jsonpath={.items[0].metadata.name}")
    code = (f"import socket\ns=socket.socket();s.settimeout(4)\n"
            f"try:\n s.connect(('{host}',{port}));print('open')\nexcept Exception as e:\n print('blocked')")
    return kubectl("exec", "-n", namespace, pod, "--", "python", "-c", code, check=False).strip() == "open"


class PortForward:
    """kubectl port-forward to a Service; the base URL is in .url."""

    def __init__(self, namespace, service, remote_port):
        """Start forwarding and wait until it accepts connections."""
        port = free_port()
        self.url = f"http://127.0.0.1:{port}"
        self.process = subprocess.Popen(["kubectl", "--context", "kind-f1", "-n", namespace, "port-forward", f"svc/{service}", f"{port}:{remote_port}"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                httpx.get(self.url, timeout=1)
                return
            except httpx.HTTPError:
                time.sleep(0.25)

    def close(self):
        """Stop forwarding."""
        self.process.terminate()


def listen_for_events(url, headers, seen, stop):
    """Collect Server-Sent Events (event name, data) into `seen` until `stop` is set."""
    try:
        with httpx.stream("GET", f"{url}/api/events", headers=headers, timeout=None) as response:
            event = None
            for line in response.iter_lines():
                if stop.is_set():
                    return
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    seen.append((event, json.loads(line[6:])))
    except httpx.HTTPError:  # the port-forward closing at the end of the test
        return


def wait_until(check, timeout=90):
    """Poll `check()` until it returns something truthy; returns the last value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(2)
    return check()


def main():
    """Run every check, then exit non-zero if any failed."""
    api = PortForward(API_NAMESPACE, "platform-api", 80)
    headers = {"Authorization": f"Bearer {secret_value(API_NAMESPACE, 'platform-api', 'PLATFORM_TOKEN')}"}
    client = httpx.Client(base_url=f"{api.url}/api", headers=headers, timeout=200)

    # 1. Login is required; the console is served.
    record("API refuses requests without login", httpx.get(f"{api.url}/api/agents").status_code == 401)
    record("console page is served at /", "<div id=\"root\">" in httpx.get(api.url).text)
    link = client.post("/login-links").json()["path"]
    login = httpx.get(f"{api.url}{link}", follow_redirects=False)
    reuse = httpx.get(f"{api.url}{link}", follow_redirects=False)
    record("one-time login link sets the session cookie, and only once", login.status_code == 303 and "session" in login.cookies and reuse.status_code == 403)

    # 2. Draft from plain English (real model): valid against the CRD schema and the tool registry.
    draft = client.post("/agents/draft", json={"description": "An agent that compares prices for a product across online stores"}).json()
    record("draft from a description is a valid spec", not draft.get("problems") and draft.get("spec", {}).get("tools"),
           f"{draft.get('name')}: tools {draft.get('spec', {}).get('tools')}, problems {draft.get('problems')}")

    # 3. Create from a template; it shows up in the live event stream; chat with it.
    seen, stop = [], threading.Event()
    threading.Thread(target=listen_for_events, args=(api.url, headers, seen, stop), daemon=True).start()
    template = next(item for item in client.get("/templates").json() if item["id"] == "research-assistant")
    created = client.post("/agents", json={"namespace": TEAM, "name": "helper", "spec": template["spec"]})
    record("create an agent from a template", created.status_code == 200, f"HTTP {created.status_code}")
    running = wait_until(lambda: any(agent["name"] == "helper" and agent["status"].get("phase") == "Running"
                                     for event, data in seen if event == "snapshot" for agent in data["agents"]))
    record("new agent appears Running in the live event stream", running)
    reply = client.post(f"/agents/{TEAM}/helper/chat", json={"message": "In one sentence: what is Kubernetes?"}).json()
    record("chat with it through the API", reply.get("status") == "answered" and reply.get("answer"), (reply.get("answer") or "")[:80])
    record("its trace arrives on the live stream", wait_until(lambda: any(event == "trace" and data["id"] == reply.get("trace_id") for event, data in seen), 15))
    record("reserved namespaces are refused", client.post("/agents", json={"namespace": "platform", "name": "x", "spec": template["spec"]}).status_code == 400)

    # 4. Approve a transfer from the inbox: runs exactly once; deciding again fails; a denied one never runs.
    paused = client.post("/agents/team-demo/bank-assistant/chat", json={"message": "Please move $20 from my checking to my savings account."}).json()
    inbox = client.get("/approvals").json()
    record("transfer waits in the approval inbox", paused.get("status") == "awaiting_approval" and any(item["id"] == paused.get("approval_id") for item in inbox),
           f"{paused.get('status')} {paused.get('arguments')}")
    if paused.get("approval_id"):
        resumed = client.post(f"/approvals/{paused['approval_id']}", json={"decision": "approve"})
        steps = [step for step in resumed.json().get("steps", []) if step.get("name") == "nessie__transfer_money"]
        record("approved transfer runs once, through the tool server's grant check", len(steps) == 1 and '"completed"' in steps[0]["result"],
               steps[0]["result"][:100] if steps else resumed.text[:150])
        again = client.post(f"/approvals/{paused['approval_id']}", json={"decision": "approve"})
        record("an approval can't be decided twice", again.status_code == 404, f"HTTP {again.status_code}")
        trace = client.get(f"/traces/{paused['trace_id']}").json()
        record("trace shows the approved transfer step", any(step.get("decision") == "approve" for step in trace.get("steps", [])))
    denied = client.post("/agents/team-demo/bank-assistant/chat", json={"message": "Move $5 from checking to savings."}).json()
    if denied.get("approval_id"):
        after = client.post(f"/approvals/{denied['approval_id']}", json={"decision": "deny"}).json()
        record("denied transfer never runs", "the user denied this action" in [step.get("result") for step in after.get("steps", [])])
    else:
        record("denied transfer never runs", False, f"did not pause: {denied.get('status')}")
    actions = [(row["actor"], row["action"]) for row in client.get("/audit").json()]
    record("audit log names the user for create, approve and deny",
           {("admin", "agent_created"), ("admin", "approval_approve"), ("admin", "approval_deny")} <= set(actions))

    # 5. Approvals can't be forged or replayed around the API.
    bank = Agent("bank-assistant", caller_token())
    forged = bank.post("/resume", {"grant": "e30.AAAA"})
    old_style = bank.post("/resume", {"approval_id": paused.get("approval_id", "x"), "decision": "approve", "decided_by": "me"})
    record("agent refuses a forged approval grant", forged[0] == 403, f"HTTP {forged[0]}")
    record("agent refuses the old unsigned /resume body", old_style[0] == 422, f"HTTP {old_style[0]}")
    bank.close()
    tool = PortForward(API_NAMESPACE, "tool-nessie", 8000)
    raw_call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "transfer_money", "arguments": {
        "from_account_id": "f26755f9-dfc1-4062-9234-8395cd1d16af", "to_account_id": "6890f1fb-7a32-4c41-a36a-8347527ce5e7", "amount": 1, "description": "x"}}}
    direct = httpx.post(f"{tool.url}/mcp", json=raw_call, timeout=10, headers={
        "Authorization": f"Bearer {secret_value(API_NAMESPACE, 'tool-nessie-token', 'TOOL_TOKEN')}", "Accept": "application/json, text/event-stream"})
    record("tool server refuses transfer_money with a valid token but no grant", direct.status_code == 403, f"HTTP {direct.status_code}: {direct.text[:60]}")
    tool.close()

    # 6. Agents' database login is limited; Postgres and tools are firewalled; the operator can't read Secrets.
    postgres = kubectl("get", "pod", "-n", API_NAMESPACE, "-l", "app=postgres", "-o", "jsonpath={.items[0].metadata.name}")
    agent_password = secret_value(API_NAMESPACE, "postgres", "AGENT_DB_PASSWORD")

    def as_agent(sql):
        """Run SQL as the agents' database login; returns psql's combined output."""
        completed = subprocess.run(["kubectl", "--context", "kind-f1", "-n", API_NAMESPACE, "exec", postgres, "--", "env", f"PGPASSWORD={agent_password}",
                                    "psql", "-h", "127.0.0.1", "-U", "agent_runtime", "-d", "platform", "-tAc", sql], capture_output=True, text=True)
        return completed.stdout + completed.stderr

    record("agent DB login can read its tables", as_agent("select count(*) from traces").strip().isdigit())
    record("agent DB login can't delete the audit log", "permission denied" in as_agent("delete from audit"))
    record("agent DB login can't create tables", "permission denied" in as_agent("create table sneaky (x int)"))
    record("agent DB login can't rewrite a pending approval's arguments", "permission denied" in as_agent("update approvals set arguments='{}'"))
    forged_id = f"forged{int(time.time())}"
    as_agent(f"insert into approvals (id, agent, namespace, tool, arguments, state, payload, mac) values ('{forged_id}', 'bank-assistant', "
             f"'team-demo', 'nessie__transfer_money', '{{}}', '{{}}', '{{\"tool\": \"nessie__transfer_money\"}}', '00')")
    hidden = forged_id not in [item["id"] for item in client.get("/approvals").json()]
    refused = client.post(f"/approvals/{forged_id}", json={"decision": "approve"}).status_code
    record("an approval written by another agent is hidden and can't be approved", hidden and refused == 409, f"hidden {hidden}, HTTP {refused}")
    record("NetworkPolicy: an agent pod reaches Postgres", pod_reaches("team-demo", "platform.hacktx.dev/agent=bank-assistant", "postgres.platform.svc", 5432))
    record("NetworkPolicy: a non-agent pod can't reach Postgres", not pod_reaches(API_NAMESPACE, "platform.hacktx.dev/tool=serpapi", "postgres.platform.svc", 5432))
    record("NetworkPolicy: a non-agent pod can't reach a tool server", not pod_reaches(API_NAMESPACE, "app=agent-operator", "tool-nessie.platform.svc", 8000))
    operator = "system:serviceaccount:platform:agent-operator"

    def can(*arguments):
        """kubectl auth can-i as the operator."""
        return kubectl("auth", "can-i", *arguments, "--as", operator, check=False).strip() == "yes"

    record("operator can't list Secrets anywhere", not can("list", "secrets", "-A"))
    record("operator can't read other Secrets, even in platform", not can("get", "secret/postgres", "-n", API_NAMESPACE)
           and not can("get", "secret/platform-api", "-n", API_NAMESPACE))
    record("operator can read the keys it needs", can("get", "secret/platform-keys", "-n", API_NAMESPACE))

    # 7. Kill switch and delete through the API.
    spec = client.get(f"/agents/{TEAM}/helper").json()["spec"]
    client.put(f"/agents/{TEAM}/helper", json={"spec": {**spec, "replicas": 0}})
    stopped = wait_until(lambda: client.get(f"/agents/{TEAM}/helper").json()["status"].get("phase") == "Stopped", 60)
    record("kill switch from the API stops the agent", stopped)
    client.delete(f"/agents/{TEAM}/helper")
    gone = wait_until(lambda: client.get(f"/agents/{TEAM}/helper").status_code == 404, 30)
    actions = [row["action"] for row in client.get("/audit").json()]
    record("delete removes it; stop and delete are audited", gone and "agent_stopped" in actions and "agent_deleted" in actions)
    stop.set()
    api.close()

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
