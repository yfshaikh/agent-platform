"""Matrix multiplication at full load for 10 minutes, like heavy training: pushes utilization, power and temperature up.
No overclocking or power-limit changes: the GPU's own protection throttles it if it gets hot."""
import time
import torch

a = torch.randn(8192, 8192, device="cuda", dtype=torch.float16)
b = torch.randn(8192, 8192, device="cuda", dtype=torch.float16)
started = last = time.time()
count = 0
while time.time() - started < 10 * 60:
    a @ b
    count += 1
    if time.time() - last > 10:
        torch.cuda.synchronize()
        print(f"{count * 2 * 8192**3 / (time.time() - last) / 1e12:.1f} TFLOPS", flush=True)
        count, last = 0, time.time()
