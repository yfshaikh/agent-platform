"""The agent operator: watches Agent objects and creates the real Kubernetes objects for each one.

For every Agent it reconciles (creates or updates until they match):
  ServiceAccount  identity with no cluster permissions
  Secret          only the keys this agent needs: model key, database URL, tokens of its own tools
  Deployment      N pods running the shared agent runtime, configured from the Agent spec
  Service         one stable address for the agent
  NetworkPolicy   who it may reach (its tools, the model API, Postgres, agents it talks to) and who may reach it
All of them are "owned" by the Agent, so deleting the Agent deletes them (Kubernetes garbage collection).

Run in the cluster (deploy/platform/platform.yaml) or locally: uv run kopf run agent_operator.py --standalone --all-namespaces
"""
import base64
import fnmatch
import hashlib
import hmac
import json
import os
import secrets

import kopf
import kubernetes
from kubernetes.dynamic import DynamicClient

GROUP, VERSION = "platform.hacktx.dev", "v1"
PLATFORM_NAMESPACE = "platform"
RUNTIME_IMAGE = os.environ.get("RUNTIME_IMAGE", "agent-runtime:dev")
FIELD_MANAGER = "agent-operator"
RULE_STRICTNESS = {"auto": 0, "human": 1, "deny": 2}
CLASS_DEFAULT_RULE = {"read": "auto", "write": "auto", "sensitive": "human"}
DEFAULT_QUOTA = {"pods": "10", "requests.cpu": "4", "requests.memory": "4Gi", "limits.memory": "8Gi"}

clients = {}


@kopf.on.startup()
def connect(settings, **_):
    """Load cluster credentials (in-cluster ServiceAccount, else local kubeconfig) and skip kopf's peering setup."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
    api_client = kubernetes.client.ApiClient()
    clients["dynamic"] = DynamicClient(api_client)
    clients["core"] = kubernetes.client.CoreV1Api(api_client)
    clients["custom"] = kubernetes.client.CustomObjectsApi(api_client)
    settings.peering.standalone = True


def apply(manifest):
    """Server-side apply: create the object or update it to match, owning only the fields we set."""
    resource = clients["dynamic"].resources.get(api_version=manifest["apiVersion"], kind=manifest["kind"])
    clients["dynamic"].server_side_apply(resource, body=manifest, name=manifest["metadata"]["name"],
                                         namespace=manifest["metadata"].get("namespace"), field_manager=FIELD_MANAGER,
                                         force_conflicts=True)


def read_secret(namespace, name):
    """A Secret's data (still base64-encoded), or {} if it doesn't exist."""
    try:
        return clients["core"].read_namespaced_secret(name, namespace).data or {}
    except kubernetes.client.ApiException as error:
        if error.status == 404:
            return {}
        raise


def encode(value):
    """Base64 for Secret data."""
    return base64.b64encode(value.encode()).decode()


def list_agents(namespace=None):
    """All Agent objects in one namespace, or everywhere."""
    if namespace:
        return clients["custom"].list_namespaced_custom_object(GROUP, VERSION, namespace, "agents")["items"]
    return clients["custom"].list_cluster_custom_object(GROUP, VERSION, "agents")["items"]


def tool_registry():
    """Tool objects by name, from the platform namespace."""
    items = clients["custom"].list_namespaced_custom_object(GROUP, VERSION, PLATFORM_NAMESPACE, "tools")["items"]
    return {tool["metadata"]["name"]: tool for tool in items}


def is_terminating(namespace):
    """A namespace being deleted refuses new objects; everything in it is going away anyway."""
    try:
        return clients["core"].read_namespace(namespace).status.phase == "Terminating"
    except kubernetes.client.ApiException as error:
        if error.status == 404:
            return True
        raise


def validate(spec, tools):
    """Reject specs the platform won't run. Returns a list of problems (empty = fine)."""
    problems = []
    for entry in spec.get("tools", []):
        tool = tools.get(entry["name"])
        if tool is None:
            problems.append(f"unknown tool {entry['name']!r}")
            continue
        classes = tool["spec"]["classes"]
        for function in entry.get("allow", []):
            if function not in classes:
                problems.append(f"{entry['name']} has no function {function!r}")
        registry = {node["name"]: node for node in tool["spec"].get("nodes", [])}
        for node in entry.get("nodes", []):
            if node not in registry:
                problems.append(f"{entry['name']} has no node {node!r}")
            elif not registry[node].get("connected", True):
                problems.append(f"{entry['name']} node {node!r} isn't connected yet")
        if registry and not entry.get("nodes"):
            problems.append(f"{entry['name']}: pick the nodes this agent may use (tools[].nodes)")
        for function, rule in entry.get("approval", {}).items():
            default = CLASS_DEFAULT_RULE[classes.get(function, "sensitive")]
            if RULE_STRICTNESS[rule] < RULE_STRICTNESS[default]:
                problems.append(f"{entry['name']}.{function} is {classes.get(function, 'sensitive')}: "
                                f"approval can't be loosened to {rule!r}")
    return problems


