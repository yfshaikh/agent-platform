// Every call goes to the platform API (same origin; the session cookie logs us in).

export type Spec = {
  model: string;
  instructions: string;
  tools?: { name: string; allow?: string[]; approval?: Record<string, string> }[];
  talksTo?: string[];
  replicas?: number;
  budget?: { tokensPerDay?: number; maxStepsPerRequest?: number };
};
export type Agent = {
  namespace: string;
  name: string;
  spec: Spec;
  status: { phase?: string; message?: string; readyReplicas?: number };
  tokens_today: number;
};
export type Tool = { name: string; description: string; classes: Record<string, "read" | "write" | "sensitive"> };
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
