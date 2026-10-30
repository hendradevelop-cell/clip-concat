# Worker membungkus transpor MCP stdio: concat binary harus ada di image ini
# (VF_CONCAT_BIN) atau jalankan worker langsung di host yang punya Concat.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY videoflow ./videoflow

RUN pip install --no-cache-dir ".[captions]" \
    && useradd -m -u 1000 app \
    && mkdir -p /data && chown -R app /data /app

# Bobot Whisper di-cache di /data supaya tidak diunduh ulang tiap container baru.
ENV HF_HOME=/data/.cache/huggingface
USER app

CMD ["celery", "-A", "videoflow.worker:celery_app", "worker", \
     "-Q", "videoflow", "--concurrency=1", "-l", "info"]
