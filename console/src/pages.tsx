import { useEffect, useState } from "react";
import { api, Reply, Step } from "./api";

type Approval = {
  id: string;
  agent: string;
  namespace: string;
  tool: string;
  arguments: Record<string, unknown>;
  status: string;
  decided_by: string | null;
  created_at: string;
};

const when = (iso: string) => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" });

// The approval inbox. Each slip shows exactly the call that will run; approving signs a grant for that call only.
export function Approvals({ pending }: { pending: number }) {
  const [items, setItems] = useState<Approval[]>([]);
  const [history, setHistory] = useState(false);
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  const [deciding, setDeciding] = useState("");

  useEffect(() => void api<Approval[]>(`/approvals?status=${history ? "all" : "pending"}`).then(setItems), [history, pending, outcomes]);

  async function decide(id: string, decision: "approve" | "deny") {
    setDeciding(id);
    try {
      const reply = await api<Reply>(`/approvals/${id}`, { method: "POST", json: { decision } });
      setOutcomes((current) => ({ ...current, [id]: reply.answer || reply.status }));
    } catch (error) {
      setOutcomes((current) => ({ ...current, [id]: (error as Error).message }));
    } finally {
      setDeciding("");
    }
  }

  return (
    <div className="approvals">
      <div className="title-row">
        <h1>Approvals</h1>
        <label className="toggle">
          <input type="checkbox" checked={history} onChange={(event) => setHistory(event.target.checked)} /> Show decided
        </label>
      </div>
      {Object.entries(outcomes).map(([id, text]) => (
        <p key={id} className="outcome">
          <strong>{id}:</strong> {text}
        </p>
      ))}
      {items.length === 0 && <p className="hint">Nothing is waiting. Sensitive actions, like moving money, show up here before they run.</p>}
      <div className="slips">
        {items.map((item) => {
          const { amount, ...rest } = item.arguments as { amount?: number };
          return (
            <article key={item.id} className={`slip ${item.status}`}>
              <header>
                <span>{item.namespace}/{item.agent} wants to run</span>
                <code>{item.tool.replace("__", ".")}</code>
              </header>
              {amount !== undefined && <div className="amount">${Number(amount).toFixed(2)}</div>}
              <dl>
                {Object.entries(rest).map(([key, value]) => (
                  <div key={key}>
                    <dt>{key.replace(/_/g, " ")}</dt>
                    <dd>{String(value)}</dd>
                  </div>
                ))}
              </dl>
              <footer>
                <span className="hint">Requested {when(item.created_at)}</span>
                {item.status === "pending" ? (
                  <div className="row">
                    <button className="quiet" disabled={deciding === item.id} onClick={() => decide(item.id, "deny")}>Deny</button>
                    <button disabled={deciding === item.id} onClick={() => decide(item.id, "approve")}>{deciding === item.id ? "Running…" : "Approve"}</button>
                  </div>
                ) : (
                  <span className="decided">{item.status === "expired" ? "Expired: the agent was deleted" : `${item.status === "approve" ? "Approved" : "Denied"} by ${item.decided_by}`}</span>
                )}
              </footer>
            </article>
          );
        })}
      </div>
    </div>
  );
}

type TraceSummary = { id: string; agent: string; namespace: string; message: string; status: string; tokens: number; started_at: string; seconds: number; step_count: number };
type Trace = TraceSummary & { answer: string; steps: Step[]; cost_usd: number };

export function Traces({ traceId }: { traceId?: string }) {
  const [list, setList] = useState<TraceSummary[]>([]);
  const [trace, setTrace] = useState<Trace | null>(null);
  useEffect(() => {
    if (traceId) api<Trace>(`/traces/${traceId}`).then(setTrace);
    else api<TraceSummary[]>("/traces").then(setList);
  }, [traceId]);

  if (traceId && trace)
    return (
      <div className="trace">
        <a href="#/traces">All traces</a>
        <h1>{trace.message}</h1>
        <p className="lede">
          {trace.namespace}/{trace.agent}, {trace.status}, {trace.tokens.toLocaleString()} tokens (about ${trace.cost_usd.toFixed(4)}), {trace.seconds.toFixed(1)}s
        </p>
        <ol className="timeline">
          {trace.steps.map((step, index) =>
            step.type === "model" ? (
              <li key={index} className="model">
                Model call, {step.tokens} tokens, {step.seconds}s
              </li>
            ) : (
              <li key={index} className={`tool ${step.rule}`}>
                <div>
                  <code>{step.name}</code> <span className="rule">{step.rule}{step.decision ? `, ${step.decision}d` : ""}</span>
                </div>
                <pre>{JSON.stringify(step.arguments)}</pre>
                <pre className="result">{step.result}</pre>
              </li>
            ),
          )}
        </ol>
        {trace.answer && <blockquote>{trace.answer}</blockquote>}
      </div>
    );

  return (
    <div>
      <h1>Traces</h1>
      <table>
        <thead>
          <tr><th>When</th><th>Agent</th><th>Message</th><th>Status</th><th className="num">Tokens</th></tr>
        </thead>
        <tbody>
          {list.map((row) => (
            <tr key={row.id}>
              <td>{when(row.started_at)}</td>
              <td>{row.namespace}/{row.agent}</td>
              <td><a href={`#/traces/${row.id}`}>{row.message.slice(0, 80)}</a></td>
              <td>{row.status}</td>
              <td className="num">{row.tokens.toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type AuditRow = { id: number; at: string; actor: string; action: string; target: string; detail: Record<string, unknown> };

export function Audit() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [filter, setFilter] = useState("");
  useEffect(() => void api<AuditRow[]>("/audit").then(setRows), []);
  const shown = rows.filter((row) => `${row.actor} ${row.action} ${row.target}`.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div>
      <div className="title-row">
        <h1>Audit log</h1>
        <input type="search" placeholder="Filter by actor, action or target" value={filter} onChange={(event) => setFilter(event.target.value)} aria-label="Filter the audit log" />
      </div>
      <table>
        <thead>
          <tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Detail</th></tr>
        </thead>
        <tbody>
          {shown.map((row) => (
            <tr key={row.id} className={row.action.replace(/_/g, "-")}>
              <td>{when(row.at)}</td>
              <td>{row.actor}</td>
              <td>{row.action.replace(/_/g, " ")}</td>
              <td>{row.target}</td>
              <td><code className="detail">{JSON.stringify(row.detail).slice(0, 140)}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
