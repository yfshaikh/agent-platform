# GPU out of memory (CUDA OOM)

Example runbook written for this demo; NMC²'s teams would add their own.

## Symptoms
- A job exits with code 1 and its log ends in `torch.OutOfMemoryError: CUDA out of memory`.
- `list_gpus` shows memory almost full (above 95%) while the failed job itself held little.

## Diagnose
1. Read the failed job's log: note how much it tried to allocate and how much was free.
2. List the jobs on the node. Find the job holding the memory: usually one long-running job started before the failure.
3. Read that job's log. A job that allocates memory and then sits idle (0% utilization) is hoarding it.
4. Check the GPU itself is healthy (no ECC errors, no thermal throttling). If it is, this is a scheduling problem, not hardware: don't cordon.

## Mitigate now (needs a human approval)
- Stop the job hoarding the memory (`stop_job`), with the evidence in the reason.
- Do not restart the failed job automatically: the customer decides when to rerun it.

## Prevent it happening again
- The node starts every job with access to all of the GPU's memory (`docker run --gpus all` in `node_agent.py`), so one job can starve the rest.
- Fix in the node agent: before starting a job, check free GPU memory and refuse the job if there isn't enough
  (an admission check next to the existing `check_capacity`), and/or give each job a memory share
  (for PyTorch jobs, `torch.cuda.set_per_process_memory_fraction`).
- Propose the change as a diff for the platform team to review. Never change a customer's job code.
