# Agent Platform (HackTX 2026, NMC² track)

A platform that lets users quickly create AI agents for enterprise-grade use.
Users write a short `Agent` object (model, instructions, tools, replicas, budget);
a Kubernetes operator turns it into an isolated, monitored, permission-limited deployment.

The pitch: the agent proposes, the platform enforces.

Showcase app on top: **Scam City**, a simulated town where AI scammers attack an AI bank assistant
(Capital One Nessie for accounts, SerpApi for real prices).

Build phases: 0 setup, 1 agent runtime, 2 operator, 3 tools, 4 guardrails, 5 platform API + UI,
6 Scam City, 7 AWS, 8 video + Devpost.