def owner_reference(agent):
    """Makes the Agent the owner, so Kubernetes deletes these objects when the Agent is deleted."""
    return [{"apiVersion": f"{GROUP}/{VERSION}", "kind": "Agent", "name": agent["metadata"]["name"],
             "uid": agent["metadata"]["uid"], "controller": True, "blockOwnerDeletion": True}]


def ensure_namespace_basics(namespace):
    """Per namespace, once: a ResourceQuota and the caller token every agent there checks on /chat."""
    apply({"apiVersion": "v1", "kind": "ResourceQuota",
           "metadata": {"name": "agent-quota", "namespace": namespace}, "spec": {"hard": DEFAULT_QUOTA}})
    try:  # create-if-missing without reading it: the operator may write Secrets in agent namespaces but not read them
        clients["core"].create_namespaced_secret(namespace, {
            "metadata": {"name": "agent-caller-token"}, "data": {"AGENT_CALLER_TOKEN": encode(secrets.token_urlsafe(32))}})
    except kubernetes.client.ApiException as error:
        if error.status != 409:  # 409 = it already exists, which is fine
            raise


def resolve_talks_to(agent, siblings):
    """Turn talksTo globs (like "resident-*") into the names of agents that exist now, never including itself."""
    name = agent["metadata"]["name"]
    patterns = agent["spec"].get("talksTo", [])
    return sorted(other for other in siblings if other != name and any(fnmatch.fnmatch(other, pattern) for pattern in patterns))


