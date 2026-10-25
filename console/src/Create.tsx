import { useEffect, useState } from "react";
import YAML from "yaml";
import { api, ApiError, go, handoff, Spec, tellVoice, useCan } from "./api";
import { CrashReport, useCrashTest } from "./Crash";

type Template = { id: string; title: string; description: string; name: string; spec: Spec };

// Pick a template or describe the agent; review and edit the spec; deploy. Nothing is created until "Deploy".
export function Create() {
  const canBuild = useCan("build");
  const canApprove = useCan("approve");
  const [templates, setTemplates] = useState<Template[]>([]);
  const [picked, setPicked] = useState("");
  const [description, setDescription] = useState("");
  const [namespace, setNamespace] = useState("team-demo");
  const [name, setName] = useState("");
  const [specText, setSpecText] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState<"" | "draft" | "deploy">("");
  const [testId, setTestId] = useState<string | null>(null);
  const [gate, setGate] = useState(""); // why the deploy gate said no; approvers may "Deploy anyway"
  const [deployed, setDeployed] = useState<{ namespace: string; name: string } | null>(null);
  const test = useCrashTest(testId);
  useEffect(() => setGate(""), [test?.status]); // a finished test makes the old gate message stale
  useEffect(() => {
    void api<Template[]>("/templates").then(setTemplates);
    if (handoff.draft) { // drafted by voice
      load(handoff.draft.name, handoff.draft.spec, handoff.draft.problems);
      handoff.draft = undefined;
    }
  }, []);

  function load(newName: string, spec: Spec, newProblems: string[] = []) {
    setName(newName);
    setSpecText(YAML.stringify(spec));
    setProblems(newProblems);
    setTestId(null);
    setGate("");
  }

  function parsedSpec(): Spec | null {
    try {
      return YAML.parse(specText);
    } catch (error) {
      setProblems([`The spec isn't valid YAML: ${(error as Error).message}`]);
      return null;
    }
  }

  async function crashTest() {
    const spec = parsedSpec();
    if (!spec) return;
    setGate("");
    try {
      const started = await api<{ id: string }>("/crash-tests", { method: "POST", json: { name, spec } });
      setTestId(started.id);
    } catch (error) {
      setProblems([(error as Error).message]);
    }
  }

  async function draft(event: React.FormEvent) {
    event.preventDefault();
    setBusy("draft");
    setPicked("");
    try {
      const result = await api<{ name: string; spec: Spec; problems: string[] }>("/agents/draft", { method: "POST", json: { description } });
      load(result.name, result.spec, result.problems);
    } catch (error) {
      setProblems([(error as Error).message]);
    } finally {
      setBusy("");
    }
  }

  async function deploy(deployAnyway = false) {
    const spec = parsedSpec();
    if (!spec) return;
    if (deployAnyway && !confirm("Deploy without a passing crash test? This is recorded in the audit log.")) return;
    setBusy("deploy");
    try {
      await api("/agents", { method: "POST", json: { namespace, name, spec, crash_test_id: testId, deploy_anyway: deployAnyway } });
      tellVoice(`The user deployed the ${name} agent to ${namespace}.`);
      setDeployed({ namespace, name });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) setGate(error.message);
      else setProblems([(error as Error).message]);
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Build an agent</h1>
          <p>Start from a template or describe the job. You see the full spec, and can crash-test it, before anything runs.</p>
        </div>
      </div>
      <div className={`create ${specText ? "" : "empty-review"}`}>
        <div className="start">
          <div className="templates">
            {templates.map((template) => (
              <button key={template.id} className="template" aria-pressed={picked === template.id}
                onClick={() => { setPicked(template.id); load(template.name, template.spec); }}>
                <strong>{template.title}</strong>
                <span>{template.description}</span>
              </button>
            ))}
          </div>
          <form className="describe" onSubmit={draft}>
            <label htmlFor="description">Or describe what it should do</label>
            <textarea id="description" rows={4} value={description} onChange={(event) => setDescription(event.target.value)}
              placeholder="Watch the research team's web searches and summarise what they found each morning" />
            <div className="row">
              <button className="secondary" disabled={description.trim().length < 3 || busy !== ""}>{busy === "draft" ? "Drafting…" : "Draft a spec"}</button>
            </div>
          </form>
          {!specText && problems.map((problem) => <p key={problem} className="error">{problem}</p>)}
        </div>

        {specText && (
          <section className="review" aria-label="Review">
            <h2>Review before deploying</h2>
            <div className="row">
              <label>Team<input value={namespace} onChange={(event) => setNamespace(event.target.value)} /></label>
              <label>Name<input value={name} onChange={(event) => setName(event.target.value)} /></label>
            </div>
            <label htmlFor="spec">Spec</label>
            <textarea id="spec" className="yaml" rows={16} value={specText} onChange={(event) => setSpecText(event.target.value)} spellCheck={false} />
            {problems.map((problem) => <p key={problem} className="error">{problem}</p>)}
            {deployed ? <DeployLog {...deployed} /> : canBuild ? (
              <div className="actions">
                <button className="secondary" onClick={crashTest} disabled={!name || test?.status === "running"}>
                  {test?.status === "running" ? "Crash test running…" : test ? "Run the crash test again" : "Run crash test"}
                </button>
                <button onClick={() => deploy()} disabled={busy !== "" || !name}>{busy === "deploy" ? "Deploying…" : "Deploy"}</button>
              </div>
            ) : <p className="notice">Your role can draft agents but not deploy them. Ask a builder to deploy this spec.</p>}
            {gate && (
              <div className="gate">
                <p>{gate}</p>
                {canApprove
                  ? <div className="actions"><button className="human" onClick={() => deploy(true)} disabled={busy !== ""}>Deploy anyway</button></div>
                  : <p className="small muted">Only an approver can deploy it without a passing test.</p>}
              </div>
            )}
            {test && <CrashReport test={test} />}
          </section>
        )}
      </div>
    </>
  );
}

