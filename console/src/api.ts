// Every call goes to the platform API (same origin; the session cookie logs us in).
import { createContext, useContext } from "react";

export type Spec = {
  model: string;
  instructions: string;
  tools?: { name: string; allow?: string[]; approval?: Record<string, string>; approvalAbove?: Record<string, number>; nodes?: string[] }[];
  talksTo?: string[];
  replicas?: number;
  budget?: { tokensPerDay?: number; maxStepsPerRequest?: number };
};
export type Agent = {
  namespace: string;
  name: string;
  spec: Spec;
  status: { phase?: string; message?: string; readyReplicas?: number; crashTest?: { id: string | null; score: number | null; passed: boolean } };
  tokens_today: number;
};
export type GpuNodeEntry = { name: string; gpu?: string; provider?: string; connected?: boolean };
export type Tool = { name: string; description: string; classes: Record<string, "read" | "write" | "sensitive">; nodes?: GpuNodeEntry[] };
export type Step = {
  type: "model" | "tool";
  name?: string;
  model?: string;
  tokens?: number;
  arguments?: Record<string, unknown> | null;
  result?: string;
  rule?: string;
  class?: string | null;
  decision?: string | null;
  seconds: number;
};
export type Reply = {
  status: "answered" | "awaiting_approval" | "stopped" | "budget_exceeded";
  answer?: string;
  approval_id?: string;
  tool?: string;
  arguments?: Record<string, unknown>;
  trace_id: string;
  steps: Step[];
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    body: init?.json === undefined ? init?.body : JSON.stringify(init.json),
    headers: { "content-type": "application/json" },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? response.statusText);
    throw new ApiError(response.status, detail);
  }
  return body as T;
}

export const go = (path: string) => (window.location.hash = path);

// Who is logged in and what their role allows (the API enforces it; the console only hides what won't work).
export type Me = { user: string; role: string | null; permissions: string[] };
export const MeContext = createContext<Me>({ user: "", role: null, permissions: [] });
export const useCan = (permission: string) => useContext(MeContext).permissions.includes(permission);

// A draft handed from the voice dock to the Create page (which reads it once when it opens).
export type Draft = { name: string; spec: Spec; problems: string[] };
export const handoff: { draft?: Draft } = {};

// Tell the voice assistant what the user just did on screen (only heard if a call is open).
export const tellVoice = (text: string) => window.dispatchEvent(new CustomEvent("voice-ui", { detail: text }));
