"""Trains for a minute, then asks for more memory than the GPU has: a real CUDA out-of-memory crash (exit code 1)."""
import time
import torch

model = torch.nn.Linear(4096, 4096).cuda()
started = time.time()
while time.time() - started < 60:
    loss = model(torch.randn(256, 4096, device="cuda")).square().mean()
    loss.backward()
print("loading the next batch (it's far too big)...", flush=True)
_, total = torch.cuda.mem_get_info()
batch = torch.empty(total * 2, dtype=torch.uint8, device="cuda")  # torch.OutOfMemoryError
