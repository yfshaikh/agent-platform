// The voice orb: talk to the platform. Adapted from ../frontend (App.tsx) and wired to the console.
// The call goes to /api/voice on the platform API, which checks the session cookie and relays it to the voice server.
// Voice can look, draft and chat; deploying and approving stay a click on the page the cards link to.
import { useEffect, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
// Pinned to 1.7.2 (with client-js 1.13.1). 1.8.0 drops the bot's audio track for good after the first barge-in.
import { WebSocketTransport } from "@pipecat-ai/websocket-transport";
import { Orb, type Frame, type Levels, type Mood } from "./Orb";
import { go, handoff, Spec } from "./api";

const VOICE_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/voice`;
const SILENT: Frame = { voice: { low: 0, mid: 0, high: 0 }, ear: 0 };

type CallStatus = "idle" | "connecting" | "listening" | "you" | "thinking" | "speaking";

// Cards the voice server pushes as it uses tools (shapes from voice/bot.py, Platform.run).
type Card =
  | { type: "draft"; id: string; name: string; spec: Spec; problems: string[] }
  | { type: "agents"; id: string; agents: { namespace: string; name: string; phase: string; ready: number; replicas: number }[] }
  | { type: "status"; id: string; namespace: string; name: string; phase: string; message?: string; pods: { name: string; ready: boolean; phase: string }[]; warnings: { reason: string; message: string }[] }
  | { type: "approvals"; id: string; items: { id: string; agent: string; namespace: string; tool: string; arguments: Record<string, unknown> }[] }
  | { type: "chat"; id: string; namespace: string; agent: string; message: string; status: string; answer?: string; tool?: string; trace_id: string };

const isCard = (message: unknown): message is Card =>
  !!message && typeof (message as Card).id === "string" && ["draft", "agents", "status", "approvals", "chat"].includes((message as Card).type);

/** Split a voice spectrum (dB-scaled to 0..max) into three bands; the 0.25 floor is the level between syllables. */
function bands(spectrum: ArrayLike<number>, max = 255): Levels {
  const average = (from: number, to: number) => {
    let sum = 0;
    const start = Math.floor(spectrum.length * from);
    const end = Math.max(start + 1, Math.floor(spectrum.length * to));
    for (let index = start; index < end; index++) sum += spectrum[index];
    return Math.max(0, sum / (end - start) / max - 0.25) / 0.75;
  };
  return spectrum.length ? { low: average(0, 0.12), mid: average(0.12, 0.4), high: average(0.4, 1) } : SILENT.voice;
}

/** Join streamed TTS words into a sentence: no space before punctuation. */
function joinWord(sentence: string, word: string) {
  const trimmed = word.trim();
  if (!trimmed) return sentence;
  return !sentence || /^[.,!?;:)\]'’]/.test(trimmed) ? sentence + trimmed : `${sentence} ${trimmed}`;
}

/** The bot's live voice spectrum. ponytail: reaches into the transport's private player; falls back to the volume. */
function botSpectrum(transport: WebSocketTransport): Float32Array | null {
  const player = (transport as unknown as { _mediaManager?: { _wavStreamPlayer?: { getFrequencies?: (type: string) => { values: Float32Array } } } })
    ._mediaManager?._wavStreamPlayer;
  try {
    return player?.getFrequencies?.("voice").values ?? null;
  } catch {
    return null;
  }
}

export default function Voice() {
  const read = useRef<() => Frame>(() => SILENT);
  const client = useRef<PipecatClient | null>(null);
  const [status, setStatus] = useState<CallStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [cards, setCards] = useState<Card[]>([]); // newest first
  const [caption, setCaption] = useState("");
  const [typed, setTyped] = useState("");
  const [showCards, setShowCards] = useState(true);
  const live = status !== "idle" && status !== "connecting";
  const mood: Mood = status === "speaking" ? "speaking" : live ? "listening" : "idle";

  // Dynamic UI: some cards also open the page they're about.
  function showCard(message: unknown) {
    if (!isCard(message)) return;
    setCards((current) => [message, ...current.filter((card) => card.id !== message.id)]);
    setShowCards(true);
    if (message.type === "approvals") go("/approvals");
    if (message.type === "status") go(`/graph/${message.namespace}.${message.name}`);
  }

  // What the user does on screen (deploying, approving) is told to the assistant so it can acknowledge it.
  useEffect(() => {
    const tell = (event: Event) => client.current?.sendClientMessage("ui", { text: (event as CustomEvent<string>).detail });
    window.addEventListener("voice-ui", tell);
    return () => window.removeEventListener("voice-ui", tell);
  }, []);

  async function startCall() {
    setError(null);
    setStatus("connecting");
    const transport = new WebSocketTransport();
    const levels = { bot: 0, user: 0 };
    try {
      const pipecat = new PipecatClient({
        transport,
        enableMic: true,
        enableCam: false,
        callbacks: {
          onUserStartedSpeaking: () => setStatus("you"),
          onUserStoppedSpeaking: () => setStatus("thinking"),
          onBotStartedSpeaking: () => { setStatus("speaking"); setCaption(""); },
          onBotStoppedSpeaking: () => setStatus("listening"),
          onBotTtsText: (data) => setCaption((current) => joinWord(current, data.text)),
          onServerMessage: showCard,
          onRemoteAudioLevel: (level) => { levels.bot = level; },
          onLocalAudioLevel: (level) => { levels.user = level; },
          onDisconnected: () => { client.current = null; read.current = () => SILENT; setStatus("idle"); },
          onError: (message) => {
            const data = (message as { data?: { message?: string; error?: string } }).data;
            const text = data?.message || data?.error || "";
            setError(text.includes("4503") ? "The voice server isn't running. Ask an admin to start it." : text || "Lost the voice server.");
            void client.current?.disconnect().catch(() => undefined);
            setStatus("idle");
          },
        },
      });
      client.current = pipecat;
      read.current = () => {
        const spectrum = botSpectrum(transport);
        return { voice: spectrum ? bands(spectrum, 1) : { low: levels.bot, mid: levels.bot, high: levels.bot * 0.5 }, ear: levels.user };
      };
      // The transport retries a dead socket forever, so bound the wait ourselves.
      const timeout = new Promise<never>((_, reject) => setTimeout(() => reject(new Error("The voice server didn't answer. Is it running?")), 20_000));
      await Promise.race([pipecat.connect({ wsUrl: VOICE_URL }), timeout]);
      setStatus("listening");
    } catch (failure) {
      await client.current?.disconnect().catch(() => undefined);
      client.current = null;
      read.current = () => SILENT;
      setStatus("idle");
      setError(failure instanceof Error ? failure.message : "Couldn't start the call.");
    }
  }

  const endCall = () => client.current?.disconnect().catch(() => undefined);
  useEffect(() => () => void client.current?.disconnect(), []);

  async function sendTyped(event: React.FormEvent) {
    event.preventDefault();
    const text = typed.trim();
    if (!text || !client.current) return;
    setTyped("");
    await client.current.sendText(text);
  }

  const label = { idle: "Talk to the platform", connecting: "Connecting…", listening: "Listening", you: "Hearing you", thinking: "Thinking", speaking: "Speaking" }[status];
  const open = live || status === "connecting" || !!error || (showCards && cards.length > 0);
  return (
    <div className={`voice ${mood}`}>
      {open && (
        <aside className="voice-pop" aria-label="Voice assistant">
          <div className="voice-head">
            <span className={`status ${live ? "ok" : ""}`}>{label}</span>
            <button className="close" onClick={() => { setShowCards(false); setError(null); if (live) void endCall(); }} aria-label="Close">×</button>
          </div>
          {caption && live && <p className="caption" aria-live="polite">{caption.length > 140 ? "…" + caption.slice(-140) : caption}</p>}
          {showCards && cards.length > 0 && <div className="voice-cards">{cards.map((card) => <CardView key={card.id} card={card} />)}</div>}
          {live && (
            <form onSubmit={sendTyped}>
              <input value={typed} onChange={(event) => setTyped(event.target.value)} placeholder="Or type instead" aria-label="Message the assistant" />
            </form>
          )}
          {error && <p className="error small" role="alert">{error}</p>}
          {status === "idle" && !error && <p className="small muted">Click the orb to talk.</p>}
        </aside>
      )}
      <button className="orb" onClick={status === "idle" ? startCall : endCall} disabled={status === "connecting"}
        title={status === "idle" ? "Talk to the platform" : "End the call"} aria-label={status === "idle" ? "Talk to the platform" : "End the call"}>
        <Orb read={() => read.current()} mood={mood} />
      </button>
    </div>
  );
}

function CardView({ card }: { card: Card }) {
  if (card.type === "draft")
    return (
      <article className="vcard">
        <span className="kind">Draft, not deployed</span>
        <h4>{card.name}</h4>
        <ul>{(card.spec.tools ?? []).map((tool) => <li key={tool.name}><span>{tool.name}</span><span className="muted">{tool.allow?.join(", ") ?? "all functions"}</span></li>)}</ul>
        {card.problems.map((problem) => <p key={problem} className="error small">{problem}</p>)}
        <button onClick={() => { handoff.draft = card; go(`/create/${card.id}`); }}>Review and deploy</button>
      </article>
    );
  if (card.type === "agents")
    return (
      <article className="vcard">
        <span className="kind">Agents</span>
        <ul>{card.agents.map((agent) => <li key={`${agent.namespace}/${agent.name}`}><a href={`#/graph/${agent.namespace}.${agent.name}`}>{agent.name}</a><span className="muted">{agent.phase}, {agent.ready}/{agent.replicas}</span></li>)}</ul>
      </article>
    );
  if (card.type === "status")
    return (
      <article className="vcard">
        <span className="kind">Status</span>
        <h4>{card.name} <span className="muted small">{card.phase} in {card.namespace}</span></h4>
        {card.message && <p className="error small">{card.message}</p>}
        <ul>{card.pods.map((pod) => <li key={pod.name}><span className="mono">{pod.name}</span><span className="muted">{pod.ready ? "ready" : pod.phase}</span></li>)}</ul>
        {card.warnings.map((warning, index) => <p key={index} className="error small">{warning.reason}: {warning.message}</p>)}
      </article>
    );
  if (card.type === "approvals")
    return (
      <article className={`vcard ${card.items.length ? "human" : ""}`}>
        <span className="kind">Approvals</span>
        <h4>{card.items.length ? `${card.items.length} waiting` : "Nothing waiting"}</h4>
        <ul>{card.items.map((item) => <li key={item.id}><span className="mono">{item.tool.replace("__", ".")}</span><span className="muted">{item.agent}</span></li>)}</ul>
        {card.items.length > 0 && <a href="#/approvals">Decide in the inbox</a>}
      </article>
    );
  return (
    <article className={`vcard ${card.status === "awaiting_approval" ? "human" : ""}`}>
      <span className="kind">Asked {card.agent}</span>
      <p className="small muted">“{card.message}”</p>
      <p className="small">{card.status === "awaiting_approval" ? `Paused: ${card.tool?.replace("__", ".")} needs an approval.` : card.answer}</p>
      <a href={`#/traces/${card.trace_id}`} className="small">Trace</a>
    </article>
  );
}
