# ARCA: certificados, relaciones y credenciales

## Estado actual

La instalación personal usa producción y tiene:

- certificado X.509 vigente hasta el 04/08/2028;
- clave privada correspondiente;
- relación al servicio de Facturación Electrónica `wsfe`;
- punto de venta Webservices `00002`;
- WSAA con tickets cifrados y reutilizables;
- Factura C real mediante WSFEv1;
- consulta de situación registral, DFE y comprobantes autorizados;
- alta privada de CUIT y clave fiscal para los portales sin API pública.

Homologación ya cumplió su función. Se eliminaron del directorio operativo su
certificado, clave y CSR. También se eliminó el CSR de producción: una solicitud ya
emitida no es necesaria para ejecutar ni renovar el servicio.

## Archivos locales necesarios

`secrets/` contiene únicamente:

| Archivo | Función | Permiso |
|---|---|---:|
| `simplearcaprod_7d17b50e91a73602.crt` | Certificado público de producción | `0644` |
| `simplearcaprod.key` | Clave privada de producción | `0600` |
| `session_encryption.key` | Clave Fernet para credenciales y tickets | `0600` |

Los tres están ignorados por Git. La pérdida de la clave Fernet impide descifrar las
credenciales y tickets persistidos; debe existir una copia de seguridad fuera del
repositorio.

## Dos mecanismos diferentes

### APIs oficiales

WSAA autentica al certificado y entrega un Ticket de Acceso para cada servicio. La
clave fiscal personal no participa en WSFE, Padrón ni DFE.

Flujo:

1. firmar un LoginTicketRequest con la clave privada;
2. solicitar token y sign a WSAA;
3. cifrar el ticket en SQLite;
4. usarlo hasta antes de su vencimiento;
5. renovarlo automáticamente.

### Portales interactivos

Monotributo, pagos e Ingresos Brutos requieren CUIT y clave fiscal porque no exponen
una API pública equivalente. Desde `/ayuda` se genera una URL privada. El formulario
valida los datos contra ARCA y guarda la clave cifrada en `portal_state`.

La URL no contiene la clave. En producción usa HTTP dentro de Tailscale; Telegram
transporta únicamente el enlace de un solo uso.

## Configuración local

La `.env` local usa archivos montados como sólo lectura:

```env
ARCA_ENVIRONMENT=production
ARCA_CERTIFICATE_PATH=/run/local-secrets/simplearcaprod_7d17b50e91a73602.crt
ARCA_PRIVATE_KEY_PATH=/run/local-secrets/simplearcaprod.key
SESSION_ENCRYPTION_KEY_FILE=/run/local-secrets/session_encryption.key
ARCA_WSFE_POINT_OF_SALE=2
```

Levantar el bot y el worker con el override local:

```bash
docker compose -f compose.yaml -f compose.local.yaml up --build -d bot
docker compose -f compose.yaml -f compose.local.yaml --profile portal up --build -d portal-worker
```

## Configuración en Coolify

Coolify no monta `secrets/` ni ningún archivo del checkout. Cargar como variables
secretas:

- `ARCA_CERTIFICATE_B64`
- `ARCA_PRIVATE_KEY_B64`
- `SESSION_ENCRYPTION_KEY`

El entrypoint las materializa en un `tmpfs` con modo `0600` y las elimina del entorno
antes de iniciar Python. El procedimiento completo está en
[DEPLOY_COOLIFY.md](DEPLOY_COOLIFY.md).

## Relaciones que deben conservarse

En Administrador de Relaciones de Clave Fiscal, el certificado/DN de producción debe
estar autorizado para el CUIT representado y cada Web Service utilizado. Como mínimo,
la emisión requiere `wsfe`.

Las relaciones de servicios interactivos y las relaciones de Web Services de negocio
no son intercambiables. Crear un punto de venta no crea automáticamente la relación al
certificado, y asociar `wsfe` no crea el punto de venta.

## Verificaciones seguras

Comprobar que certificado y clave forman el mismo par sin mostrar material privado:

```bash
openssl x509 -in secrets/simplearcaprod_7d17b50e91a73602.crt -pubkey -noout \
  | openssl pkey -pubin -outform DER | openssl dgst -sha256
openssl pkey -in secrets/simplearcaprod.key -pubout -outform DER \
  | openssl dgst -sha256
```

Los hashes deben coincidir. Verificar vigencia:

```bash
openssl x509 -in secrets/simplearcaprod_7d17b50e91a73602.crt -noout -dates
```

No imprimir la clave, no adjuntarla a tickets y no copiar token/sign de WSAA.

## Renovación

Antes del vencimiento:

1. generar una clave y CSR nuevos fuera del repositorio;
2. emitir un certificado nuevo en ARCA;
3. autorizar el nuevo certificado para los mismos servicios;
4. validar en homologación sólo si cambió el flujo, no reutilizando datos de producción;
5. cargar certificado y clave nuevos en Coolify;
6. desplegar y comprobar WSAA/WSFE;
7. conservar temporalmente el par anterior para rollback;
8. eliminarlo cuando la nueva versión quede verificada.

Nunca reemplazar sólo el certificado o sólo la clave: deben cambiar como par.

## Generar un nuevo par cuando sea necesario

El script permanece disponible para renovaciones:

```bash
./scripts/generate_arca_csr.sh nuevo_alias "CN=nuevo_alias,serialNumber=CUIT CUIT_SIN_GUIONES"
```

Genera `.key` y `.csr` bajo `secrets/`. Después de emitir y validar el certificado, el
CSR puede eliminarse. No ejecutar este paso para un redeploy normal.
