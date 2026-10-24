FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.7.6 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY bot.py ./
RUN useradd --uid 10001 --no-create-home app
USER 10001
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "7860"]
