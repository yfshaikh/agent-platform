# The operator's image (agent-operator:dev), deployed by deploy/platform/platform.yaml.
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
# Run as an unprivileged user, not root. The user must exist (kopf and others look up the name), hence useradd.
RUN useradd --uid 10001 --no-create-home app
USER 10001

# What the container runs: kopf runs the operator; --standalone = no coordination between copies, --all-namespaces
# = watch Agents everywhere.
CMD ["kopf", "run", "agent_operator.py", "--standalone", "--all-namespaces"]
