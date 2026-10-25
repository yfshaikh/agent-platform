import { useEffect, useRef } from "react";

export type Levels = { low: number; mid: number; high: number };
export type Frame = { voice: Levels; ear: number }; // voice = the assistant speaking, ear = how loud you are
export type Mood = "idle" | "listening" | "speaking";

// Fast attack, slow release: swells on a syllable and eases back in the gaps.
const env = (current: number, target: number, dt: number, up = 30, down = 16) =>
  current + (target - current) * (1 - Math.exp(-dt * (target > current ? up : down)));

// One blob per voice band (lavender follows your mic), swirling inside a glass circle. Drawn on a plain canvas,
// replacing the Spline scene from ../frontend (same idea, no watermark, no 3D runtime).
const BLOBS = [
  { color: "79,140,255", band: "low" },
  { color: "79,178,182", band: "mid" },
  { color: "190,230,90", band: "high" },
  { color: "176,150,255", band: "ear" },
] as const;

export function Orb({ read, mood }: { read: () => Frame; mood: Mood }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const live = useRef({ read, mood });
  live.current = { read, mood };

  useEffect(() => {
    const element = canvas.current!;
    const context = element.getContext("2d")!;
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const state = { t: 0, spin: 0, open: 0, energy: 0, low: 0, mid: 0, high: 0, ear: 0, blink: 0, nextBlink: 2 };
    let last = performance.now();
    let raf = 0;

    const tick = (now: number) => {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      state.t += dt;
      const { read, mood } = live.current;
      const { voice, ear } = read();
      const energy = Math.min(1, (voice.low * 0.5 + voice.mid) * 1.2);
      state.open = env(state.open, energy, dt);
      state.energy = env(state.energy, energy, dt, 6, 2);
      state.low = env(state.low, voice.low, dt);
      state.mid = env(state.mid, voice.mid, dt);
      state.high = env(state.high, voice.high, dt);
      state.ear = env(state.ear, mood === "listening" ? Math.min(1, ear * 3) : 0, dt, 10, 3);
      state.spin += still ? 0 : dt * (0.35 + state.energy * 2.2 + state.ear * 1.2);

      const size = element.clientWidth * devicePixelRatio;
      if (element.width !== size) element.width = element.height = size;
      const r = size / 2;
      context.clearRect(0, 0, size, size);
      context.save();
      context.beginPath();
      context.arc(r, r, r, 0, Math.PI * 2);
      context.clip();
      context.fillStyle = "#082f34";
      context.fillRect(0, 0, size, size);
      context.globalCompositeOperation = "lighter";
      BLOBS.forEach((blob, index) => {
        const level = state[blob.band];
        const angle = state.spin + (index * Math.PI) / 2;
        const x = r + Math.cos(angle) * r * 0.38;
        const y = r + Math.sin(angle * 1.3) * r * 0.3;
        const radius = r * (0.5 + level * 0.3);
        const gradient = context.createRadialGradient(x, y, 0, x, y, radius);
        gradient.addColorStop(0, `rgba(${blob.color},0.65)`);
        gradient.addColorStop(1, `rgba(${blob.color},0)`);
        context.fillStyle = gradient;
        context.fillRect(0, 0, size, size);
      });
      context.globalCompositeOperation = "source-over";
      const glass = context.createRadialGradient(r * 0.6, r * 0.38, 0, r * 0.6, r * 0.38, r * 0.5);
      glass.addColorStop(0, "rgba(255,255,255,0.28)");
      glass.addColorStop(1, "rgba(255,255,255,0)");
      context.fillStyle = glass;
      context.fillRect(0, 0, size, size);

      // Eyes: blink every 2–5 s, squash and hop on syllables, stand tall while you talk.
      state.nextBlink -= dt;
      if (state.nextBlink < 0) { state.blink = 1; state.nextBlink = 2 + Math.random() * 3; }
      state.blink = Math.max(0, state.blink - dt * 7);
      const lid = 1 - Math.sin(state.blink * Math.PI) * 0.85;
      const eyeHeight = r * 0.26 * lid * (1 - state.open * 0.35 + state.ear * 0.18);
      const eyeWidth = r * 0.11 * (1 + state.open * 0.25);
      const eyeY = r * 1.05 - state.open * r * 0.06 + (still ? 0 : Math.sin(state.t * 1.2) * r * 0.03);
      context.fillStyle = "#ffffff";
      context.shadowColor = "rgba(0,0,0,0.35)";
      context.shadowBlur = r * 0.08;
      for (const side of [-1, 1]) {
        context.beginPath();
        context.roundRect(r + side * r * 0.22 - eyeWidth / 2, eyeY - eyeHeight / 2, eyeWidth, eyeHeight, eyeWidth / 2);
        context.fill();
      }
      context.restore();
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  return <canvas ref={canvas} style={{ width: "100%", height: "100%", display: "block", borderRadius: "50%" }} aria-hidden />;
}
