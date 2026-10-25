#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
compose_args=(-f docker-compose.yml)
if [ "${1:-}" = '--example' ]; then
  [ "${2:-}" = 'shopping' ] || { echo 'Supported example: shopping' >&2; exit 2; }
  compose_args+=(-f docker-compose.example.yml)
fi
python3 scripts/prepare_env.py
if [ "${1:-}" = '--example' ]; then python3 examples/shopping/prepare.py; fi
if ! docker info >/dev/null 2>&1; then
  echo 'Start Docker Engine/Desktop, then rerun bootstrap.' >&2
  exit 1
fi
docker compose "${compose_args[@]}" --profile tools build
docker compose "${compose_args[@]}" up -d
for attempt in $(seq 1 30); do
  if docker compose "${compose_args[@]}" exec -T ollama ollama list >/dev/null 2>&1; then break; fi
  sleep 1
done
configured_model="$(docker compose "${compose_args[@]}" exec -T control-plane python -c "import os; print(os.environ['OLLAMA_MODEL'])")"
configured_endpoint="$(docker compose "${compose_args[@]}" exec -T control-plane python -c "import os; print(os.environ['OLLAMA_URL'])")"
if [ "$configured_endpoint" = 'http://ollama:11434' ]; then docker compose "${compose_args[@]}" exec -T ollama ollama pull "$configured_model"; fi
echo 'Ready: http://localhost:3000 — operator token is in ignored .env.'
