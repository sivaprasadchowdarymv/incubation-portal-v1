# Hugging Face Spaces image: ONE container with Ollama (local AI) + the web portal + the email worker + backups.
# Hugging Face builds this file automatically from the Space repository (it must be called "Dockerfile").
# For a PC or your own server use `docker compose` instead (docker-compose.yml builds docker/app.Dockerfile).
#
# Build arguments (Space "Variables" with the same name are passed in at build time):
#   CHAT_MODEL   default qwen2.5:1.5b  - small enough for the free 2-CPU hardware; use mistral on bigger hardware
#   EMBED_MODEL  default nomic-embed-text
#   OLLAMA_IMAGE default ollama/ollama:latest - pin a version (e.g. ollama/ollama:0.9.6) once you have a working build
ARG OLLAMA_IMAGE=ollama/ollama:latest
FROM ${OLLAMA_IMAGE}

ARG CHAT_MODEL=qwen2.5:1.5b
ARG EMBED_MODEL=nomic-embed-text

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Python for the portal (the Ollama image is Ubuntu-based). tzdata = Asia/Kolkata deadline handling.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# Hugging Face runs containers as user id 1000. Ubuntu images already have a "ubuntu" user with that id: replace it.
RUN (userdel -r ubuntu 2>/dev/null || true) \
 && useradd -m -u 1000 user \
 && mkdir -p /opt/ollama/models \
 && chown -R user:user /opt/ollama

USER 1000
ENV HOME=/home/user \
    VIRTUAL_ENV=/home/user/venv \
    PATH=/home/user/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    OLLAMA_MODELS=/opt/ollama/models \
    OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_BASE_URL=http://127.0.0.1:11434 \
    OLLAMA_CHAT_MODEL=${CHAT_MODEL} \
    OLLAMA_EMBED_MODEL=${EMBED_MODEL}
WORKDIR /home/user/app

# Python packages (separate layer so code changes rebuild quickly)
RUN python3 -m venv "$VIRTUAL_ENV"
COPY --chown=user requirements.txt .
RUN pip install -r requirements.txt

# Download the AI models during the build, so the Space starts without a multi-GB download.
RUN ollama serve > /tmp/ollama-build.log 2>&1 & \
    for _ in $(seq 1 60); do ollama list > /dev/null 2>&1 && break; sleep 1; done; \
    ollama pull "${CHAT_MODEL}" && ollama pull "${EMBED_MODEL}"

# Application code (secrets, local data and caches are excluded by .dockerignore)
COPY --chown=user . .

# 7860 = the port Hugging Face exposes. Streamlit's XSRF check must be off inside the Hugging Face page frame,
# otherwise file uploads fail with "403" (documented Spaces limitation).
ENV PORT=7860 \
    RUN_OLLAMA=1 \
    STREAMLIT_DISABLE_XSRF=1 \
    PERSIST_DIR=/data
EXPOSE 7860

ENTRYPOINT []
CMD ["bash", "docker/hf_start.sh"]
