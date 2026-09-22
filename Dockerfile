FROM ghcr.io/astral-sh/uv:0.8.22 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY docs/runbooks ./docs/runbooks
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home app
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["uvicorn", "incident_platform.gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
