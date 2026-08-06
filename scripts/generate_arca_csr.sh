#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Uso: $0 <CUIT-11-digitos> <alias-sin-espacios>" >&2
  exit 2
fi

operator_cuit="$1"
certificate_alias="$2"

if [[ ! "$operator_cuit" =~ ^[0-9]{11}$ ]]; then
  echo "El CUIT debe contener exactamente 11 dígitos." >&2
  exit 2
fi

if [[ ! "$certificate_alias" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "El alias sólo puede contener letras, números, punto, guion y guion bajo." >&2
  exit 2
fi

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
secret_dir="$project_dir/secrets"
private_key="$secret_dir/$certificate_alias.key"
csr_file="$secret_dir/$certificate_alias.csr"

mkdir -p "$secret_dir"
chmod 700 "$secret_dir"

if [[ -e "$private_key" || -e "$csr_file" ]]; then
  echo "Ya existe una clave o CSR para ese alias; no se sobrescribió nada." >&2
  exit 1
fi

openssl genrsa -out "$private_key" 2048
chmod 600 "$private_key"
openssl req \
  -new \
  -key "$private_key" \
  -subj "/C=AR/O=Contador-AI/serialNumber=CUIT $operator_cuit/CN=$certificate_alias" \
  -out "$csr_file"
chmod 600 "$csr_file"

echo "Clave privada: $private_key"
echo "CSR para cargar en ARCA/WSASS: $csr_file"
echo "La clave privada no debe cargarse ni enviarse a ARCA."