type LogLine = { at: string; object: string; reason: string; message: string; source: string; warning: boolean };
type Log = { phase: string; message: string | null; ready: number; lines: LogLine[] };
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

// What happens after "Deploy", live: the API creates the Agent object, the operator applies the objects it becomes,
// then Kubernetes schedules the pod, starts the container and waits for its health check. All from real cluster events.
function DeployLog({ namespace, name }: { namespace: string; name: string }) {
  const [log, setLog] = useState<Log | null>(null);
  const done = !!log && ((log.phase === "Running" && log.ready > 0) || log.phase === "Failed");
  useEffect(() => {
    if (done) return;
    const timer = setInterval(() => api<Log>(`/agents/${namespace}/${name}/deploy-log`).then(setLog, () => {}), 1500);
    return () => clearInterval(timer);
  }, [namespace, name, done]);
  return (
    <section className="deploy-log" aria-label="Deploy log" aria-live="polite">
      <div className="terminal">
        <div className="line cmd">$ kubectl apply -f agent.yaml   <span># what the platform API did: created Agent {namespace}/{name}</span></div>
        {log?.lines.map((line, index) => (
          <div key={index} className={`line ${line.warning ? "warn" : ""}`}>
            <span className="at">{clock(line.at)}</span> <span className="who">{line.source || "kubernetes"}</span>{" "}
            <span className="obj">{line.object}</span> {line.reason}: {line.message}
          </div>
        ))}
        {!done && <div className="line wait">waiting for the cluster…</div>}
      </div>
      {log?.phase === "Failed" && <p className="notice bad">{log.message}</p>}
      {done && log?.phase === "Running" && (
        <div className="actions">
          <span className="status ok">{name} is running</span>
          <button onClick={() => go(`/graph/${namespace}.${name}`)}>Open {name}</button>
        </div>
      )}
    </section>
  );
}
