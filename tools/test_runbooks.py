"""Offline check that runbook search finds the right runbook.  Run: uv run python test_runbooks.py"""
from runbooks_server import RUNBOOKS, search

assert len(RUNBOOKS) >= 4
assert search("torch.OutOfMemoryError: CUDA out of memory")[0] == "gpu-out-of-memory"
assert search("GPU temperature hot throttling")[0] == "gpu-overheating"
assert search("zzz qqq") == []
print("runbook checks passed")
