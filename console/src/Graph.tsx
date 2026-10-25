import { useContext, useEffect, useMemo, useState } from "react";
import { Background, Controls, Edge, Handle, Node, NodeProps, Position, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import YAML from "yaml";
import { Agent, api, go, MeContext, Reply, Tool, useCan } from "./api";
import type { TracePulse } from "./main";

const PULSE_MS = 4000;
const DEFAULT_TOKENS_PER_DAY = 2_000_000; // the runtime's default when a spec sets no budget

const phaseClass = (phase?: string) => ({ Running: "ok", Pending: "wait", Stopped: "off", Failed: "bad" })[phase ?? ""] ?? "wait";
const compact = (value: number) => Intl.NumberFormat("en", { notation: "compact" }).format(value);

type ModuleData = { agent: Agent; classes: Record<string, Tool["classes"]> };

// An agent, drawn as a rack module: status LED, a port per tool (amber if it can call something sensitive), budget bar.
function AgentModule({ data }: NodeProps<Node<ModuleData>>) {
  const { agent, classes } = data;
  const budget = agent.spec.budget?.tokensPerDay ?? DEFAULT_TOKENS_PER_DAY;
  const used = Math.min(100, (agent.tokens_today / budget) * 100);
  const tools = agent.spec.tools ?? [];
  return (
    <div className={`module ${phaseClass(agent.status.phase)}`}>
      <Handle type="target" position={Position.Left} />
      <div className="led" aria-hidden />
      <div className="module-body">
        <div className="module-top">
          <strong>{agent.name}</strong>
          <span className="pods" title="Ready pods / wanted">{agent.status.readyReplicas ?? 0}/{agent.spec.replicas ?? 1}</span>
        </div>
        <div className="team">{agent.status.phase ?? "Pending"} in {agent.namespace}</div>
        <div className="ports">
          {tools.length === 0 && <span className="port none">no tools</span>}
          {tools.map((tool) => {
            const functions = tool.allow ?? Object.keys(classes[tool.name] ?? {});
            const sensitive = functions.some((name) => (classes[tool.name]?.[name] ?? "sensitive") === "sensitive");
            return <span key={tool.name} className={`port ${sensitive ? "sensitive" : ""}`} title={sensitive ? "Can request actions that need a human" : undefined}>{tool.name}</span>;
          })}
        </div>
        <div className="budget">
          <div className="bar"><span style={{ width: `${used}%` }} /></div>
          <span>{compact(agent.tokens_today)} of {compact(budget)} tokens today</span>
        </div>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

// A tool server, drawn as a patch panel: its functions and who decides when they run.
function ToolPanel({ data }: NodeProps<Node<{ tool: Tool }>>) {
  const decides = { read: "runs", write: "runs", sensitive: "human approves" };
  return (
    <div className="panel-tool">
      <Handle type="target" position={Position.Left} />
      <strong>{data.tool.name}</strong>
      <p>{data.tool.description}</p>
      <ul>
        {Object.entries(data.tool.classes).map(([name, kind]) => (
          <li key={name} className={kind}>{name}<span>{decides[kind]}</span></li>
        ))}
      </ul>
      {data.tool.nodes && (
        <ul className="tool-nodes">
          {data.tool.nodes.map((node) => (
            <li key={node.name} className={node.connected ? "up" : ""}>{node.name}<span>{node.connected ? node.gpu : "not connected"}</span></li>
          ))}
        </ul>
      )}
    </div>
  );
}

const nodeTypes = { agent: AgentModule, tool: ToolPanel };
const globToRegex = (glob: string) => new RegExp(`^${glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`);

export function Graph({ agents: snapshot, pulse, selected, pending }: { agents: Agent[] | null; pulse: TracePulse | null; selected?: string; pending: number }) {
  const [tools, setTools] = useState<Tool[]>([]);
  const [hot, setHot] = useState<Record<string, number>>({});
  const [team, setTeam] = useState("all");
  const [width, setWidth] = useState(() => Math.round(window.innerWidth / 200));
  useEffect(() => {
    const update = () => setWidth(Math.round(window.innerWidth / 200)); // re-fit when the window changes size noticeably
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);
  useEffect(() => void api<Tool[]>("/tools").then(setTools), []);

  const agents = (snapshot ?? []).filter((agent) => !agent.namespace.startsWith("crash-")); // crash-test sandboxes come and go
  const teams = [...new Set(agents.map((agent) => agent.namespace))].sort();
  const shown = agents.filter((agent) => team === "all" || agent.namespace === team);

  // A new trace lights up the edges it used for a few seconds.
  useEffect(() => {
    if (!pulse) return;
    const until = Date.now() + PULSE_MS;
    const ids: Record<string, number> = {};
    for (const name of pulse.tools) {
      const server = name.split("__")[0];
      if (server === "platform") agents.forEach((other) => (ids[`talk:${pulse.namespace}/${pulse.agent}->${other.name}`] = until));
      else ids[`use:${pulse.namespace}/${pulse.agent}->${server}`] = until;
    }
    setHot((current) => ({ ...current, ...ids }));
    const timer = setTimeout(() => setHot((current) => ({ ...current })), PULSE_MS + 50);
    return () => clearTimeout(timer);
  }, [pulse]); // eslint-disable-line react-hooks/exhaustive-deps

  const { nodes, edges } = useMemo(() => {
    const now = Date.now();
    const classes = Object.fromEntries(tools.map((tool) => [tool.name, tool.classes]));
    const used = new Set(shown.flatMap((agent) => (agent.spec.tools ?? []).map((tool) => tool.name)));
    const visibleTools = team === "all" ? tools : tools.filter((tool) => used.has(tool.name));
    let toolY = 0; // stack tool panels by their height (one row per function), so taller ones never overlap the next
    const nodes: Node[] = visibleTools.map((tool) => {
      const node = { id: `tool:${tool.name}`, type: "tool", position: { x: 0, y: toolY }, data: { tool } };
      toolY += 110 + (Object.keys(tool.classes).length + (tool.nodes?.length ?? 0) + (tool.nodes ? 1 : 0)) * 19;
      return node;
    });
    const edges: Edge[] = [];
    const namespaces = [...new Set(shown.map((agent) => agent.namespace))].sort();
    namespaces.forEach((namespace, column) => {
      const members = shown.filter((agent) => agent.namespace === namespace);
      members.forEach((agent, row) => {
        const id = `${agent.namespace}/${agent.name}`;
        nodes.push({ id, type: "agent", position: { x: -400 - column * 330, y: row * 170 }, data: { agent, classes }, selected: selected === `${agent.namespace}.${agent.name}` });
        for (const tool of agent.spec.tools ?? []) {
          const edgeId = `use:${id}->${tool.name}`;
          const isHot = (hot[edgeId] ?? 0) > now;
          edges.push({ id: edgeId, source: id, target: `tool:${tool.name}`, animated: isHot, className: isHot ? "hot" : "" });
        }
        const patterns = (agent.spec.talksTo ?? []).map(globToRegex);
        for (const other of members) {
          if (other.name === agent.name || !patterns.some((pattern) => pattern.test(other.name))) continue;
          const edgeId = `talk:${id}->${other.name}`;
          const isHot = (hot[edgeId] ?? 0) > now;
          edges.push({ id: edgeId, source: id, target: `${namespace}/${other.name}`, animated: isHot, className: `talk ${isHot ? "hot" : ""}` });
        }
      });
    });
    return { nodes, edges };
  }, [shown, tools, hot, selected, team]);

  const running = agents.filter((agent) => agent.status.phase === "Running").length;
  const selectedAgent = agents.find((agent) => `${agent.namespace}.${agent.name}` === selected);
  return (
    <div className="map">
      <div className="map-main">
        <div className="map-bar">
          <h1>Agents</h1>
          <p className="summary">
            {snapshot ? `${running} of ${agents.length} running. ` : "Loading… "}
            {pending > 0 ? <a href="#/approvals">{pending} {pending === 1 ? "action waits" : "actions wait"} for approval.</a> : "Nothing waits for approval."}
          </p>
          {teams.length > 1 && (
            <div className="teams" role="group" aria-label="Show team">
              {["all", ...teams].map((name) => (
                <button key={name} aria-pressed={team === name} onClick={() => setTeam(name)}>{name === "all" ? "All teams" : name}</button>
              ))}
            </div>
          )}
        </div>
        <div className="canvas">
          {snapshot && agents.length === 0 && (
            <div className="empty">
              <strong>No agents yet</strong>
              <span className="small muted">The tools on the right are what agents can use. Build an agent to connect one.</span>
              <a href="#/create">Build one from a template</a>
            </div>
          )}
          <ReactFlow
            key={`${team}:${nodes.length}:${width}:${!!selected}`} // re-fit when agents come or go, the team changes, the window resizes, or the drawer opens
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodeClick={(_, node) => node.type === "agent" && go(`/graph/${node.id.replace("/", ".")}`)}
            onPaneClick={() => go("/graph")}
            nodesDraggable={false}
            fitView
            fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={22} size={1.2} />
            <Controls showInteractive={false} position="top-right" />
          </ReactFlow>
          <div className="legend" aria-label="Legend">
            <div><i /> may use this tool</div>
            <div><i className="talk" /> may message this agent</div>
            <div><i className="hot" /> working right now</div>
            <div><b /> a human approves</div>
          </div>
        </div>
      </div>
      {selectedAgent && <Drawer key={selected} agent={selectedAgent} />}
    </div>
  );
}

type Detail = {
  pods: { name: string; phase: string; ready: boolean; restarts: number }[];
  warnings: { object: string; reason: string; message: string }[];
  traces: { id: string; message: string; status: string; tokens: number }[];
};
type ChatLine = { from: "you" | "agent" | "platform"; text: string; traceId?: string; approval?: boolean };

// Browser storage can be missing or blocked (private windows): then each drawer just starts a new chat.
function remembered(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function remember(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* not remembered */ }
  return value;
}

function Drawer({ agent }: { agent: Agent }) {
  const path = `/agents/${agent.namespace}/${agent.name}`;
  const canBuild = useCan("build");
  const canChat = useCan("chat");
  const [tab, setTab] = useState<"chat" | "overview" | "spec">(canChat ? "chat" : "overview");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [lines, setLines] = useState<ChatLine[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  // One conversation per person and agent, remembered in this browser, so closing the drawer keeps the chat.
  const chatKey = `chat:${useContext(MeContext).user}:${path}`;
  const [conversation, setConversation] = useState(() => remembered(chatKey) ?? remember(chatKey, crypto.randomUUID()));

  useEffect(() => {
    if (canChat) api<ChatLine[]>(`${path}/conversations/${conversation}`).then((history) => setLines((current) => current.length ? current : history), () => {});
  }, [path, conversation, canChat]);

  function newChat() {
    setConversation(remember(chatKey, crypto.randomUUID()));
    setLines([]);
  }

  useEffect(() => {
    const load = () => api<Detail>(path).then(setDetail, () => {});
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [path]);

  async function send(event: React.FormEvent) {
    event.preventDefault();
    if (!draft.trim()) return;
    const message = draft.trim();
    setDraft("");
    setLines((current) => [...current, { from: "you", text: message }]);
    setBusy(true);
    try {
      const reply = await api<Reply>(`${path}/chat`, { method: "POST", json: { message, conversation_id: conversation } });
      const line: ChatLine =
        reply.status === "awaiting_approval"
          ? { from: "platform", text: `Paused: ${reply.tool?.replace("__", ".")} needs a human approval.`, traceId: reply.trace_id, approval: true }
          : { from: reply.status === "answered" ? "agent" : "platform", text: reply.answer ?? reply.status, traceId: reply.trace_id };
      setLines((current) => [...current, line]);
    } catch (error) {
      setLines((current) => [...current, { from: "platform", text: (error as Error).message }]);
    } finally {
      setBusy(false);
    }
  }

  const stopped = (agent.spec.replicas ?? 1) === 0;
  const setReplicas = (replicas: number) => api(path, { method: "PUT", json: { spec: { ...agent.spec, replicas } } });
  async function remove() {
    if (!confirm(`Delete ${agent.name}? Its pods, keys and network rules are deleted too.`)) return;
    await api(path, { method: "DELETE" });
    go("/graph");
  }
  const crash = agent.status.crashTest;

  return (
    <aside className="drawer" aria-label={`Agent ${agent.name}`}>
      <header className="drawer-head">
        <div className="row">
          <div>
            <h2>{agent.name}</h2>
            <span className={`status ${phaseClass(agent.status.phase)}`}>{agent.status.phase ?? "Pending"} in {agent.namespace}</span>
          </div>
          <span className="spacer" />
          {canBuild && (stopped
            ? <button className="secondary" onClick={() => setReplicas(1)}>Start</button>
            : <button className="secondary" onClick={() => setReplicas(0)}>Stop</button>)}
          {canBuild && <button className="secondary" onClick={remove} aria-label="Delete agent">Delete</button>}
          <button className="close" onClick={() => go("/graph")} aria-label="Close">×</button>
        </div>
        {agent.status.message && <p className="notice bad">{agent.status.message}</p>}
        {crash && (
          <p className={`notice ${crash.passed ? "good" : "human"}`}>
            {crash.passed ? `Passed its crash test with ${crash.score}.` : `Deployed without passing the crash test (score ${crash.score ?? "none"}).`}
          </p>
        )}
      </header>
      <div className="tabs" role="tablist">
        {canChat && <button role="tab" aria-selected={tab === "chat"} onClick={() => setTab("chat")}>Chat</button>}
        <button role="tab" aria-selected={tab === "overview"} onClick={() => setTab("overview")}>Overview</button>
        <button role="tab" aria-selected={tab === "spec"} onClick={() => setTab("spec")}>Spec</button>
      </div>

      {tab === "chat" && (
        <section className="chat">
          <div className="lines" aria-live="polite">
            {lines.length === 0 && <p className="muted small">Send a message to try {agent.name}. Anything sensitive pauses for an approval.</p>}
            {lines.map((line, index) => (
              <div key={index} className={`line ${line.from}`}>
                {line.text}
                {line.approval && <a href="#/approvals">Open approvals</a>}
                {line.traceId && <a href={`#/traces/${line.traceId}`}>Trace</a>}
              </div>
            ))}
            {busy && <div className="line agent thinking">Working…</div>}
          </div>
          {lines.length > 0 && !busy && <button type="button" className="link small new-chat" onClick={newChat}>New chat</button>}
          <form onSubmit={send}>
            <input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={stopped ? "The agent is stopped" : `Message ${agent.name}`} disabled={stopped || busy} aria-label="Message to the agent" />
            <button disabled={stopped || busy || !draft.trim()}>Send</button>
          </form>
        </section>
      )}

      {tab === "overview" && (
        <div className="tab">
          <dl className="facts">
            <dt>Model</dt><dd className="mono">{agent.spec.model}</dd>
            <dt>Tokens today</dt><dd>{agent.tokens_today.toLocaleString()} of {(agent.spec.budget?.tokensPerDay ?? DEFAULT_TOKENS_PER_DAY).toLocaleString()}</dd>
            <dt>Steps per request</dt><dd>at most {agent.spec.budget?.maxStepsPerRequest ?? 12}</dd>
            <dt>May message</dt><dd>{agent.spec.talksTo?.join(", ") || "no other agents"}</dd>
          </dl>
          <section>
            <h3>Pods</h3>
            {detail?.pods.length ? (
              <ul className="list">
                {detail.pods.map((pod) => (
                  <li key={pod.name}><span className="mono">{pod.name}</span><span className="muted">{pod.ready ? "ready" : pod.phase}{pod.restarts ? `, ${pod.restarts} restarts` : ""}</span></li>
                ))}
              </ul>
            ) : (
              <p className="muted small">{stopped ? "None: the agent is stopped." : "Starting…"}</p>
            )}
            {detail?.warnings.map((warning, index) => <p key={index} className="notice bad">{warning.reason}: {warning.message}</p>)}
          </section>
          <section>
            <h3>Recent requests</h3>
            {detail?.traces.length ? (
              <ul className="list">
                {detail.traces.map((trace) => (
                  <li key={trace.id}><a href={`#/traces/${trace.id}`}>{trace.message.slice(0, 56)}</a><span className="muted">{trace.status.replace("_", " ")}</span></li>
                ))}
              </ul>
            ) : <p className="muted small">No requests yet.</p>}
          </section>
        </div>
      )}

      {tab === "spec" && (
        <div className="tab">
          <pre className="yaml">{YAML.stringify(agent.spec)}</pre>
        </div>
      )}
    </aside>
  );
}