def build_objects(agent, tools, siblings_by_name):
    """Everything one Agent becomes, as manifests. Pure function of its inputs (easy to read and test)."""
    name, namespace, spec = agent["metadata"]["name"], agent["metadata"]["namespace"], agent["spec"]
    resource_name = f"agent-{name}"
    labels = {"platform.hacktx.dev/agent": name, "app.kubernetes.io/managed-by": FIELD_MANAGER}
    owner = owner_reference(agent)
    platform_keys = read_secret(PLATFORM_NAMESPACE, "platform-keys")

    secret_data = {key: platform_keys[key] for key in ("OPENROUTER_API_KEY", "DATABASE_URL", "APPROVAL_PUBLIC_KEY")
                   if key in platform_keys}
    if "AGENT_KEY_MASTER" in platform_keys:  # this agent's own key for stamping approval requests (the API derives the same)
        master = base64.b64decode(platform_keys["AGENT_KEY_MASTER"])
        secret_data["AGENT_SIGNING_KEY"] = encode(hmac.new(master, f"{namespace}/{name}".encode(), hashlib.sha256).hexdigest())
    tool_servers = []
    for entry in spec.get("tools", []):
        tool = tools[entry["name"]]
        token_env = f"TOOL_{entry['name'].upper().replace('-', '_')}_TOKEN"
        token = read_secret(PLATFORM_NAMESPACE, tool["spec"]["secretRef"]).get("TOOL_TOKEN")
        if token:
            secret_data[token_env] = token
        tool_servers.append({"name": entry["name"], "url": tool["spec"]["url"], "token_env": token_env,
                             "classes": tool["spec"]["classes"], "allow": entry.get("allow", []),
                             "approval": entry.get("approval", {}), "approvalAbove": entry.get("approvalAbove", {}),
                             "nodes": entry.get("nodes", [])})

    talks_to = resolve_talks_to(agent, siblings_by_name)
    talked_to_by = sorted(other for other, other_agent in siblings_by_name.items()
                          if other != name and name in resolve_talks_to(other_agent, siblings_by_name))
    budget = spec.get("budget", {})
    environment = {
        "AGENT_NAME": name, "AGENT_NAMESPACE": namespace, "MODEL": spec["model"], "INSTRUCTIONS": spec["instructions"],
        "TOOL_SERVERS": json.dumps(tool_servers), "TALKS_TO": json.dumps(talks_to),
        "MAX_STEPS": str(budget.get("maxStepsPerRequest", 12)), "TOKENS_PER_DAY": str(budget.get("tokensPerDay", 2000000)),
    }
    # Changing config or keys changes this hash, which rolls the pods so they pick up the new values.
    config_hash = hashlib.sha256(json.dumps([environment, secret_data], sort_keys=True).encode()).hexdigest()[:16]

    def agent_pods(names):
        """Pod selector for a list of agents in this namespace."""
        return {"podSelector": {"matchExpressions": [{"key": "platform.hacktx.dev/agent", "operator": "In", "values": names}]}}

    egress = [
        {"to": [{"namespaceSelector": {}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
         "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
        # ponytail: the model API (OpenRouter) has no fixed IP, so HTTPS to the internet is open; an egress proxy would pin it
        {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]}}],
         "ports": [{"protocol": "TCP", "port": 443}]},
        {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": PLATFORM_NAMESPACE}},
                 "podSelector": {"matchLabels": {"app": "postgres"}}}], "ports": [{"protocol": "TCP", "port": 5432}]},
    ]
    if tool_servers:
        egress.append({"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": PLATFORM_NAMESPACE}},
                               "podSelector": {"matchExpressions": [{"key": "platform.hacktx.dev/tool", "operator": "In",
                                                                     "values": [server["name"] for server in tool_servers]}]}}],
                       "ports": [{"protocol": "TCP", "port": 8000}]})
    if talks_to:
        egress.append({"to": [agent_pods(talks_to)], "ports": [{"protocol": "TCP", "port": 8000}]})
    ingress = [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": PLATFORM_NAMESPACE}},
                          "podSelector": {"matchLabels": {"app": "platform-api"}}}], "ports": [{"protocol": "TCP", "port": 8000}]}]
    if talked_to_by:
        ingress.append({"from": [agent_pods(talked_to_by)], "ports": [{"protocol": "TCP", "port": 8000}]})

    metadata = {"name": resource_name, "namespace": namespace, "labels": labels, "ownerReferences": owner}
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata, "automountServiceAccountToken": False},
        {"apiVersion": "v1", "kind": "Secret", "metadata": {**metadata, "name": f"{resource_name}-keys"}, "data": secret_data},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata, "spec": {
            "replicas": spec.get("replicas", 1),
            "selector": {"matchLabels": {"platform.hacktx.dev/agent": name}},
            "template": {
                "metadata": {"labels": labels, "annotations": {"platform.hacktx.dev/config-hash": config_hash}},
                "spec": {
                    "serviceAccountName": resource_name, "automountServiceAccountToken": False,
                    "containers": [{
                        "name": "runtime", "image": RUNTIME_IMAGE, "imagePullPolicy": "IfNotPresent",
                        "ports": [{"containerPort": 8000}],
                        "env": [{"name": key, "value": value} for key, value in environment.items()] + [
                            {"name": "AGENT_CALLER_TOKEN", "valueFrom": {"secretKeyRef": {"name": "agent-caller-token", "key": "AGENT_CALLER_TOKEN"}}}],
                        "envFrom": [{"secretRef": {"name": f"{resource_name}-keys"}}],
                        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
                        "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8000}, "periodSeconds": 5},
                        "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8000}, "periodSeconds": 20, "initialDelaySeconds": 10},
                        "securityContext": {"allowPrivilegeEscalation": False, "runAsNonRoot": True, "runAsUser": 10001,
                                            "capabilities": {"drop": ["ALL"]}},
                    }],
                },
            },
        }},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"selector": {"platform.hacktx.dev/agent": name}, "ports": [{"port": 80, "targetPort": 8000}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata, "spec": {
            "podSelector": {"matchLabels": {"platform.hacktx.dev/agent": name}},
            "policyTypes": ["Ingress", "Egress"], "ingress": ingress, "egress": egress}},
    ]


