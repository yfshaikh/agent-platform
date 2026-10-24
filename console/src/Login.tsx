import { useState } from "react";
import { api } from "./api";

export function Mark({ size = 30 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 30 30" aria-hidden>
      <rect x="1" y="1" width="28" height="28" rx="7" fill="var(--ink)" />
      {[7, 13.5, 20].map((y, index) => (
        <g key={y}>
          <rect x="6" y={y} width="18" height="3.5" rx="1.2" fill="var(--paper)" opacity={0.85} />
          <circle cx="21.5" cy={y + 1.75} r="1.1" fill={index === 1 ? "var(--human)" : "var(--accent)"} />
        </g>
      ))}
    </svg>
  );
}

// Name and password. The very first login (before anyone has an account) uses a one-time admin link instead.
export function Login({ onLogin }: { onLogin: () => void }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/login", { method: "POST", json: { name, password } });
      onLogin();
    } catch (failure) {
      setError((failure as Error).message);
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <div className="login-box">
        <div className="mark">
          <Mark size={36} />
          <span>NMC² Agents<small>GPU cloud operations</small></span>
        </div>
        <form onSubmit={submit}>
          <h1>Sign in</h1>
          <label>
            Name
            <input value={name} onChange={(event) => setName(event.target.value)} autoComplete="username" autoFocus required />
          </label>
          <label>
            Password
            <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required />
          </label>
          {error && <p className="error" role="alert">{error}</p>}
          <button disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
        <p className="note">
          No account yet? An admin adds people under People. The first admin signs in with a one-time link from{" "}
          <code>deploy/login_link.py</code>.
        </p>
      </div>
    </main>
  );
}
