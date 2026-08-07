# Despliegue en Coolify sobre Tailscale

## Resultado esperado

Coolify construye y mantiene dos servicios desde [compose.yaml](compose.yaml):

- `bot`, sin puertos publicados;
- `portal-worker`, con los puertos `8090` y `6080` ligados únicamente a la IP de
  Tailscale de la VPS.

No agregar dominios públicos a ninguno de los dos servicios. El formulario de CUIT y
clave fiscal se abre mediante una URL HTTP del tailnet. Tailscale aporta el túnel
cifrado entre el dispositivo y la VPS.

## Requisitos previos

- Tailscale activo en la VPS y en el dispositivo que abrirá el enlace.
- Una IP de la VPS dentro de `100.64.0.0/10` o su nombre completo `*.ts.net`.
- Repositorio accesible desde Coolify.
- Certificado y clave de producción de ARCA que formen el mismo par.
- Clave Fernet estable para cifrar credenciales y tickets.

El certificado actual vence el 04/08/2028. Programar su renovación con anticipación.

## Compose en Coolify

Configurar [compose.yaml](compose.yaml) como único archivo Compose. No usar
[compose.local.yaml](compose.local.yaml): contiene un bind mount pensado sólo para el
Mac de desarrollo.

El Compose de producción:

- no usa rutas del checkout ni del equipo local;
- usa los volúmenes nombrados `tax_data` y `portal_state`;
- ejecuta la aplicación como UID/GID `10001` definidos en las imágenes;
- guarda secretos de ejecución en un `tmpfs` con modo `0700`;
- limita los logs de cada contenedor a tres archivos de 10 MB;
- incluye healthchecks para ambos servicios;
- no declara redes personalizadas ni nombres fijos de contenedor.

## Variables secretas

Marcar como secretas en Coolify:

| Variable | Contenido |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token vigente de BotFather |
| `SESSION_ENCRYPTION_KEY` | Contenido exacto de `session_encryption.key` |
| `ARCA_CERTIFICATE_B64` | Certificado de producción codificado en base64 |
| `ARCA_PRIVATE_KEY_B64` | Clave privada de producción codificada en base64 |

Para obtener los dos valores base64 localmente sin modificar los archivos:

```bash
base64 < secrets/simplearcaprod_7d17b50e91a73602.crt | tr -d '\n'
base64 < secrets/simplearcaprod.key | tr -d '\n'
```

No pegar esos valores en el repositorio, logs, Telegram ni documentación. Durante el
arranque se escriben en `/run/contador-secrets`, que es memoria temporal, con modo
`0600`. El entrypoint elimina las variables del entorno antes de ejecutar Python.

## Variables obligatorias

| Variable | Valor esperado |
|---|---|
| `BOT_ALLOWED_CHAT_IDS` | Chat personal autorizado |
| `INVOICE_EMISSION_CHAT_IDS` | Chat autorizado para emitir |
| `IIBB_PAYMENT_CHAT_IDS` | Chat autorizado para generar VEP provinciales |
| `CCMA_CHAT_IDS` | Chat que sincroniza portales |
| `ARCA_ENVIRONMENT` | `production` |
| `ARCA_OPERATOR_CUIT` | CUIT operador del certificado |
| `ARCA_CERTIFICATE_ALIAS` | Alias registrado en ARCA |
| `ARCA_WSFE_POINT_OF_SALE` | `2` |
| `INVOICE_ISSUER_IIBB` | Número de Ingresos Brutos del emisor |
| `TAILSCALE_BIND_IP` | IP `100.x` asignada a la VPS |
| `ARCA_ENROLLMENT_BASE_URL` | `http://IP_TAILSCALE:8090` o `http://host.tailnet.ts.net:8090` |
| `ARCA_ENROLLMENT_ALLOW_TAILSCALE_HTTP` | `true` |

La aplicación rechaza HTTP si la URL no es localhost, una IP del rango de Tailscale o
un nombre terminado en `.ts.net`. La bandera no habilita HTTP arbitrario.

## Variables operativas

Los valores por defecto son adecuados para la instalación personal:

