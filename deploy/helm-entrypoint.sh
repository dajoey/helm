#!/bin/sh
# Start one role of Helm (deploy/Dockerfile ENTRYPOINT).
#   HELM_ROLE=api  -> app.service:app  (the thread store API, bearer auth)
#   HELM_ROLE=web  -> app.web:app      (the screens; proxies /api/* with the token injected;
#                                       put an authenticating proxy in front of it)
# Any arguments run instead of the server, in the same image and environment, e.g.
#   docker run --rm -v helm-data:/data helm:local python3 -c 'print("hi")'
set -eu
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
role="${HELM_ROLE:-api}"
case "$role" in
  api) module="app.service:app" ;;
  web) module="app.web:app" ;;
  *) echo "helm-entrypoint: HELM_ROLE must be 'api' or 'web', got '$role'" >&2; exit 64 ;;
esac
exec python3 -m uvicorn "$module" --host "${HELM_HOST:-0.0.0.0}" --port "${HELM_PORT:-3038}"
