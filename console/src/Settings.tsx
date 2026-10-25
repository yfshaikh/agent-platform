import { useEffect, useState } from "react";
import { Agent, api } from "./api";

type Paging = { on: boolean; ready: boolean; every_seconds: number; last_call_at: string | null; next_call_in_seconds: number };

// Admin settings: on-call phone calls (on/off, reset the wait between calls) and agents' daily token budgets.
export function Settings({ agents }: { agents: Agent[] }) {
  const [paging, setPaging] = useState<Paging | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const load = () => api<Paging>("/gpu-nodes/paging").then(setPaging, (error) => setMessage((error as Error).message));
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  async function act(path: string, method: "PUT" | "POST", json: unknown, done: string) {
    try {
      const result = await api<Paging>(path, { method, json });
      if (path.startsWith("/gpu-nodes/paging")) setPaging(result);
      setMessage(done);
    } catch (error) {
      setMessage((error as Error).message);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Admin controls. Every change here goes in the audit log.</p>
        </div>
      </div>
      {message && <p className="notice" style={{ marginBottom: "1rem" }}>{message}</p>}

      <section className="settings-card" aria-label="On-call phone calls">
        <h2>On-call phone calls</h2>
        <p className="small muted">
          When an alert's fix is waiting for approval, the platform phones the on-call engineer through Cartesia. The call only explains;
          approving stays a click here. At most one call every {Math.round((paging?.every_seconds ?? 300) / 60)} minutes.
        </p>
        {paging && !paging.ready && <p className="notice bad">Not set up: add the Cartesia pager settings to .env and run sync_secrets.py (see the README).</p>}
        {paging && (
          <div className="settings-row">
            <label className="switch">
              <input type="checkbox" checked={paging.on} disabled={!paging.ready}
                onChange={(event) => act("/gpu-nodes/paging", "PUT", { on: event.target.checked }, event.target.checked ? "On-call calls turned on." : "On-call calls turned off.")} />
              {" "}Phone the on-call engineer
            </label>
            <span className="small muted">
              {paging.last_call_at ? `Last call ${new Date(paging.last_call_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}. ` : "No calls yet. "}
              {paging.next_call_in_seconds > 0 ? `Next call allowed in ${Math.ceil(paging.next_call_in_seconds / 60)} min.` : "A call can go out now."}
            </span>
            <span className="spacer" />
            <button className="secondary" disabled={paging.next_call_in_seconds === 0}
              onClick={() => act("/gpu-nodes/paging/reset-timer", "POST", {}, "Timer reset: the next alert can call right away.")}>
              Reset call timer
            </button>
          </div>
        )}
      </section>

      <section className="settings-card" aria-label="Token budgets">
        <h2>Daily token budgets</h2>
        <p className="small muted">Each agent stops when it reaches its daily limit (set in its spec). Resetting clears today's count so it can run again.</p>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Agent</th><th>Used today</th><th className="num">Daily limit</th><th /></tr></thead>
            <tbody>
              {agents.length === 0 && <tr><td colSpan={4} className="muted">No agents.</td></tr>}
              {agents.map((agent) => {
                const limit = agent.spec.budget?.tokensPerDay ?? 2000000;
                return (
                  <tr key={`${agent.namespace}/${agent.name}`}>
                    <td>{agent.namespace}/{agent.name}</td>
                    <td>
                      <div className="budget-cell">
                        <span>{agent.tokens_today.toLocaleString()}</span>
                        <div className="bar"><span style={{ width: `${Math.min(100, (100 * agent.tokens_today) / limit)}%` }} className={agent.tokens_today >= limit ? "high" : ""} /></div>
                      </div>
                    </td>
                    <td className="num">{limit.toLocaleString()}</td>
                    <td className="num">
                      <button className="link small" disabled={agent.tokens_today === 0}
                        onClick={() => confirm(`Reset today's tokens for ${agent.name}?`) &&
                          act(`/usage/${agent.namespace}/${agent.name}/reset`, "POST", {}, `Reset today's tokens for ${agent.name}.`)}>
                        Reset
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
