# Built from the repo root (it needs console/ too):  docker build -t platform-api:dev -f api/Dockerfile .
# A multi-stage build. Stage 1 ("console"): build the React console with Node. Only its output is kept.
FROM node:22-slim AS console
WORKDIR /console
COPY console/package.json console/package-lock.json ./
RUN npm ci --silent
COPY console/ ./
# -> /console/dist (plain HTML/JS/CSS)
RUN npm run build

# Stage 2: the actual image. Node isn't in it at all, which keeps it small.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.7.6 /uv /usr/local/bin/uv
WORKDIR /app
COPY api/pyproject.toml api/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
# The virtualenv uv made goes first on PATH; unbuffered output so `kubectl logs` shows lines immediately.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY api/*.py ./
# the same runbooks the tool serves, for the console's Runbooks page
COPY tools/runbooks ./runbooks
# take only the built console from stage 1; the API serves it at /
COPY --from=console /console/dist ./static
# Run as an unprivileged user, not root.
RUN useradd --uid 10001 --no-create-home app
USER 10001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
