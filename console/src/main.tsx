import { lazy, StrictMode, Suspense, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Agent, api, ApiError, Me, MeContext } from "./api";
import { Graph } from "./Graph";
import { Create } from "./Create";
import { Login, Mark } from "./Login";
import { Approvals, Audit, Traces, Users } from "./pages";
import { Gpus } from "./Gpus";
import { Incident } from "./Incident";
import { Settings } from "./Settings";
import { Runbooks } from "./Runbooks";
import "./styles.css";

// The voice orb pulls in Pipecat, so it loads after the rest of the console.
const Voice = lazy(() => import("./Voice"));

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
  const [agents, setAgents] = useState<Agent[] | null>(null); // null until the first snapshot
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
  const [route, param, extra] = useHash();
  const [me, setMe] = useState<Me | null | "checking">("checking");
  const { agents, pending, pulse } = useEvents(me !== null && me !== "checking");

  const loadMe = () =>
    api<Me>("/me").then(setMe, (error) => setMe(error instanceof ApiError && error.status === 401 ? null : { user: "?", role: null, permissions: [] }));
  useEffect(() => void loadMe(), []);

  if (me === "checking") return null;
  if (me === null) return <Login onLogin={loadMe} />;

  const can = (permission: string) => me.permissions.includes(permission);
  const links: [string, string, boolean][] = [
    ["graph", "Agents", true],
    ["create", "Build an agent", can("draft") || can("build")],
    ["gpus", "GPUs", true],
    ["approvals", "Approvals", true],
    ["runbooks", "Runbooks", true],
    ["traces", "Traces", true],
    ["audit", "Audit log", true],
    ["users", "People", can("admin")],
    ["settings", "Settings", can("admin")],
  ];
  async function logout() {
    await api("/logout", { method: "POST" }).catch(() => undefined);
    setMe(null);
  }

  return (
    <MeContext.Provider value={me}>
      <div className="shell">
        <nav className="rail" aria-label="Main">
          <div className="mark">
            <Mark />
            <span>NMC² Agents<small>GPU cloud operations</small></span>
          </div>
          {links.filter(([, , shown]) => shown).map(([id, label]) => (
            <a key={id} href={`#/${id}`} aria-current={route === id ? "page" : undefined}>
              {label}
              {id === "approvals" && pending > 0 && <span className="count" aria-label={`${pending} waiting`}>{pending}</span>}
            </a>
          ))}
          <div className="whoami">
            <strong>{me.user}</strong>
            <div className="row">
              <span className="muted">{me.role ?? "custom role"}</span>
              <button className="link small" onClick={logout}>Sign out</button>
            </div>
          </div>
        </nav>
        <main className="page">
          {route !== "approvals" && route !== "incidents" && <Alarm pending={pending} />}
          {route === "graph" && <Graph agents={agents} pulse={pulse} selected={param} pending={pending} />}
          {route === "create" && <Create key={param} />}
          {route === "gpus" && <Gpus />}
          {route === "incidents" && param && <Incident id={decodeURIComponent(param)} />}
          {route === "settings" && can("admin") && <Settings agents={agents ?? []} />}
          {route === "approvals" && <Approvals pending={pending} />}
          {route === "runbooks" && <Runbooks name={param} section={extra && decodeURIComponent(extra)} />}
          {route === "traces" && <Traces traceId={param} />}
          {route === "audit" && <Audit />}
          {route === "users" && <Users />}
        </main>
      </div>
      <Suspense fallback={null}>
        <Voice />
      </Suspense>
    </MeContext.Provider>
  );
}

type Waiting = { id: string; alert_id: string | null; tool: string; arguments: Record<string, unknown> };
type AlertRow = { id: string; node: string; title: string };

// While a fix for a GPU alert waits for a human, the whole console runs hot: this banner on every page.
function Alarm({ pending }: { pending: number }) {
  const [alarms, setAlarms] = useState<{ approval: Waiting; alert: AlertRow }[]>([]);
  useEffect(() => {
    if (pending === 0) return setAlarms([]);
    Promise.all([api<Waiting[]>("/approvals"), api<AlertRow[]>("/gpu-nodes/alerts")]).then(([approvals, alerts]) =>
      setAlarms(approvals.flatMap((approval) => {
        const alert = alerts.find((row) => row.id === approval.alert_id);
        return alert ? [{ approval, alert }] : [];
      })), () => setAlarms([]));
  }, [pending]);
  if (alarms.length === 0) return null;
  const { approval, alert } = alarms[0];
  return (
    <section className="alarm" role="alert">
      <span className="alarm-dot" aria-hidden />
      <div>
        <strong>{alert.node} needs you</strong>
        <p>{alert.title}. The agents worked it; {approval.tool.replace("__", ".")} {String(approval.arguments.name ?? "")} is waiting for your approval{alarms.length > 1 ? `, plus ${alarms.length - 1} more` : ""}.</p>
      </div>
      <a className="alarm-go" href="#/approvals">Review the fix</a>
      <a href={`#/incidents/${encodeURIComponent(alert.id)}`}>What happened</a>
    </section>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
