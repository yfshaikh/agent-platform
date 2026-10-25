# Working an incident as a team

Example runbook written for this demo; NMC²'s teams would add their own.

## Who does what
- **Triage** diagnoses the incident and proposes the immediate fix. It hands its diagnosis to the fixer before
  proposing the fix, so both happen while a human is being paged.
- **Fixer** turns the diagnosis into a prevention change: a diff to NMC²'s own code (the node agent or a job
  template), with the reason and the risk. It never applies anything; people review it.
- **The on-call engineer** is phoned by the platform once a fix waits for approval. The phone call can only explain;
  approvals happen in the console.

## What a good hand-off says
- The node, the failed job and the job causing it.
- The evidence: the exact error line and the readings, quoted, not paraphrased.
- The immediate fix being proposed.
- What you are unsure about.
