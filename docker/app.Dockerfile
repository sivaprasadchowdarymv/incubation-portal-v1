# PC / server image (docker compose). Ollama runs as a separate container. Hugging Face uses the top-level Dockerfile instead.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Python packages first (cached layer). tzdata = time-zone data for the deadline (Asia/Kolkata).
COPY requirements.txt .
RUN pip install -r requirements.txt tzdata

# Application code. Secrets (.env), local data (storage/) and caches are excluded by .dockerignore.
COPY . .

# Run as an unprivileged user; storage/ and kb/ become persistent volumes owned by that user.
RUN useradd --create-home --uid 1000 app \
 && mkdir -p /app/storage \
 && chmod +x /app/docker/entrypoint.sh \
 && chown -R app:app /app
USER 1000

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["app"]
