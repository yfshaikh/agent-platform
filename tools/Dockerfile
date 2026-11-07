# One image (platform-tools:dev) for all tool servers; each Deployment in platform.yaml runs a different *_server.py.
# FROM: the base image to start from (a small Debian with Python 3.12). Every line below adds a layer on top.
FROM python:3.12-slim
# Copy the uv binary out of uv's own image (a multi-image COPY), to install dependencies fast.
COPY --from=ghcr.io/astral-sh/uv:0.7.6 /uv /usr/local/bin/uv
# later paths are relative to /app (created if missing)
WORKDIR /app
# Dependencies first, code later: Docker caches each layer, so editing code doesn't reinstall every package.
COPY pyproject.toml uv.lock ./
# --frozen: exactly the versions in uv.lock
RUN uv sync --frozen --no-install-project --no-dev
# The virtualenv uv made goes first on PATH; unbuffered output so `kubectl logs` shows lines immediately.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY *.py ./
# the runbook Markdown files are baked into the image
COPY runbooks ./runbooks
# Run as an unprivileged user, not root. The user must exist (kopf and others look up the name), hence useradd.
RUN useradd --uid 10001 --no-create-home app
USER 10001
# ENTRYPOINT is always run; CMD is its default argument. A Deployment's `args:` replaces CMD, which is how one
# image becomes four different tool servers (args: [serpapi_server.py], ...).
ENTRYPOINT ["python"]
CMD ["nessie_server.py"]
