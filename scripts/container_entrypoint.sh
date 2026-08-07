#!/bin/sh
set -eu

runtime_secret_dir="/run/contador-secrets"
umask 077

write_base64_secret() {
    encoded_value="$1"
    target="$2"
    temporary="${target}.tmp.$$"
    mkdir -p "$runtime_secret_dir"
    if ! printf '%s' "$encoded_value" | base64 -d >"$temporary"; then
        rm -f "$temporary"
        echo "No se pudo decodificar un secreto de ejecución" >&2
        exit 1
    fi
    chmod 0600 "$temporary"
    mv "$temporary" "$target"
}

certificate_b64="${ARCA_CERTIFICATE_B64:-}"
private_key_b64="${ARCA_PRIVATE_KEY_B64:-}"
if [ -n "$certificate_b64" ] || [ -n "$private_key_b64" ]; then
    if [ -z "$certificate_b64" ] || [ -z "$private_key_b64" ]; then
        echo "ARCA_CERTIFICATE_B64 y ARCA_PRIVATE_KEY_B64 deben configurarse juntos" >&2
        exit 1
    fi
    write_base64_secret "$certificate_b64" "$runtime_secret_dir/arca.crt"
    write_base64_secret "$private_key_b64" "$runtime_secret_dir/arca.key"
    export ARCA_CERTIFICATE_PATH="$runtime_secret_dir/arca.crt"
    export ARCA_PRIVATE_KEY_PATH="$runtime_secret_dir/arca.key"
fi

if [ -n "${SESSION_ENCRYPTION_KEY:-}" ]; then
    mkdir -p "$runtime_secret_dir"
    printf '%s' "$SESSION_ENCRYPTION_KEY" >"$runtime_secret_dir/session_encryption.key"
    chmod 0600 "$runtime_secret_dir/session_encryption.key"
    export SESSION_ENCRYPTION_KEY_FILE="$runtime_secret_dir/session_encryption.key"
fi

unset ARCA_CERTIFICATE_B64 ARCA_PRIVATE_KEY_B64 SESSION_ENCRYPTION_KEY
exec "$@"
