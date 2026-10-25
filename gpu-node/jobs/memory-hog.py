"""Grabs 90% of the GPU's free memory and holds it for 30 minutes: any other job that needs memory will fail."""
import time
import torch

free, total = torch.cuda.mem_get_info()
held = torch.empty(int(free * 0.9), dtype=torch.uint8, device="cuda")
print(f"holding {held.numel() / 2**30:.1f} GiB of {total / 2**30:.1f} GiB", flush=True)
time.sleep(30 * 60)
