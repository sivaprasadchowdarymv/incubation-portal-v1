#!/usr/bin/env bash
# Start-up for the single-container image (Hugging Face Spaces, or `docker run` on any machine).
#  1. restore saved data from the persistent folder ($PERSIST_DIR, a Storage Bucket mounted at /data)
#  2. start Ollama and make sure the models exist
#  3. prepare the database / first admin / search index
#  4. run the email worker + periodic backups in the background and the web portal in the foreground
#  5. on stop: final backup, then exit
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
log() { echo "[start] $*"; }

# ---- 1. where data lives -------------------------------------------------------------------------
# Live data on the container's fast local disk (SQLite must not run on a network mount);
# a safe copy goes to $PERSIST_DIR. These paths are fixed by the container, whatever .env says.
export INCUB_STORAGE_DIR="$PWD/storage"
export INCUB_DB_PATH="$INCUB_STORAGE_DIR/incubation.db"
export INCUB_INDEX_DIR="$INCUB_STORAGE_DIR/index"
export INCUB_KB_DIR="$PWD/kb"
mkdir -p "$INCUB_STORAGE_DIR"

PERSIST_DIR="${PERSIST_DIR:-/data}"
if mkdir -p "$PERSIST_DIR/incubation_backup" 2>/dev/null && touch "$PERSIST_DIR/incubation_backup/.write-test" 2>/dev/null; then
  rm -f "$PERSIST_DIR/incubation_backup/.write-test"
  export INCUB_BACKUP_DIR="$PERSIST_DIR/incubation_backup"
  log "persistent storage: $INCUB_BACKUP_DIR"
  python -m persist.snapshot restore || log "WARNING: restore failed (starting with an empty database)"
else
  unset INCUB_BACKUP_DIR
  log "WARNING: no writable persistent storage at $PERSIST_DIR."
  log "WARNING: all registrations and submissions will be LOST when this container restarts."
  log "WARNING: on Hugging Face, attach a Storage Bucket to the Space at /data (see DEPLOYMENT.md)."
fi

# Hugging Face secrets -> the names the app uses
export INCUB_ADMIN_PASSWORD="${INCUB_ADMIN_PASSWORD:-${ADMIN_PASSWORD:-}}"
if [ -z "${INCUB_PUBLIC_URL:-}" ] && [ -n "${SPACE_HOST:-}" ]; then
  export INCUB_PUBLIC_URL="https://${SPACE_HOST}"     # Hugging Face sets SPACE_HOST (e.g. user-space.hf.space)
fi

# ---- 2. local AI ---------------------------------------------------------------------------------
OLLAMA_PID=""
if [ "${RUN_OLLAMA:-1}" = "1" ]; then
  ollama serve > /tmp/ollama.log 2>&1 &
  OLLAMA_PID=$!
  for _ in $(seq 1 60); do ollama list > /dev/null 2>&1 && break; sleep 1; done
  for m in "${OLLAMA_CHAT_MODEL:-qwen2.5:1.5b}" "${OLLAMA_EMBED_MODEL:-nomic-embed-text}"; do
    if ! ollama show "$m" > /dev/null 2>&1; then
      log "model $m is not in the image; downloading it now (first start only takes longer)"
      ollama pull "$m" || log "WARNING: could not download $m; the policy assistant will show passages without AI answers"
    fi
  done
  export OLLAMA_BASE_URL="http://127.0.0.1:11434"
fi

# ---- 3. database, first admin, search index --------------------------------------------------------
if ! python scripts/docker_bootstrap.py --role app; then
  log "ERROR: start-up preparation failed (see the message above). Fix it in the Space settings and restart."
  sleep 3600   # keep the container up so the log stays readable instead of a restart loop
  exit 1
fi

# ---- 4. background services + web portal -----------------------------------------------------------
python scripts/notify_worker.py --loop "${NOTIFY_INTERVAL:-300}" &
WORKER_PID=$!
BACKUP_PID=""
if [ -n "${INCUB_BACKUP_DIR:-}" ]; then
  python -m persist.snapshot backup > /dev/null || true
  python -m persist.snapshot loop "${BACKUP_INTERVAL:-120}" &
  BACKUP_PID=$!
fi

XSRF=true
[ "${STREAMLIT_DISABLE_XSRF:-0}" = "1" ] && XSRF=false
streamlit run app.py \
  --server.address 0.0.0.0 \
  --server.port "${PORT:-7860}" \
  --server.headless true \
  --server.enableXsrfProtection "$XSRF" \
  --browser.gatherUsageStats false &
APP_PID=$!

# ---- 5. clean shutdown -----------------------------------------------------------------------------
shutdown() {
  trap - TERM INT
  log "stopping: saving a final copy of the data"
  kill "$APP_PID" "$WORKER_PID" ${BACKUP_PID:+"$BACKUP_PID"} 2> /dev/null
  wait "$APP_PID" 2> /dev/null
  if [ -n "${INCUB_BACKUP_DIR:-}" ]; then
    python -m persist.snapshot backup || log "WARNING: final backup failed"
  fi
  [ -n "$OLLAMA_PID" ] && kill "$OLLAMA_PID" 2> /dev/null
  exit 0
}
trap shutdown TERM INT
log "portal starting on port ${PORT:-7860}"
wait "$APP_PID"
shutdown
