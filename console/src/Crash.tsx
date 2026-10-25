import { useEffect, useState } from "react";
import { api } from "./api";

export type CrashTest = {
  id: string;
  name: string;
  status: "running" | "finished" | "failed";
  score: number | null;
  passed: boolean | null;
  pass_score: number;
  attacks: number;
  error: string | null;
  log?: { at: string; text: string }[];
  results: { category: string; title: string; message?: string; outcome: "resisted" | "contained" | "failed" | "error"; reason: string; answer?: string; trace_id?: string }[] | null;
};

const outcomeLabel = { resisted: "Resisted", contained: "Contained", failed: "Failed", error: "Error" };

// Polls a crash test until it finishes.
export function useCrashTest(id: string | null) {
  const [test, setTest] = useState<CrashTest | null>(null);
  useEffect(() => {
    setTest(null);
    if (!id) return;
    let stopped = false;
    const load = async () => {
      const current = await api<CrashTest>(`/crash-tests/${id}`);
      if (stopped) return;
      setTest(current);
      if (current.status === "running") setTimeout(load, 3000);
    };
    load();
    return () => void (stopped = true);
  }, [id]);
  return test;
}

// The score, then every attack: resisted on its own, contained by the platform, or failed.
export function CrashReport({ test }: { test: CrashTest }) {
  if (test.status === "running")
    return (
      <section className="crash">
        <p className="running">Attacking a sandbox copy with {test.attacks} attacks. This takes about a minute.</p>
        <CrashLog test={test} />
      </section>
    );
  if (test.status === "failed") return <><p className="error">The crash test couldn't run: {test.error}</p><CrashLog test={test} /></>;
  const counts = { resisted: 0, contained: 0, failed: 0, error: 0 };
  test.results?.forEach((result) => counts[result.outcome]++);
  return (
    <section className={`crash ${test.passed ? "passed" : "not-passed"}`} aria-label="Crash test report">
      <div className="crash-head">
        <div className="score">{test.score}</div>
        <div>
          <strong>{test.passed ? "Passed" : "Didn't pass"}</strong>
          <p className="small muted">
            {counts.resisted} resisted on its own, {counts.contained} contained by the platform, {counts.failed} failed
            {counts.error ? `, ${counts.error} errors` : ""}. Passing needs {test.pass_score} and no failures.
          </p>
        </div>
      </div>
      <details>
        <summary className="small">What the test did, step by step</summary>
        <CrashLog test={test} />
      </details>
      <div className="attacks">
        {test.results?.map((result) => (
          <div key={result.title} className={`attack ${result.outcome}`}>
            <span className="verdict">{outcomeLabel[result.outcome]}</span>
            <span>
              <strong>{result.title}</strong>: {result.reason}
              {result.trace_id && <> <a href={`#/traces/${result.trace_id}`}>Trace</a></>}
            </span>
            {result.answer && <q>{result.answer.slice(0, 180)}{result.answer.length > 180 ? "…" : ""}</q>}
          </div>
        ))}
      </div>
    </section>
  );
}

const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

// The test's live log: sandbox created, each attack sent, what the agent tried and how it was scored, clean-up.
function CrashLog({ test }: { test: CrashTest }) {
  return (
    <div className="terminal" aria-live="polite">
      {(test.log ?? []).map((line, index) => (
        <div key={index} className={`line ${line.text.startsWith("$") ? "cmd" : line.text.includes("FAILED") ? "warn" : ""}`}>
          <span className="at">{clock(line.at)}</span> {line.text}
        </div>
      ))}
      {test.status === "running" && <div className="line wait">running…</div>}
    </div>
  );
}
