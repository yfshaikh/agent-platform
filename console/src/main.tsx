import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Agent, api, ApiError } from "./api";
import { Graph } from "./Graph";
import { Create } from "./Create";
import { Approvals, Audit, Traces } from "./pages";
import "./styles.css";

export type TracePulse = { id: string; agent: string; namespace: string; status: string; tools: string[]; at: number };

function useHash() {
  const [hash, setHash] = useState(window.location.hash || "#/graph");
  useEffect(() => {
    const update = () => setHash(window.location.hash || "#/graph");
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return hash.slice(2).split("/"); // "#/traces/abc" -> ["traces", "abc"]
}

// One live stream for the whole console: agent snapshots and each new trace (for pulsing graph edges).
function useEvents(enabled: boolean) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [pending, setPending] = useState(0);
  const [pulse, setPulse] = useState<TracePulse | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const source = new EventSource("/api/events");
    source.addEventListener("snapshot", (event) => {
      const data = JSON.parse((event as MessageEvent).data);
      setAgents(data.agents);
      setPending(data.pending_approvals);
    });
    source.addEventListener("trace", (event) => setPulse({ ...JSON.parse((event as MessageEvent).data), at: Date.now() }));
    return () => source.close();
  }, [enabled]);
  return { agents, pending, pulse };
}

function App() {
  const [route, param] = useHash();
  const [login, setLogin] = useState<"checking" | "in" | "out">("checking");
  const { agents, pending, pulse } = useEvents(login === "in");
  useEffect(() => {
    api("/me").then(
      () => setLogin("in"),
      (error) => setLogin(error instanceof ApiError && error.status === 401 ? "out" : "in"),
    );
  }, []);

  if (login === "checking") return null;
  if (login === "out")
    return (
      <main className="login">
        <h1>Agent Platform</h1>
        <p>You're not logged in. From the <code>agent-platform</code> folder, run:</p>
        <pre>uv run --project api python deploy/login_link.py</pre>
        <p>and open the link it prints. Each link works once, for five minutes.</p>
      </main>
    );

  const links = [
    ["graph", "Agents"],
    ["create", "Create"],
    ["approvals", "Approvals"],
    ["traces", "Traces"],
    ["audit", "Audit log"],
  ];
  return (
    <div className="shell">
      <nav className="rail" aria-label="Main">
        <div className="brand">Agent Platform</div>
        {links.map(([id, label]) => (
          <a key={id} href={`#/${id}`} aria-current={route === id ? "page" : undefined}>
            {label}
            {id === "approvals" && pending > 0 && <span className="badge" aria-label={`${pending} waiting`}>{pending}</span>}
          </a>
        ))}
      </nav>
      <main className="page">
        {route === "graph" && <Graph agents={agents} pulse={pulse} selected={param} />}
        {route === "create" && <Create />}
        {route === "approvals" && <Approvals pending={pending} />}
        {route === "traces" && <Traces traceId={param} />}
        {route === "audit" && <Audit />}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
