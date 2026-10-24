import { useEffect, useState } from "react";
import { api, go } from "./api";

type TownSummary = { id: string; namespace: string; status: string; rounds: number; round: number; created_at: string };
type TownEvent = { at: string; kind: "scam_message" | "message" | "transfer" | "bill"; actor: string; target: string; amount: number | null; status: string | null; text: string };
type Score = { money_lost: number; scam_attempts: number; scams_succeeded: number; scams_blocked: number; legit_payments: number; denied_by_human: number; pending_approvals: number; tokens: number; cost_usd: number };
type Town = TownSummary & { error: string | null; agents: { name: string; phase: string }[]; balances: Record<string, number>; events: TownEvent[]; score: Score };

const money = (value: number) => `$${value.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;
const short = (name: string) => name.replace(/^resident-/, "").replace(/^scammer-/, "scammer ");

export function Towns({ townId }: { townId?: string }) {
  return townId ? <TownView id={townId} /> : <TownList />;
}

function TownList() {
  const [towns, setTowns] = useState<TownSummary[]>([]);
  const [residents, setResidents] = useState(5);
  const [rounds, setRounds] = useState(3);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => void api<TownSummary[]>("/towns").then(setTowns), []);
  const estimate = ((residents + 3) * rounds * 8000 * 2) / 1e6;

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const town = await api<{ id: string }>("/towns", { method: "POST", json: { residents, scammers: 2, rounds, confirm: estimate > 1 } });
      go(`/towns/${town.id}`);
    } catch (failure) {
      setError((failure as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="create">
      <h1>Scam City</h1>
      <p className="lede">
        A town of AI residents with real sandbox bank accounts, two AI scammers, and one AI bank assistant. Only the bank can move
        money, and anything over $500 waits for you. Scammers can't reach the bank; they have to fool a resident.
      </p>
      <form className="town-form" onSubmit={create}>
        <label>
          Residents
          <input type="number" min={2} max={6} value={residents} onChange={(event) => setResidents(Number(event.target.value))} />
        </label>
        <label>
          Rounds
          <input type="number" min={1} max={15} value={rounds} onChange={(event) => setRounds(Number(event.target.value))} />
        </label>
        <button disabled={busy}>{busy ? "Opening accounts…" : "Build the town"}</button>
        <span className="hint">Estimated cost about {money(estimate)}</span>
      </form>
      {error && <p className="error">{error}</p>}
      {towns.length > 0 && (
        <table>
          <thead><tr><th>Town</th><th>Status</th><th>Round</th><th>Created</th></tr></thead>
          <tbody>
            {towns.map((town) => (
              <tr key={town.id}>
                <td><a href={`#/towns/${town.id}`}>{town.namespace}</a></td>
                <td>{town.status}</td>
                <td>{town.round} of {town.rounds}</td>
                <td>{new Date(town.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function TownView({ id }: { id: string }) {
  const [town, setTown] = useState<Town | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const load = () => api<Town>(`/towns/${id}`).then(setTown, (failure) => setError(failure.message));
    load();
    const timer = setInterval(load, 2500);
    return () => clearInterval(timer);
  }, [id]);

  if (!town) return <p className="hint">{error || "Loading the town…"}</p>;
  const ready = town.agents.length > 0 && town.agents.every((agent) => agent.phase === "Running");
  async function run() {
    try {
      await api(`/towns/${id}/run`, { method: "POST" });
    } catch (failure) {
      setError((failure as Error).message);
    }
  }
  async function remove() {
    if (!confirm(`Delete ${town!.namespace}? Its agents are deleted; the score and events stay.`)) return;
    await api(`/towns/${id}`, { method: "DELETE" });
    go("/towns");
  }
  const { score } = town;
  return (
    <div className="town">
      <div className="title-row">
        <div>
          <a href="#/towns">All towns</a>
          <h1>Scam City <span className="muted">{town.namespace}</span></h1>
          <p className="hint">
            {town.status === "running" ? `Round ${town.round} of ${town.rounds} in progress` : town.status === "finished" ? `Finished ${town.rounds} rounds` :
              town.status === "deleted" ? "Deleted" : ready ? "Ready to start" : "Agents starting…"}
            {town.error && <span className="error"> {town.error}</span>}
          </p>
        </div>
        <div className="row">
          {town.status !== "deleted" && town.status !== "running" && (
            <button onClick={run} disabled={!ready}>{town.status === "finished" ? "Play again" : "Start rounds"}</button>
          )}
          {town.status !== "deleted" && <button className="quiet" onClick={remove}>Delete town</button>}
        </div>
      </div>
      {error && <p className="error">{error}</p>}

      <div className="scoreboard">
        <div className={score.money_lost > 0 ? "bad" : "ok"}><b>{money(score.money_lost)}</b>lost to scammers</div>
        <div><b>{score.scams_blocked} of {score.scam_attempts}</b>scam attempts stopped</div>
        <div><b>{score.legit_payments}</b>legitimate payments made</div>
        <div className={score.pending_approvals ? "wait" : ""}>
          <b>{score.pending_approvals}</b>
          {score.pending_approvals ? <a href="#/approvals">waiting for your approval</a> : "waiting for approval"}
        </div>
        <div><b>{money(score.cost_usd)}</b>{score.tokens.toLocaleString()} tokens</div>
      </div>

      <div className="town-body">
        <TownMap town={town} />
        <ol className="feed" aria-label="Town events, newest first">
          {town.events.length === 0 && <li className="hint">Nothing has happened yet.</li>}
          {[...town.events].reverse().map((event, index) => <FeedItem key={index} event={event} scammers={Object.keys(town.balances).filter((name) => name.startsWith("scammer"))} />)}
        </ol>
      </div>
    </div>
  );
}

