import { useContext, useEffect, useState } from "react";
import { api, MeContext, Reply, Step, tellVoice, useCan } from "./api";

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
  const canApprove = useCan("approve");
  const [items, setItems] = useState<Approval[]>([]);
  const [history, setHistory] = useState(false);
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  const [deciding, setDeciding] = useState("");

  useEffect(() => void api<Approval[]>(`/approvals?status=${history ? "all" : "pending"}`).then(setItems), [history, pending, outcomes]);

  async function decide(item: Approval, decision: "approve" | "deny") {
    setDeciding(item.id);
    try {
      const reply = await api<Reply>(`/approvals/${item.id}`, { method: "POST", json: { decision } });
      const verb = decision === "approve" ? "Approved" : "Denied";
      setOutcomes((current) => ({ ...current, [item.id]: `${verb} ${item.tool.replace("__", ".")} for ${item.agent}. ${item.agent} says: ${reply.answer || reply.status}` }));
      tellVoice(`The user ${decision === "approve" ? "approved" : "denied"} ${item.tool.replace("__", ".")} for ${item.agent}.`);
    } catch (error) {
      setOutcomes((current) => ({ ...current, [item.id]: (error as Error).message }));
    } finally {
      setDeciding("");
    }
  }

  const decidedLabel = (item: Approval) =>
    item.status === "expired" ? "Expired: the agent was deleted" : `${item.status === "approve" ? "Approved" : "Denied"} by ${item.decided_by}`;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Approvals</h1>
          <p>Sensitive actions wait here before they run. Approving signs a grant for exactly the call on the slip, valid once, for five minutes.</p>
        </div>
        <label className="toggle">
          <input type="checkbox" checked={history} onChange={(event) => setHistory(event.target.checked)} /> Show decided
        </label>
      </div>
      {!canApprove && <p className="notice" style={{ marginBottom: "1rem" }}>Your role can see the inbox, but only approvers can decide.</p>}
      {Object.entries(outcomes).filter(([id]) => !items.some((item) => item.id === id)).map(([id, text]) => (
        <p key={id} className="notice good" style={{ marginBottom: "1rem" }}>{text}</p>
      ))}
      {items.length === 0 && <div className="empty-state">Nothing is waiting for a decision.</div>}
      <div className="slips">
        {items.map((item) => {
          const { amount, ...rest } = item.arguments as { amount?: number };
          return (
            <article key={item.id} className={`slip ${item.status === "pending" ? "" : "decided"}`}>
              <div>
                <p className="who">{item.namespace}/{item.agent} wants to run</p>
                <code>{item.tool.replace("__", ".")}</code>
              </div>
              {amount !== undefined && <div className="amount">${Number(amount).toLocaleString(undefined, { minimumFractionDigits: 2 })}</div>}
              {Object.keys(rest).length > 0 && (
                <dl>
                  {Object.entries(rest).map(([key, value]) => (
                    <div key={key} style={{ display: "contents" }}>
                      <dt>{key.replace(/_/g, " ")}</dt>
                      <dd>{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd>
                    </div>
                  ))}
                </dl>
              )}
              {outcomes[item.id] && <p className="result">{outcomes[item.id]}</p>}
              <footer>
                <span>{when(item.created_at)}</span>
                {item.status === "pending" ? (
                  canApprove && (
                    <div className="row">
                      <button className="secondary" disabled={deciding === item.id} onClick={() => decide(item, "deny")}>Deny</button>
                      <button className="human" disabled={deciding === item.id} onClick={() => decide(item, "approve")}>{deciding === item.id ? "Running…" : "Approve"}</button>
                    </div>
                  )
                ) : <span className="row">{decidedLabel(item)}</span>}
              </footer>
            </article>
          );
        })}
      </div>
    </>
  );
}

type TraceSummary = { id: string; agent: string; namespace: string; message: string; status: string; tokens: number; started_at: string; seconds: number; step_count: number; caller: string | null; from_alert: boolean };
const fromWhom = (row: TraceSummary) => (row.caller ? `agent ${row.caller}` : row.from_alert ? "alert" : "a person");
type Trace = TraceSummary & { answer: string; steps: Step[]; cost_usd: number };
const statusPill = (status: string) => ({ answered: "ok", awaiting_approval: "human", stopped: "bad", budget_exceeded: "bad" })[status] ?? "";

