import { useEffect, useState } from "react";
import { api, useCan } from "./api";

type Gpu = { index: number; name: string; "temperature.gpu": number; "utilization.gpu": number; "memory.used": number; "memory.total": number; "power.draw": number; "power.limit": number };
type GpuNode = { name: string; gpu?: string; provider?: string; connected: boolean; reachable?: boolean; error?: string; cordoned?: boolean; gpus?: Gpu[] };
type Job = { name: string; template: string; status: string; exit_code: number; oom_killed: boolean; started_at: string; finished_at: string };

const INCIDENTS = [
  { kind: "out-of-memory", title: "Out of memory", text: "A memory hog takes the GPU's memory; a training job then crashes with CUDA out of memory." },
  { kind: "overheating", title: "Overheating", text: "A full-load job pushes power and temperature up for 10 minutes." },
  { kind: "crashing-job", title: "Crashing job", text: "A training job crashes on its own after a minute." },
];
const clock = (iso: string) => (iso.startsWith("0001") ? "" : new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));

// Each GPU node in the fleet: live readings, its jobs and their logs, and buttons that start real incidents.
export function Gpus() {
  const canInject = useCan("admin");
  const canStop = useCan("approve");
  const [nodes, setNodes] = useState<GpuNode[] | null>(null);
  const [selected, setSelected] = useState("nmc-gpu");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [logsOf, setLogsOf] = useState<string | null>(null);
  const [logs, setLogs] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const load = () => {
      api<GpuNode[]>("/gpu-nodes").then(setNodes, (error) => setMessage((error as Error).message));
      api<Job[]>(`/gpu-nodes/${selected}/jobs`).then(setJobs, () => setJobs([]));
      if (logsOf) api<{ logs: string }>(`/gpu-nodes/${selected}/jobs/${logsOf}/logs`).then((body) => setLogs(body.logs), () => {});
    };
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [selected, logsOf]);

  async function inject(kind: string) {
    try {
      const result = await api<{ what: string }>(`/gpu-nodes/${selected}/incidents`, { method: "POST", json: { kind } });
      setMessage(`Started on ${selected}: ${result.what}. Watch the jobs below, then ask the triage agent what's wrong.`);
    } catch (error) {
      setMessage((error as Error).message);
    }
  }
  async function stop(job: string) {
    if (!confirm(`Stop ${job}? It can't be resumed.`)) return;
    await api(`/gpu-nodes/${selected}/jobs/${job}/stop`, { method: "POST" }).catch((error) => setMessage((error as Error).message));
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>GPUs</h1>
          <p>The machines NMC²'s customers run their jobs on, read live. Agents see the same nodes through the gpu-fleet tool, but only the nodes they were given.</p>
        </div>
      </div>
      {message && <p className="notice" style={{ marginBottom: "1rem" }}>{message}</p>}
      <div className="nodes">
        {nodes?.map((node) => {
          const gpu = node.gpus?.[0];
          return (
            <button key={node.name} className={`node-card ${node.name === selected ? "on" : ""}`} disabled={!node.connected}
              onClick={() => { setSelected(node.name); setLogsOf(null); }} aria-pressed={node.name === selected}>
              <div className="row">
                <strong>{node.name}</strong>
                <span className={`status ${!node.connected ? "" : node.reachable ? (node.cordoned ? "wait" : "ok") : "bad"}`}>
                  {!node.connected ? "not connected" : !node.reachable ? "unreachable" : node.cordoned ? "cordoned" : "online"}
                </span>
              </div>
              <span className="small muted">{node.gpu} on {node.provider}</span>
              {gpu ? (
                <div className="readings">
                  <Reading label="Temperature" value={`${gpu["temperature.gpu"]} °C`} level={gpu["temperature.gpu"] / 90} />
                  <Reading label="Utilization" value={`${gpu["utilization.gpu"]}%`} level={gpu["utilization.gpu"] / 100} />
                  <Reading label="Memory" value={`${(gpu["memory.used"] / 1024).toFixed(1)} of ${(gpu["memory.total"] / 1024).toFixed(0)} GB`} level={gpu["memory.used"] / gpu["memory.total"]} />
                  <Reading label="Power" value={`${Math.round(gpu["power.draw"])} of ${Math.round(gpu["power.limit"])} W`} level={gpu["power.draw"] / gpu["power.limit"]} />
                </div>
              ) : (
                <p className="small muted">{node.connected ? node.error ?? "Reading…" : "Add a second machine: run deploy/gpu_node_setup.sh on it and list it in NODES."}</p>
              )}
            </button>
          );
        })}
      </div>

      {canInject && (
        <section className="incidents" aria-label="Start an incident">
          <h2>Start a real incident on {selected}</h2>
          <p className="small muted">For demos and drills. These run real jobs on the GPU; nothing is overclocked. Audited.</p>
          <div className="incident-row">
            {INCIDENTS.map((incident) => (
              <button key={incident.kind} className="template" onClick={() => inject(incident.kind)}>
                <strong>{incident.title}</strong>
                <span>{incident.text}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="jobs-area">
        <div className="table-wrap">
          <table>
            <thead><tr><th>Job on {selected}</th><th>Status</th><th>Started</th><th>Finished</th><th /></tr></thead>
            <tbody>
              {jobs.length === 0 && <tr><td colSpan={5} className="muted">No jobs yet.</td></tr>}
              {jobs.map((job) => (
                <tr key={job.name} className={logsOf === job.name ? "picked" : ""}>
                  <td><button className="link" onClick={() => setLogsOf(job.name)}>{job.name}</button></td>
                  <td>
                    <span className={`pill ${job.status === "running" ? "ok" : job.exit_code ? "bad" : ""}`}>
                      {job.status === "running" ? "running" : job.exit_code ? `failed (exit ${job.exit_code})` : "finished"}
                    </span>
                  </td>
                  <td className="muted">{clock(job.started_at)}</td>
                  <td className="muted">{clock(job.finished_at)}</td>
                  <td className="num">{canStop && job.status === "running" && <button className="link small" onClick={() => stop(job.name)}>Stop</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="terminal job-logs" aria-live="polite">
          {logsOf ? <><div className="line cmd">$ docker logs --tail 40 {logsOf}</div><div className="line">{logs || "…"}</div></> : <div className="line wait">Pick a job to see its logs.</div>}
        </div>
      </section>
    </>
  );
}

function Reading({ label, value, level }: { label: string; value: string; level: number }) {
  return (
    <div className="reading">
      <span className="small muted">{label}</span>
      <strong>{value}</strong>
      <div className="bar"><span style={{ width: `${Math.min(100, level * 100)}%` }} className={level > 0.9 ? "high" : ""} /></div>
    </div>
  );
}