def set_status(agent, **fields):
    """Patch an Agent's status (phase, message, readyReplicas)."""
    clients["custom"].patch_namespaced_custom_object_status(
        GROUP, VERSION, agent["metadata"]["namespace"], "agents", agent["metadata"]["name"], {"status": fields})


announced = set()  # (agent uid, generation) whose objects were already reported as events. ponytail: in memory


def reconcile_namespace(namespace, logger):
    """Reconcile every Agent in a namespace. Siblings matter: one agent's talksTo changes another's network rules."""
    agents = list_agents(namespace)
    if not agents or is_terminating(namespace):  # otherwise the delete handler retries forever and blocks namespace deletion
        return
    tools = tool_registry()
    ensure_namespace_basics(namespace)
    siblings = {agent["metadata"]["name"]: agent for agent in agents if not agent["metadata"].get("deletionTimestamp")}
    for agent in siblings.values():
        problems = validate(agent["spec"], tools)
        if problems:
            set_status(agent, phase="Failed", message="; ".join(problems))
            logger.warning(f"{namespace}/{agent['metadata']['name']} rejected: {problems}")
            continue
        version = (agent["metadata"]["uid"], agent["metadata"].get("generation"))
        if version not in announced:
            kopf.info(agent, reason="Validated", message="spec checked: every tool and function exists, approval rules only tighten")
        for manifest in build_objects(agent, tools, siblings):
            apply(manifest)
            if version not in announced:  # Kubernetes events, so the console's deploy log can show each step
                kopf.info(agent, reason="Applied", message=f"server-side apply {manifest['kind']} {manifest['metadata']['name']}")
        announced.add(version)
        if agent.get("status", {}).get("phase") == "Failed":
            set_status(agent, phase="Pending", message="")


@kopf.on.resume(GROUP, VERSION, "agents")
@kopf.on.create(GROUP, VERSION, "agents")
@kopf.on.update(GROUP, VERSION, "agents", field="spec")
def agent_changed(namespace, logger, **_):
    """An Agent was created or its spec changed (or the operator restarted): reconcile its namespace."""
    reconcile_namespace(namespace, logger)


@kopf.on.delete(GROUP, VERSION, "agents", optional=True)
def agent_deleted(namespace, logger, **_):
    """Its own objects are garbage-collected; siblings that talked to it need new network rules."""
    reconcile_namespace(namespace, logger)


@kopf.on.update(GROUP, VERSION, "tools", field="spec")
def tool_changed(name, logger, **_):
    """A Tool's URL or classes changed: re-render every namespace with an agent that uses it."""
    for namespace in {agent["metadata"]["namespace"] for agent in list_agents()
                      if any(entry["name"] == name for entry in agent["spec"].get("tools", []))}:
        reconcile_namespace(namespace, logger)


@kopf.timer(GROUP, VERSION, "agents", interval=5, idle=0)
def report_status(body, namespace, name, **_):
    """Copy the Deployment's readiness into the Agent's status, so `kubectl get agents` shows real state."""
    if body.get("status", {}).get("phase") == "Failed":
        return
    try:
        deployment = kubernetes.client.AppsV1Api().read_namespaced_deployment(f"agent-{name}", namespace)
    except kubernetes.client.ApiException:
        return
    wanted = deployment.spec.replicas or 0
    ready = deployment.status.ready_replicas or 0
    phase = "Stopped" if wanted == 0 else "Running" if ready >= wanted else "Pending"
    if body.get("status", {}).get("phase") != phase or body.get("status", {}).get("readyReplicas") != ready:
        set_status(body, phase=phase, readyReplicas=ready)