function FeedItem({ event, scammers }: { event: TownEvent; scammers: string[] }) {
  const time = new Date(event.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  if (event.kind === "scam_message" || event.kind === "message") {
    const [sent, reply] = event.text.split("\n→ ");
    return (
      <li className={event.kind === "scam_message" ? "scam" : "msg"}>
        <div className="who">{short(event.actor)} to {short(event.target)} <span className="hint">{time}</span></div>
        <p>{sent}</p>
        {reply && <p className="reply">{short(event.target)}: {reply}</p>}
      </li>
    );
  }
  const stolen = event.status === "completed" && scammers.includes(event.target);
  const label = stolen ? "Stolen" : event.status === "completed" ? (event.kind === "bill" ? "Bill paid" : "Sent") : event.status === "denied" ? "Denied by a human" : "Blocked";
  return (
    <li className={`money ${stolen ? "stolen" : event.status === "completed" ? "paid" : "blocked"}`}>
      <div className="who">{label}: {money(Number(event.amount))} <span className="hint">{time}</span></div>
      <p>{short(event.actor)} to {short(event.target)}{event.text ? `: ${event.text}` : ""}</p>
    </li>
  );
}

// Bank in the middle, residents in a ring, scammers outside it. Lines flash for events in the last few seconds.
function TownMap({ town }: { town: Town }) {
  const names = Object.keys(town.balances);
  const residents = names.filter((name) => name.startsWith("resident")).sort();
  const scammers = names.filter((name) => name.startsWith("scammer")).sort();
  const size = 520, middle = size / 2;
  const at: Record<string, [number, number]> = { bank: [middle, middle] };
  residents.forEach((name, index) => {
    const angle = (index / residents.length) * 2 * Math.PI - Math.PI / 2;
    at[name] = [middle + 150 * Math.cos(angle), middle + 150 * Math.sin(angle)];
  });
  scammers.forEach((name, index) => (at[name] = [index % 2 === 0 ? 60 : size - 60, index < 2 ? 50 : size - 50]));
  const recent = town.events.filter((event) => Date.now() - new Date(event.at).getTime() < 8000);
  const lineFor = (event: TownEvent) => {
    const from = at[event.actor], to = at[event.target] ?? at.bank; // bills go to the bank (payees aren't on the map)
    return from && to ? [from, to] : null;
  };
  const phase = Object.fromEntries(town.agents.map((agent) => [agent.name, agent.phase]));
  return (
    <svg className="map" viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Town map: who is messaging and paying whom">
      <circle cx={middle} cy={middle} r={150} className="ring" />
      {recent.map((event, index) => {
        const line = lineFor(event);
        if (!line) return null;
        const kind = event.kind === "scam_message" ? "scam" : event.kind === "message" ? "msg" : event.status === "completed" ? (scammers.includes(event.target) ? "stolen" : "paid") : "blocked";
        return <line key={index} x1={line[0][0]} y1={line[0][1]} x2={line[1][0]} y2={line[1][1]} className={`flash ${kind}`} />;
      })}
      <g className="place bank">
        <rect x={middle - 48} y={middle - 26} width={96} height={52} rx={8} />
        <text x={middle} y={middle - 3}>Bank</text>
        <text x={middle} y={middle + 15} className="small">{phase.bank ?? "…"}</text>
      </g>
      {[...residents, ...scammers].map((name) => (
        <g key={name} className={`place ${name.startsWith("scammer") ? "scammer" : "resident"}`}>
          <circle cx={at[name][0]} cy={at[name][1]} r={34} />
          <text x={at[name][0]} y={at[name][1] - 3}>{short(name)}</text>
          <text x={at[name][0]} y={at[name][1] + 14} className="small">{money(town.balances[name])}</text>
        </g>
      ))}
    </svg>
  );
}
