# Build from the repository root with: docker build -f cloud/Dockerfile .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN UV_NO_CACHE=1 uv sync --locked --only-group cloud --no-install-project
COPY cloud/dlx_cloud dlx_cloud
COPY src/dlx_tray/__init__.py dlx_tray/
COPY src/dlx_tray/contracts dlx_tray/contracts
COPY src/dlx_tray/core/__init__.py src/dlx_tray/core/models.py src/dlx_tray/core/chunker.py src/dlx_tray/core/profiles.py dlx_tray/core/
ENV PYTHONUNBUFFERED=1
USER 65532:65532
CMD ["sh", "-c", "exec .venv/bin/uvicorn dlx_cloud.app:app --no-access-log --host 0.0.0.0 --port \"${PORT:-8080}\""]