```env
CCMA_ENABLED=true
CCMA_SYNC_INTERVAL_SECONDS=21600
CCMA_SNAPSHOT_TTL_SECONDS=86400
CCMA_BROWSER_HEADLESS=true
ARCA_SETI_ENABLED=true
ARCA_SETI_SNAPSHOT_TTL_SECONDS=86400
SANTA_FE_IIBB_ENABLED=true
SANTA_FE_IIBB_SNAPSHOT_TTL_SECONDS=86400
IIBB_PAYMENTS_ENABLED=true
IIBB_PAYMENT_CHAT_IDS=CHAT_PERSONAL
AUTOMATIC_MONITOR_ENABLED=true
AUTOMATIC_MONITOR_CHECK_SECONDS=300
AUTOMATIC_SYNC_INTERVAL_SECONDS=21600
ARCA_ENROLLMENT_ENABLED=true
ARCA_ENROLLMENT_HOST_PORT=8090
CCMA_NOVNC_HOST_PORT=6080
LOG_LEVEL=INFO
```

`CCMA_LOGIN_URL` puede apuntar a
`http://IP_TAILSCALE:6080/vnc.html?autoconnect=true&resize=scale` para diagnóstico
manual. No es necesario para el alta normal.

No definir `TAX_MONITOR_DB`, `CCMA_SESSION_DIR`, `ARCA_ENROLLMENT_BIND` ni
`ARCA_ENROLLMENT_PORT` en Coolify: el Compose fija sus rutas y puertos internos.

## Persistencia y copias de seguridad

`tax_data` contiene `/data/tax_monitor.sqlite3`: identidad, snapshots, facturas,
VEP provinciales, sus PDFs oficiales, auditoría, notificaciones y tickets cifrados.

`portal_state` contiene las credenciales cifradas y el estado renovable de los
navegadores. Para restaurar el acceso sin repetir el alta se necesitan conjuntamente:

1. ambos volúmenes;
2. el mismo `SESSION_ENCRYPTION_KEY`;
3. el certificado y la clave de producción vigentes.

Configurar backups de ambos volúmenes desde Coolify. Antes de migrar datos, detener
los dos servicios para obtener una copia consistente. No copiar únicamente el archivo
SQLite mientras el bot está escribiendo.

## Red y formulario privado

El host publica:

- `TAILSCALE_BIND_IP:8090` → formulario privado;
- `TAILSCALE_BIND_IP:6080` → noVNC de diagnóstico.

No abrir esos puertos en la interfaz pública, no usar Tailscale Funnel y no asignarles
un dominio de Coolify. El contenedor escucha en `0.0.0.0`, pero Docker sólo publica en
la IP indicada por `TAILSCALE_BIND_IP`.

La URL que llega por Telegram contiene un token aleatorio de un solo uso, no la clave.
El CUIT y la clave fiscal se envían desde el navegador directamente al worker a través
del tailnet.

## Primer despliegue

1. Crear la aplicación Docker Compose en Coolify.
2. Cargar todas las variables y marcar los cuatro secretos.
3. Confirmar que no se agregó ningún dominio público.
4. Desplegar ambos servicios.
5. Esperar que `bot` y `portal-worker` estén `healthy`.
6. En Telegram ejecutar `/resumen` y `/facturas`.
7. Desde `/ayuda`, generar el enlace privado y verificar que abra por Tailscale.
8. Confirmar que el alta termine en “Acceso guardado”.
9. Verificar que la siguiente sincronización actualice Monotributo e Ingresos Brutos.
10. Abrir `Pagos y vencimientos` y comprobar que el botón de pago sólo aparece en el
    chat autorizado. No confirmar la generación durante una prueba de despliegue.

## Verificaciones

Desde el repositorio, antes de desplegar:

```bash
./scripts/deployment_preflight.sh
.venv/bin/python -m unittest discover -s tests
```

En la VPS, el healthcheck interno del portal consulta `http://127.0.0.1:8090/health`.
El bot valida que la base SQLite pueda abrirse. Los endpoints de salud no contienen
datos personales ni secretos.

## Actualización y rollback

Una actualización normal reconstruye las imágenes y conserva ambos volúmenes. Antes
de una versión con cambios de esquema, tomar backup. Para volver atrás, desplegar la
imagen o commit anterior sin eliminar volúmenes. Nunca usar “delete volumes” como
parte de un redeploy o rollback.
