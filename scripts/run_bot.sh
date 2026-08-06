#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "Falta .env. Copiá config.example.env y completá el token y chat_id." >&2
  exit 1
fi

set -a
source .env
set +a

exec python3 -m contador_ai "$@"
