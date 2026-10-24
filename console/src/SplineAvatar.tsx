import { useEffect, useRef } from 'react'
import { Application, type SPEObject } from '@splinetool/runtime'

export type Levels = { low: number; mid: number; high: number }
export type Frame = { voice: Levels; ear: number } // voice drives the mouth, ear = how loud the user is
export type Mood = 'idle' | 'listening' | 'speaking'

// Fast attack, slow release: the jaw snaps open on a syllable and eases shut.
const env = (cur: number, target: number, dt: number, up = 30, down = 16) =>
  cur + (target - cur) * (1 - Math.exp(-dt * (target > cur ? up : down)))

// Parts named in the Spline file: a frosted Glass sphere over glowing blobs, with two pill eyes.
// Orb and Blobs are Empties, so rotating Blobs swirls the colours inside the glass.
const PARTS = ['Orb', 'Blobs', 'Blob Blue', 'Blob Teal', 'Blob Lime', 'Blob Lavender', 'Eye L', 'Eye R'] as const
type Part = (typeof PARTS)[number]
type Rest = { obj: SPEObject; p: SPEObject['position']; r: SPEObject['rotation']; s: SPEObject['scale'] }

export function SplineAvatar({ scene, read, mood }: { scene: string; read: () => Frame; mood: Mood }) {
  const host = useRef<HTMLDivElement>(null)
  const live = useRef({ read, mood })
  live.current = { read, mood }

  useEffect(() => {
    // Fresh canvas per mount: a disposed Application leaves its WebGPU context behind, and reusing that
    // canvas (StrictMode mounts twice in dev) breaks the next one.
    const canvas = host.current!.appendChild(Object.assign(document.createElement('canvas'), { style: 'width:100%;height:100%;display:block' }))
    const app = new Application(canvas)
    let disposed = false
    const pointer = { x: 0, y: 0 }
    const onMove = (e: PointerEvent) => {
      pointer.x = (e.clientX / innerWidth) * 2 - 1
      pointer.y = -((e.clientY / innerHeight) * 2 - 1)
    }
    addEventListener('pointermove', onMove)
    let raf = 0

    app.load(scene).then(() => {
      if (disposed) return
      // Snapshot the authored pose once; every frame writes rest + offset, so nothing drifts.
      const rig = {} as Record<Part, Rest>
      for (const name of PARTS) {
        const obj = app.findObjectByName(name)
        if (!obj) { console.warn(`Spline scene is missing "${name}"`); continue }
        // Copy x/y/z explicitly: rotation is a three.js Euler (x/y/z are getters), so a spread yields NaN later.
        const xyz = (v: { x: number; y: number; z: number }) => ({ x: v.x, y: v.y, z: v.z })
        rig[name] = { obj, p: xyz(obj.position), r: xyz(obj.rotation), s: xyz(obj.scale) }
      }
      const set = (name: Part, f: (o: SPEObject, rest: Rest) => void) => rig[name] && f(rig[name].obj, rig[name])

      const st = { t: 0, open: 0, energy: 0, ear: 0, low: 0, mid: 0, high: 0, spin: 0, blink: 0, nextBlink: 2, yaw: 0, pitch: 0 }
      let last = performance.now()

      const tick = (now: number) => {
        const dt = Math.min((now - last) / 1000, 0.05)
        last = now
        st.t += dt
        const { read, mood } = live.current
        const { voice, ear } = read()

        // Syllable envelope: snaps open on each syllable, eases shut in the gaps.
        const energy = Math.min(1, (voice.low * 0.5 + voice.mid) * 1.2)
        st.open = env(st.open, energy, dt)
        st.energy = env(st.energy, energy, dt, 6, 2) // slow version, for body language
        st.ear = env(st.ear, mood === 'listening' ? Math.min(1, ear * 3) : 0, dt, 10, 3)

        // Each blob is one band of the voice spectrum, so the colours inside the glass pulse with speech.
        st.low = env(st.low, voice.low, dt)
        st.mid = env(st.mid, voice.mid, dt)
        st.high = env(st.high, voice.high, dt)
        const bandOf = { 'Blob Blue': st.low, 'Blob Teal': st.mid, 'Blob Lime': st.high, 'Blob Lavender': st.ear } as const
        for (const [blob, v] of Object.entries(bandOf) as [Part, number][])
          set(blob, (o, r) => {
            const k = 1 + v * 0.2 // more than ~0.2 pushes the outer blobs through the glass
            o.scale.x = r.s.x * k
            o.scale.y = r.s.y * k
            o.scale.z = r.s.z * k
          })

        // Blobs swirl slowly at rest and faster while talking or hearing you.
        st.spin += dt * (0.25 + st.energy * 2.2 + st.ear * 1.2)
        set('Blobs', (o, r) => {
          o.rotation.z = r.r.z + st.spin
          o.rotation.y = r.r.y + Math.sin(st.spin * 0.6) * 0.25
        })

        // Eyes: blink every 2–5s, squash-and-hop on syllables, stretch tall (attentive) while you talk,
        // and drift toward the pointer like they're looking at it.
        st.nextBlink -= dt
        if (st.nextBlink < 0) { st.blink = 1; st.nextBlink = 2 + Math.random() * 3 }
        st.blink = Math.max(0, st.blink - dt * 7)
        const lid = 1 - Math.sin(st.blink * Math.PI) * 0.85
        st.yaw = env(st.yaw, pointer.x, dt, 5, 5)
        st.pitch = env(st.pitch, pointer.y, dt, 5, 5)
        for (const eye of ['Eye L', 'Eye R'] as const)
          set(eye, (o, r) => {
            o.scale.y = r.s.y * lid * (1 - st.open * 0.35 + st.ear * 0.18)
            o.scale.x = r.s.x * (1 + st.open * 0.25)
            o.position.x = r.p.x + st.yaw * 30
            o.position.y = r.p.y + st.pitch * 18 + st.open * 10
          })

        // Whole orb: gentle float, turns a little toward the pointer, swells while talking or listening.
        set('Orb', (o, r) => {
          const k = 1 + st.energy * 0.05 + st.ear * 0.04 + Math.sin(st.t * 1.6) * 0.01
          o.scale.x = r.s.x * k
          o.scale.y = r.s.y * k
          o.scale.z = r.s.z * k
          o.position.y = r.p.y + Math.sin(st.t * 1.2) * 8
          o.rotation.y = r.r.y + st.yaw * 0.25
          o.rotation.x = r.r.x - st.pitch * 0.15
        })

        raf = requestAnimationFrame(tick)
      }
      raf = requestAnimationFrame(tick)
    })

    return () => {
      disposed = true
      cancelAnimationFrame(raf)
      removeEventListener('pointermove', onMove)
      app.dispose()
      canvas.remove()
    }
  }, [scene])

  return <div ref={host} style={{ position: 'absolute', inset: 0 }} />
}
