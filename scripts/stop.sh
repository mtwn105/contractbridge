#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
compose_args=(-f docker-compose.yml)
if [ "${1:-}" = '--example' ]; then
  [ "${2:-}" = 'shopping' ] || { echo 'Supported example: shopping' >&2; exit 2; }
  compose_args+=(-f docker-compose.example.yml)
elif [ -n "${1:-}" ]; then
  echo 'Usage: scripts/stop.sh [--example shopping]' >&2
  exit 2
fi
while IFS= read -r container; do
  [ -n "$container" ] && docker rm -f "$container"
done < <(docker ps -aq --filter label=contractbridge.role=approved-proxy)
docker compose "${compose_args[@]}" down
