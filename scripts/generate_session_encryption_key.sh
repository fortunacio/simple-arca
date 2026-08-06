#!/usr/bin/env bash
set -euo pipefail

destination="${1:-secrets/session_encryption.key}"

if [[ -e "$destination" ]]; then
  echo "La clave ya existe: $destination" >&2
  exit 1
fi

mkdir -p "$(dirname "$destination")"
umask 077
openssl rand 32 | openssl base64 -A | tr '+/' '-_' > "$destination"
printf '\n' >> "$destination"
chmod 600 "$destination"
echo "Clave de cifrado creada: $destination"
