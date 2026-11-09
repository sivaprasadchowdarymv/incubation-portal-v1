#!/bin/sh
# Container start-up: prepare database/index, then run the requested service.
#   app     -> web portal (Streamlit) on port 8501
#   worker  -> email reminders + delivery/retries
#   anything else is executed as a command (e.g. "python -m pytest")
set -e
cd "$(dirname "$0")/.."

case "${1:-app}" in
  app)
    python scripts/docker_bootstrap.py --role app
    exec streamlit run app.py --server.address 0.0.0.0 --server.port "${PORT:-8501}" --server.headless true
    ;;
  worker)
    python scripts/docker_bootstrap.py --role worker
    exec python scripts/notify_worker.py --loop "${NOTIFY_INTERVAL:-300}"
    ;;
  *)
    exec "$@"
    ;;
esac
