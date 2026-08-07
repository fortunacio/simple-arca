#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
production_json=$(mktemp)
local_json=$(mktemp)
winston_json=$(mktemp)
local_env_file="$project_dir/.env"
if [ ! -f "$local_env_file" ]; then
    local_env_file="$project_dir/config.example.env"
fi
cleanup() {
    rm -f "$production_json" "$local_json" "$winston_json"
}
trap cleanup EXIT INT TERM

cd "$project_dir"

docker compose --env-file /dev/null -f compose.yaml config --format json >"$production_json"
docker compose --env-file "$local_env_file" -f compose.yaml -f compose.local.yaml \
    --profile portal config --format json >"$local_json"
docker compose --env-file config.coolify-winston.env.example -f compose.yaml \
    config --format json >"$winston_json"

python3 - "$production_json" "$local_json" "$winston_json" "$project_dir" <<'PY'
import ipaddress
import json
import sys
from pathlib import Path

production = json.loads(Path(sys.argv[1]).read_text())
local = json.loads(Path(sys.argv[2]).read_text())
winston = json.loads(Path(sys.argv[3]).read_text())
project = Path(sys.argv[4]).resolve()
errors = []

services = production.get("services", {})
if set(services) != {"bot", "portal-worker"}:
    errors.append("producción debe renderizar bot y portal-worker")

custom_networks = set(production.get("networks", {})) - {"default"}
if custom_networks:
    errors.append(f"hay redes personalizadas: {sorted(custom_networks)}")

for name, service in services.items():
    if service.get("restart") != "unless-stopped":
        errors.append(f"{name}: restart no es unless-stopped")
    if not service.get("healthcheck"):
        errors.append(f"{name}: falta healthcheck")
    if service.get("container_name"):
        errors.append(f"{name}: no debe usar container_name")
    build_args = (service.get("build") or {}).get("args") or {}
    for key in build_args:
        if any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "KEY", "CERT")):
            errors.append(f"{name}: secreto presente en build args: {key}")
    for volume in service.get("volumes", []):
        target = volume.get("target", "")
        if not target.startswith("/"):
            errors.append(f"{name}: destino de volumen no absoluto: {target}")
        if volume.get("type") == "bind":
            source = volume.get("source", "")
            if not source.startswith("/"):
                errors.append(f"{name}: bind de producción no absoluto: {source}")
            if "${" in source or source.startswith("/data/coolify/") or source.startswith(str(project)):
                errors.append(f"{name}: bind de producción no portable: {source}")
    for port in service.get("ports", []):
        host = port.get("host_ip", "")
        safe = host in {"127.0.0.1", "::1"}
        if not safe:
            try:
                safe = ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10")
            except ValueError:
                safe = False
        if not safe:
            errors.append(f"{name}: puerto publicado fuera de loopback/Tailscale: {host}")

for name, service in local.get("services", {}).items():
    for volume in service.get("volumes", []):
        if volume.get("type") != "bind":
            continue
        source = Path(volume.get("source", "")).resolve()
        if source != project / "secrets" or not volume.get("read_only"):
            errors.append(f"{name}: el bind local debe ser secrets/ en sólo lectura")

winston_ports = {
    (str(port.get("host_ip", "")), int(port.get("published", 0)), int(port.get("target", 0)))
    for port in winston.get("services", {}).get("portal-worker", {}).get("ports", [])
}
expected_winston_ports = {
    ("100.75.19.73", 8091, 8090),
    ("100.75.19.73", 6080, 6080),
}
if winston_ports != expected_winston_ports:
    errors.append(f"winston: puertos renderizados inesperados: {sorted(winston_ports)}")

for dockerfile in (project / "Dockerfile", project / "Dockerfile.portal"):
    users = [
        line.split(maxsplit=1)[1].strip()
        for line in dockerfile.read_text().splitlines()
        if line.strip().upper().startswith("USER ")
    ]
    if not users or users[-1] != "appuser":
        errors.append(f"{dockerfile.name}: la imagen final no usa appuser")

dockerignore = {
    line.strip()
    for line in (project / ".dockerignore").read_text().splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}
for required in {".env", "secrets", "*.key", "*.crt", "*.csr"}:
    if required not in dockerignore:
        errors.append(f".dockerignore no excluye {required}")

if errors:
    print("Preflight fallido:")
    for error in errors:
        print(f"- {error}")
    raise SystemExit(1)

print("Compose de producción y local: OK")
print("Render de winston sobre Tailscale: OK")
print("Persistencia, secretos, healthchecks, red y usuario final: OK")
PY

docker compose --env-file /dev/null -f compose.yaml build bot portal-worker
docker compose --env-file /dev/null -f compose.yaml run --rm --no-deps bot \
    python -c "from pathlib import Path; p=Path('/data/.write-test'); p.write_text('ok'); p.unlink()"
docker compose --env-file /dev/null -f compose.yaml run --rm --no-deps portal-worker \
    python -c "from pathlib import Path; p=Path('/portal-state/.write-test'); p.write_text('ok'); p.unlink()"

echo "Escritura con el UID final en tax_data y portal_state: OK"
