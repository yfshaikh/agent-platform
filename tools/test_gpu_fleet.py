"""Offline check of how GPU readings become plain-language problems.  Run: uv run python test_gpu_fleet.py"""
import os

os.environ.setdefault("NODE_TOKEN", "test")
from gpu_fleet_server import SOURCE_FILE, health, job_path

GOOD = {"temperature.gpu": 61, "memory.used": 1200, "memory.total": 15360, "clocks_throttle_reasons.active": 0x1,
        "ecc.errors.uncorrected.volatile.total": 0}
assert health(GOOD) == [], health(GOOD)  # 0x1 = idle, not a problem
assert health({**GOOD, "clocks_throttle_reasons.active": 0x4}) == []  # at the power limit under load: normal
bad = health({**GOOD, "temperature.gpu": 88, "memory.used": 15000, "clocks_throttle_reasons.active": 0x24,
              "ecc.errors.uncorrected.volatile.total": 2})
assert len(bad) == 4 and "hot: 88°C" in bad and any("thermal" in problem for problem in bad), bad
assert health({**GOOD, "temperature.gpu": "[N/A]", "ecc.errors.uncorrected.volatile.total": "[N/A]"}) == []  # T4s report N/A
assert job_path("job-train-1a2b3c", "stop") == "/jobs/job-train-1a2b3c/stop"
for sneaky in ("../../cordon#", "job-train-1a2b3c/../../cordon", "job-train-1a2b3c?x=1"):
    try:
        job_path(sneaky, "stop")
        raise AssertionError(f"{sneaky} got through")
    except ValueError:
        pass
assert SOURCE_FILE.fullmatch("node_agent.py") and SOURCE_FILE.fullmatch("jobs/memory-hog.py")
for sneaky in ("../.env", "jobs/../node_agent.py", "jobs/../../.ssh/id_rsa.py", ".env", "jobs/train.py/../../x.py", "/etc/passwd"):
    assert not SOURCE_FILE.fullmatch(sneaky), sneaky
print("gpu fleet checks passed")
