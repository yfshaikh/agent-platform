import { useEffect, useMemo, useState } from "react";
import { Background, Controls, Edge, Handle, Node, NodeProps, Position, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import YAML from "yaml";
import { Agent, api, go, Reply, Tool } from "./api";
import type { TracePulse } from "./main";

const PULSE_MS = 4000;

const phaseClass = (phase?: string) =>
  ({ Running: "ok", Pending: "wait", Stopped: "off", Failed: "bad" })[phase ?? ""] ?? "wait";

function AgentNode({ data }: NodeProps<Node<{ agent: Agent }>>) {
  const { agent } = data;
  const wanted = agent.spec.replicas ?? 1;
  return (
    <div className={`node agent-node ${phaseClass(agent.status.phase)}`}>
      <Handle type="target" position={Position.Left} />
      <div className="node-title">
        <span className="dot" aria-hidden /> {agent.name}
      </div>
      <div className="node-meta">{agent.namespace}</div>
      <div className="node-stats">
        <span>{agent.status.phase ?? "Pending"}</span>
        <span>{agent.status.readyReplicas ?? 0}/{wanted} pods</span>
        <span>{agent.tokens_today.toLocaleString()} tokens today</span>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function ToolNode({ data }: NodeProps<Node<{ tool: Tool }>>) {
  return (
    <div className="node tool-node">
      <Handle type="target" position={Position.Left} />
      <div className="node-title">{data.tool.name}</div>
      <ul className="functions">
        {Object.entries(data.tool.classes).map(([name, kind]) => (
          <li key={name} className={kind}>
            {name}
            {kind === "sensitive" && <span title="Needs a human approval"> (needs approval)</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

const nodeTypes = { agent: AgentNode, tool: ToolNode };
const globToRegex = (glob: string) => new RegExp(`^${glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`);

export function Graph({ agents, pulse, selected }: { agents: Agent[]; pulse: TracePulse | null; selected?: string }) {
  const [tools, setTools] = useState<Tool[]>([]);
  const [hot, setHot] = useState<Record<string, number>>({});
  useEffect(() => void api<Tool[]>("/tools").then(setTools), []);

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
    const nodes: Node[] = tools.map((tool, index) => ({ id: `tool:${tool.name}`, type: "tool", position: { x: 0, y: index * 190 }, data: { tool } }));
    const namespaces = [...new Set(agents.map((agent) => agent.namespace))].sort();
    const edges: Edge[] = [];
    namespaces.forEach((namespace, column) => {
      const members = agents.filter((agent) => agent.namespace === namespace);
      members.forEach((agent, row) => {
        const id = `${agent.namespace}/${agent.name}`;
        nodes.push({ id, type: "agent", position: { x: -380 - column * 300, y: row * 150 }, data: { agent }, selected: selected === `${agent.namespace}.${agent.name}` });
        for (const tool of agent.spec.tools ?? []) {
          const edgeId = `use:${id}->${tool.name}`;
          edges.push({ id: edgeId, source: id, target: `tool:${tool.name}`, animated: (hot[edgeId] ?? 0) > now, className: (hot[edgeId] ?? 0) > now ? "hot" : "" });
        }
        const patterns = (agent.spec.talksTo ?? []).map(globToRegex);
        for (const other of members) {
          if (other.name === agent.name || !patterns.some((pattern) => pattern.test(other.name))) continue;
          const edgeId = `talk:${id}->${other.name}`;
          edges.push({ id: edgeId, source: id, target: `${namespace}/${other.name}`, animated: (hot[edgeId] ?? 0) > now, className: `talk ${(hot[edgeId] ?? 0) > now ? "hot" : ""}` });
        }
      });
    });
    return { nodes, edges };
  }, [agents, tools, hot, selected]);

  const selectedAgent = agents.find((agent) => `${agent.namespace}.${agent.name}` === selected);
  return (
    <div className="graph-page">
      <div className="canvas">
        {agents.length === 0 && (
          <div className="empty">
            No agents yet. <a href="#/create">Create one from a template</a>.
          </div>
        )}
        <ReactFlow
          key={nodes.length} // re-fit the view when agents arrive or leave
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodeClick={(_, node) => node.type === "agent" && go(`/graph/${node.id.replace("/", ".")}`)}
          onPaneClick={() => go("/graph")}
          nodesDraggable={false}
          fitView
          fitViewOptions={{ padding: 0.3, maxZoom: 1 }}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={24} size={1} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      {selectedAgent && <AgentPanel key={selected} agent={selectedAgent} />}
    </div>
  );
}

type Detail = {
  pods: { name: string; phase: string; ready: boolean; restarts: number }[];
  warnings: { object: string; reason: string; message: string }[];
  traces: { id: string; message: string; status: string; tokens: number }[];
};
type ChatLine = { from: "you" | "agent" | "platform"; text: string; traceId?: string };

function AgentPanel({ agent }: { agent: Agent }) {
  const path = `/agents/${agent.namespace}/${agent.name}`;
  const [detail, setDetail] = useState<Detail | null>(null);
  const [lines, setLines] = useState<ChatLine[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [conversation] = useState(() => crypto.randomUUID());

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
          ? { from: "platform", text: `Waiting for a human to approve ${reply.tool} ${JSON.stringify(reply.arguments)}.`, traceId: reply.trace_id }
          : { from: reply.status === "answered" ? "agent" : "platform", text: reply.answer ?? reply.status, traceId: reply.trace_id };
      setLines((current) => [...current, line]);
    } catch (error) {
      setLines((current) => [...current, { from: "platform", text: (error as Error).message }]);
    } finally {
      setBusy(false);
    }
  }

  const stopped = (agent.spec.replicas ?? 1) === 0;
  async function setReplicas(replicas: number) {
    await api(path, { method: "PUT", json: { spec: { ...agent.spec, replicas } } });
  }
  async function remove() {
    if (!confirm(`Delete ${agent.name}? Its pods, keys and network rules are deleted too.`)) return;
    await api(path, { method: "DELETE" });
    go("/graph");
  }

  return (
    <aside className="panel" aria-label={`Agent ${agent.name}`}>
      <header className="panel-head">
        <div>
          <h2>{agent.name}</h2>
          <div className={`phase ${phaseClass(agent.status.phase)}`}>
            <span className="dot" aria-hidden /> {agent.status.phase ?? "Pending"} in {agent.namespace}
          </div>
          {agent.status.message && <p className="error">{agent.status.message}</p>}
        </div>
        <button className="close" onClick={() => go("/graph")} aria-label="Close panel">×</button>
      </header>

      <section className="chat">
        <div className="lines" aria-live="polite">
          {lines.length === 0 && <p className="hint">Send a message to try the agent.</p>}
          {lines.map((line, index) => (
            <div key={index} className={`line ${line.from}`}>
              {line.text}
              {line.traceId && <a href={`#/traces/${line.traceId}`}> trace</a>}
              {line.from === "platform" && line.text.startsWith("Waiting") && <a href="#/approvals"> open approvals</a>}
            </div>
          ))}
          {busy && <div className="line agent thinking">Working…</div>}
        </div>
        <form onSubmit={send}>
          <input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={stopped ? "Agent is stopped" : "Message"} disabled={stopped || busy} aria-label="Message to the agent" />
          <button disabled={stopped || busy}>Send</button>
        </form>
      </section>

      <section>
        <h3>Controls</h3>
        <div className="row">
          {stopped ? (
            <button onClick={() => setReplicas(1)}>Start agent</button>
          ) : (
            <button className="danger" onClick={() => setReplicas(0)}>Stop agent</button>
          )}
          <button className="quiet" onClick={remove}>Delete</button>
        </div>
      </section>

      <section>
        <h3>Pods</h3>
        {detail?.pods.length ? (
          <ul className="plain">
            {detail.pods.map((pod) => (
              <li key={pod.name}>
                {pod.name}: {pod.ready ? "ready" : pod.phase}, {pod.restarts} restarts
              </li>
            ))}
          </ul>
        ) : (
          <p className="hint">{stopped ? "No pods: the agent is stopped." : "Starting…"}</p>
        )}
        {detail?.warnings.map((warning, index) => (
          <p key={index} className="error">{warning.reason}: {warning.message}</p>
        ))}
      </section>

      <section>
        <h3>Recent requests</h3>
        <ul className="plain">
          {detail?.traces.map((trace) => (
            <li key={trace.id}>
              <a href={`#/traces/${trace.id}`}>{trace.message.slice(0, 60)}</a> ({trace.status.replace("_", " ")})
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h3>Spec</h3>
        <pre className="yaml">{YAML.stringify(agent.spec)}</pre>
      </section>
    </aside>
  );
}
