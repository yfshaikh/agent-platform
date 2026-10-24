import { useEffect, useState } from "react";
import YAML from "yaml";
import { api, go, Spec } from "./api";

type Template = { id: string; title: string; description: string; name: string; spec: Spec };

// Pick a template or describe the agent; review and edit the spec; deploy. Nothing is created until "Deploy agent".
export function Create() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [description, setDescription] = useState("");
  const [namespace, setNamespace] = useState("team-demo");
  const [name, setName] = useState("");
  const [specText, setSpecText] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState<"" | "draft" | "deploy">("");
  useEffect(() => void api<Template[]>("/templates").then(setTemplates), []);

  function load(newName: string, spec: Spec, newProblems: string[] = []) {
    setName(newName);
    setSpecText(YAML.stringify(spec));
    setProblems(newProblems);
  }

  async function draft(event: React.FormEvent) {
    event.preventDefault();
    setBusy("draft");
    try {
      const result = await api<{ name: string; spec: Spec; problems: string[] }>("/agents/draft", { method: "POST", json: { description } });
      load(result.name, result.spec, result.problems);
    } catch (error) {
      setProblems([(error as Error).message]);
    } finally {
      setBusy("");
    }
  }

  async function deploy() {
    let spec: Spec;
    try {
      spec = YAML.parse(specText);
    } catch (error) {
      setProblems([`The spec isn't valid YAML: ${(error as Error).message}`]);
      return;
    }
    setBusy("deploy");
    try {
      await api("/agents", { method: "POST", json: { namespace, name, spec } });
      go(`/graph/${namespace}.${name}`);
    } catch (error) {
      setProblems([(error as Error).message]);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="create">
      <h1>Create an agent</h1>
      <p className="lede">Start from a template or describe the job. You review the spec before anything is deployed.</p>

      <div className="templates">
        {templates.map((template) => (
          <button key={template.id} className="template" onClick={() => load(template.name, template.spec)}>
            <strong>{template.title}</strong>
            <span>{template.description}</span>
          </button>
        ))}
      </div>

      <form className="describe" onSubmit={draft}>
        <label htmlFor="description">Or describe what it should do</label>
        <textarea id="description" rows={3} value={description} onChange={(event) => setDescription(event.target.value)}
          placeholder="An agent that finds the cheapest flights from Austin and checks the weather there" />
        <button disabled={description.trim().length < 3 || busy !== ""}>{busy === "draft" ? "Drafting…" : "Draft a spec"}</button>
      </form>

      {specText && (
        <section className="review">
          <h2>Review</h2>
          <div className="row">
            <label>
              Team (namespace)
              <input value={namespace} onChange={(event) => setNamespace(event.target.value)} />
            </label>
            <label>
              Name
              <input value={name} onChange={(event) => setName(event.target.value)} />
            </label>
          </div>
          <label htmlFor="spec">Spec</label>
          <textarea id="spec" className="yaml" rows={18} value={specText} onChange={(event) => setSpecText(event.target.value)} spellCheck={false} />
          {problems.map((problem) => (
            <p key={problem} className="error">{problem}</p>
          ))}
          <button onClick={deploy} disabled={busy !== "" || !name}>{busy === "deploy" ? "Deploying…" : "Deploy agent"}</button>
        </section>
      )}
      {!specText && problems.map((problem) => <p key={problem} className="error">{problem}</p>)}
    </div>
  );
}
