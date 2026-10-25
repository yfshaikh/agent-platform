"""A small training run (a convolutional network on random images): steady, healthy GPU load for 15 minutes."""
import time
import torch

model = torch.nn.Sequential(torch.nn.Conv2d(3, 64, 3), torch.nn.ReLU(), torch.nn.Conv2d(64, 64, 3), torch.nn.ReLU(),
                            torch.nn.AdaptiveAvgPool2d(1), torch.nn.Flatten(), torch.nn.Linear(64, 10)).cuda()
optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
started = time.time()
step = 0
while time.time() - started < 15 * 60:
    images, labels = torch.randn(128, 3, 64, 64, device="cuda"), torch.randint(0, 10, (128,), device="cuda")
    loss = torch.nn.functional.cross_entropy(model(images), labels)
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    step += 1
    if step % 200 == 0:
        print(f"step {step} loss {loss.item():.3f}", flush=True)
print("training finished", flush=True)
