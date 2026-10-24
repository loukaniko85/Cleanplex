# Build the React UI first, then ship a small Python runtime that serves both.
FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Vite writes to ../cleanplex/web/static, which is what the backend serves.
RUN npm run build


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLEANPLEX_DATA=/data

# FFmpeg extracts scan frames; the GL libs are for NudeNet's ONNX runtime and
# OpenCV dependencies. No desktop stack is needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY cleanplex ./cleanplex
COPY --from=frontend /app/cleanplex/web/static ./cleanplex/web/static

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 cleanplex \
    && mkdir -p /data \
    && chown -R cleanplex:cleanplex /data /app

USER cleanplex
VOLUME ["/data"]
EXPOSE 7979

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7979/api/health', timeout=3)"

CMD ["cleanplex"]
