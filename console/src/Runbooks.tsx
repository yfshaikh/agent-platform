import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Markdown } from "./Incident";

type Listed = { name: string; title: string };

// The team's runbooks: the same files the agents search and read. With a section, that section is the one a fix cited.
export function Runbooks({ name, section }: { name?: string; section?: string }) {
  const [list, setList] = useState<Listed[]>([]);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const cited = useRef<HTMLElement>(null);

  useEffect(() => void api<Listed[]>("/runbooks").then(setList, (failure) => setError((failure as Error).message)), []);
  useEffect(() => {
    setText("");
    if (name) api<{ text: string }>(`/runbooks/${name}`).then((body) => setText(body.text), (failure) => setError((failure as Error).message));
  }, [name]);
  useEffect(() => cited.current?.scrollIntoView({ block: "center" }), [text, section]);

  if (error) return <p className="error">{error}</p>;
  if (!name)
    return (
      <>
        <div className="page-head">
          <div>
            <h1>Runbooks</h1>
            <p>How the team handles known incidents. The agents search and read these same files; to teach them a new one, add a Markdown file to tools/runbooks.</p>
          </div>
        </div>
        <div className="templates">
          {list.map((runbook) => (
            <a key={runbook.name} className="template" href={`#/runbooks/${runbook.name}`}>
              <strong>{runbook.title}</strong>
              <span className="mono">{runbook.name}</span>
            </a>
          ))}
        </div>
      </>
    );

  // "# Title", an intro, then one "## Section" each.
  const [head, ...parts] = text.split(/^## /m);
  const [title, ...intro] = head.split("\n");
  return (
    <article className="runbook">
      <div className="page-head">
        <div>
          <a href="#/runbooks" className="small">Runbooks</a>
          <h1>{title.replace(/^#\s*/, "")}</h1>
          <div className="muted"><Markdown text={intro.join("\n")} /></div>
        </div>
      </div>
      {parts.map((part) => {
        const [heading, ...body] = part.split("\n");
        const here = heading.trim() === section;
        return (
          <section key={heading} ref={here ? cited : undefined} className={`runbook-section ${here ? "cited" : ""}`}>
            <h2>{heading}</h2>
            {here && <p className="small cited-note">The fix follows this section.</p>}
            <div className="answer md"><Markdown text={body.join("\n")} /></div>
          </section>
        );
      })}
    </article>
  );
}
