# A GPU running hot

Example runbook written for this demo; NMC²'s teams would add their own.

## Symptoms
- An alert: the GPU stayed at or above the alert temperature (75°C on the T4) for 30 seconds.
- `list_gpus` may also show throttling. Power-cap throttling (the GPU sitting at its power limit) is normal under load;
  thermal slowdown is not.

## Diagnose
1. Read the GPU readings: temperature, utilization, power, throttle reasons, ECC errors.
2. List the jobs: is one job running at full load? Read its log to see if it's doing real work.
3. Healthy full load (high utilization, temperature steady below the slowdown point, no thermal throttling) is the GPU
   doing its job: report it and take no action.

## Mitigate now (needs a human approval)
- If the GPU shows thermal slowdown or keeps climbing: cordon the node (`cordon_node`) so no new jobs land on it.
  Running jobs keep running.
- Stop a job only if it is not real work (for example a stress test left running).
- Hardware errors (uncorrected ECC errors) mean the GPU itself may be failing: cordon and tell the hardware team.

## Prevent it happening again
- Check the node's cooling and airflow (a hardware ticket, not a code change).
- If one kind of job always runs hot, propose a limit in the node agent (for example fewer of those jobs at once).
