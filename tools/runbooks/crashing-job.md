# A job crashes on its own

Example runbook written for this demo; NMC²'s teams would add their own.

## Symptoms
- A job exits with a non-zero code (not 137 or 143, which mean someone stopped it).
- The GPU and the other jobs look healthy.

## Diagnose
1. Read the job's log. Find the last lines before it stopped: the error and the line of code that raised it.
2. Read the job's script (`read_source`, `jobs/<template>.py`) to see why: a bug, a batch that is too big, a bad input.
3. Check whether other jobs on the node failed at the same time. If several did, suspect the node (go to the
   overheating runbook, or cordon the node); if only this one did, it's the job.

## Mitigate now (needs a human approval)
- Usually nothing to stop: the job is already dead. Restart it (`restart_job`) only if the cause was outside the job
  (for example another job holding the memory) and that cause is gone.

## Prevent it happening again
- If the job's own code is at fault, write up the exact line and a suggested fix for the customer; never edit it yourself.
- If the platform could have caught it (for example by refusing a job that can't fit), propose that change to the
  node agent as a diff.