// Every request an agent handled; one trace shows each model call and tool call in order.
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
        <div>
          <h1>{trace.message}</h1>
          <p className="muted" style={{ marginTop: "0.4rem" }}>
            {trace.namespace}/{trace.agent}, from {fromWhom(trace)}, {trace.status.replace("_", " ")}, {trace.tokens.toLocaleString()} tokens (about ${trace.cost_usd.toFixed(4)}), {trace.seconds.toFixed(1)}s
          </p>
        </div>
        <ol className="timeline">
          {trace.steps.map((step, index) =>
            step.type === "model" ? (
              <li key={index} className="model">Model call, {step.tokens} tokens, {step.seconds}s</li>
            ) : (
              <li key={index} className={`tool ${step.rule}`}>
                <div className="row">
                  <code>{step.name?.replace("__", ".")}</code>
                  <span className={`pill ${step.rule === "auto" ? "" : step.rule === "human" ? "human" : "bad"}`}>
                    {step.rule === "auto" ? "ran" : step.rule === "human" ? `human ${step.decision ? `${step.decision}d` : "approval"}` : step.rule}
                  </span>
                </div>
                <pre>{JSON.stringify(step.arguments)}</pre>
                <pre>{step.result}</pre>
              </li>
            ),
          )}
        </ol>
        {trace.answer && <p className="answer">{trace.answer}</p>}
      </div>
    );

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Traces</h1>
          <p>Every request an agent handled, with each model and tool call it made.</p>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>When</th><th>Agent</th><th>From</th><th>Message</th><th>Status</th><th className="num">Tokens</th></tr>
          </thead>
          <tbody>
            {list.map((row) => (
              <tr key={row.id}>
                <td className="muted">{when(row.started_at)}</td>
                <td>{row.namespace}/{row.agent}</td>
                <td className={row.caller || row.from_alert ? "" : "muted"}>{fromWhom(row)}</td>
                <td><a href={`#/traces/${row.id}`}>{row.message.slice(0, 80)}</a></td>
                <td><span className={`pill ${statusPill(row.status)}`}>{row.status.replace("_", " ")}</span></td>
                <td className="num">{row.tokens.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

type AuditRow = { id: number; at: string; actor: string; action: string; target: string; detail: Record<string, unknown> };

// Who changed what: every deploy, stop, delete, decision and login.
export function Audit() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [filter, setFilter] = useState("");
  useEffect(() => void api<AuditRow[]>("/audit").then(setRows), []);
  const shown = rows.filter((row) => `${row.actor} ${row.action} ${row.target}`.toLowerCase().includes(filter.toLowerCase()));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Audit log</h1>
          <p>Append-only. Agents can add to it but never change it.</p>
        </div>
        <input className="search" type="search" placeholder="Filter by person, action or target" value={filter} onChange={(event) => setFilter(event.target.value)} aria-label="Filter the audit log" />
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Detail</th></tr>
          </thead>
          <tbody>
            {shown.map((row) => (
              <tr key={row.id}>
                <td className="muted">{when(row.at)}</td>
                <td>{row.actor}</td>
                <td>{row.action.replace(/_/g, " ")}</td>
                <td>{row.target}</td>
                <td><span className="mono">{JSON.stringify(row.detail).slice(0, 120)}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

type User = { name: string; role: string; created_at: string };
const ROLE_TEXT: Record<string, string> = {
  viewer: "sees agents, traces and the audit log",
  builder: "also drafts, deploys, stops and chats with agents",
  approver: "also approves sensitive actions",
  admin: "also manages people",
};

// Admins add people and pick their role. Saving an existing name resets that person's role and password.
export function Users() {
  const me = useContext(MeContext);
  const [users, setUsers] = useState<User[]>([]);
  const [form, setForm] = useState({ name: "", role: "builder", password: "" });
  const [message, setMessage] = useState("");
  const load = () => api<User[]>("/users").then(setUsers);
  useEffect(() => void load(), []);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    try {
      await api("/users", { method: "POST", json: form });
      setMessage(`Saved ${form.name} as ${form.role}.`);
      setForm({ name: "", role: form.role, password: "" });
      load();
    } catch (error) {
      setMessage((error as Error).message);
    }
  }
  async function remove(name: string) {
    if (!confirm(`Remove ${name}? They're signed out at once.`)) return;
    await api(`/users/${name}`, { method: "DELETE" });
    load();
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>People</h1>
          <p>Each person signs in with their own name, so the audit log shows who did what. Roles decide what they can do.</p>
        </div>
      </div>
      <div className="users">
        <div className="table-wrap">
          <table>
            <thead><tr><th>Name</th><th>Role</th><th>Added</th><th /></tr></thead>
            <tbody>
              {users.length === 0 && <tr><td colSpan={4} className="muted">No one yet. Add the first person on the right.</td></tr>}
              {users.map((user) => (
                <tr key={user.name}>
                  <td>{user.name}</td>
                  <td><span className="pill">{user.role}</span> <span className="small muted">{ROLE_TEXT[user.role]}</span></td>
                  <td className="muted">{when(user.created_at)}</td>
                  <td className="num">{user.name !== me.user && <button className="link small" onClick={() => remove(user.name)}>Remove</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <form onSubmit={save}>
          <h2>Add someone</h2>
          <label>Name<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" title="Lowercase letters, digits and dashes" required /></label>
          <label>
            Role
            <select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value })}>
              {Object.keys(ROLE_TEXT).map((role) => <option key={role} value={role}>{role}</option>)}
            </select>
          </label>
          <p className="small muted">A {form.role} {ROLE_TEXT[form.role]}.</p>
          <label>Password<input type="password" minLength={10} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} autoComplete="new-password" required /></label>
          {message && <p className="small" role="status">{message}</p>}
          <button>Save</button>
        </form>
      </div>
    </>
  );
}
