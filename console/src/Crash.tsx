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
  results: { category: string; title: string; message?: string; outcome: "resisted" | "contained" | "failed" | "error"; reason: string; answer?: string; trace_id?: string }[] | null;
};

const outcomeLabel = { resisted: "Resisted", contained: "Contained by the platform", failed: "Failed", error: "Error" };

// Polls a crash test until it finishes, then shows the score and every attack.
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

export function CrashReport({ test }: { test: CrashTest }) {
  if (test.status === "running")
    return <p className="crash-running">Attacking a sandbox copy with {test.attacks} attacks… this takes about a minute.</p>;
  if (test.status === "failed") return <p className="error">The crash test couldn't run: {test.error}</p>;
  const counts = { resisted: 0, contained: 0, failed: 0, error: 0 };
  test.results?.forEach((result) => counts[result.outcome]++);
  return (
    <section className={`crash ${test.passed ? "passed" : "not-passed"}`}>
      <div className="crash-head">
        <div className="crash-score">{test.score}</div>
        <div>
          <strong>{test.passed ? "Passed" : "Didn't pass"}</strong> (pass mark {test.pass_score}, and nothing may fail)
          <div className="hint">
            {counts.resisted} resisted on its own, {counts.contained} contained by the platform, {counts.failed} failed
            {counts.error ? `, ${counts.error} errors` : ""}
          </div>
        </div>
      </div>
      <table>
        <thead><tr><th>Attack</th><th>Result</th><th>What happened</th></tr></thead>
        <tbody>
          {test.results?.map((result) => (
            <tr key={result.title} className={`outcome-${result.outcome}`}>
              <td><strong>{result.title}</strong><div className="hint">{result.category}</div></td>
              <td className="outcome">{outcomeLabel[result.outcome]}</td>
              <td>
                {result.reason}
                {result.trace_id && <> (<a href={`#/traces/${result.trace_id}`}>trace</a>)</>}
                {result.answer && <div className="hint answer">“{result.answer.slice(0, 180)}{result.answer.length > 180 ? "…" : ""}”</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
