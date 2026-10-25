import { Fragment, ReactNode, useEffect, useState } from "react";
import { api } from "./api";

type Step = { agent: string; trace_id: string; started_at: string; seconds: number | null; status: string; answer: string | null; tools: { name: string; rule: string | null }[] };
type Approval = { id: string; status: string; decided_by: string | null; tool: string; arguments: Record<string, unknown> };
type IncidentData = {
  id: string; at: string; node: string; title: string; agent: string | null; namespace: string | null; status: string;
  paged: string | null; steps: Step[]; approvals: Approval[]; prevention_diff: string | null;
};

const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const said = { auto: "ran", human: "paused for a human", blocked: "refused: not its tool", deny: "refused by policy", out_of_scope: "refused: not its node" } as Record<string, string>;

// One alert as an incident: what each agent did, the fix waiting for a human, the prevention diff, and the phone call.
export function Incident({ id }: { id: string }) {
  const [incident, setIncident] = useState<IncidentData | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const load = () => api<IncidentData>(`/gpu-nodes/alerts/${encodeURIComponent(id)}`).then(setIncident, (failure) => setError((failure as Error).message));
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [id]);

  if (error) return <p className="error">{error}</p>;
  if (!incident) return <p className="muted">Loading…</p>;
  const fix = incident.approvals[0];
  return (
    <>
      <div className="page-head">
        <div>
          <a href="#/gpus" className="small">GPUs</a>
          <h1>{incident.node}: {incident.title}</h1>
          <p>Alert raised at {clock(incident.at)}. {incident.agent ? `Sent to ${incident.namespace}/${incident.agent}.` : "No agent has it yet."}</p>
        </div>
      </div>

      <div className="incident">
        <section aria-label="What the agents did">
          <h2>What the agents did</h2>
          {incident.steps.length === 0 && <p className="muted small">{incident.status === "no-agent" ? "No running agent is subscribed to this node." : "Triage is investigating…"}</p>}
          <ol className="incident-steps">
            {incident.steps.map((step) => (
              <li key={step.trace_id}>
                <div className="row">
                  <strong>{step.agent}</strong>
                  <span className="muted small">{clock(step.started_at)}{step.seconds ? `, ${Math.round(step.seconds)} s` : ""}</span>
                  <span className="spacer" />
                  <a className="small" href={`#/traces/${step.trace_id}`}>Trace</a>
                </div>
                <p className="small muted">{step.tools.map((tool) => `${tool.name.replace("__", ".")} (${said[tool.rule ?? ""] ?? tool.rule})`).join(" → ") || "no tools"}</p>
                {step.answer && <div className="answer md"><Markdown text={step.answer.replace(/```diff[\s\S]*?```/g, "(diff below)")} /></div>}
              </li>
            ))}
          </ol>
        </section>

        <aside>
          <section className={`slip-card ${fix?.status === "pending" ? "waiting" : ""}`} aria-label="Fix now">
            <h2>Fix now</h2>
            {fix ? (
              <>
                <p><strong>{fix.tool.replace("__", ".")}</strong> {Object.entries(fix.arguments).filter(([key]) => key !== "reason").map(([key, value]) => `${key}=${value}`).join(", ")}</p>
                {typeof fix.arguments.reason === "string" && <p className="small">{fix.arguments.reason}</p>}
                <p className="small">
                  {fix.status === "pending" ? <>Waiting for a human. <a href="#/approvals">Review it</a></> : `${fix.status} by ${fix.decided_by ?? "the platform"}`}
                </p>
              </>
            ) : <p className="muted small">No fix proposed yet.</p>}
          </section>

          <section className="slip-card" aria-label="On-call">
            <h2>On-call</h2>
            <p className="small">{incident.paged ?? "Not paged yet."}</p>
          </section>
        </aside>
      </div>

      <section aria-label="Prevention">
        <h2>Prevent it happening again</h2>
        <p className="small muted">Written by the fixer against NMC²'s own code. Nothing has been applied: a person reviews it.</p>
        {incident.prevention_diff ? (
<Diff text={incident.prevention_diff} />
        ) : <p className="muted small">No prevention change yet.</p>}
      </section>
    </>
  );
}

export function Diff({ text }: { text: string }) {
  return (
    <pre className="diff">
      {text.split("\n").map((line, index) => (
        <span key={index} className={line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : ""}>{line}{"\n"}</span>
      ))}
    </pre>
  );
}

// Bold, `code` and "- " lists from an agent's answer, built as React elements (never raw HTML), so agent text can't inject markup.
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? <strong key={index}>{part.slice(2, -2)}</strong>
      : part.startsWith("`") && part.endsWith("`") && part.length > 2 ? <code key={index}>{part.slice(1, -1)}</code>
      : <Fragment key={index}>{part}</Fragment>);
}

export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) blocks.push(<ul key={blocks.length}>{list.map((item, index) => <li key={index}>{inline(item)}</li>)}</ul>);
    list = [];
  };
  for (const line of text.split("\n")) {
    const item = line.match(/^\s*[-*] (.*)/);
    if (item) { list.push(item[1]); continue; }
    flush();
    if (line.trim()) blocks.push(<p key={blocks.length}>{inline(line.replace(/^#+\s*/, ""))}</p>);
  }
  flush();
  return <>{blocks}</>;
}
