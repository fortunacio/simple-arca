# Despliegue en Coolify sobre Tailscale

## Resultado esperado

Coolify construye y mantiene dos servicios desde [compose.yaml](compose.yaml):

- `bot`, sin puertos publicados;
- `portal-worker`, con los puertos de host `8091` y `6080` ligados únicamente a la
  IP de Tailscale de `winston`. Dentro del contenedor, el formulario conserva `8090`.

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

## Winston verificado

Inspección de sólo lectura realizada el 06/08/2026:

- host: `winston.mandrill-piano.ts.net`;
- IP de Tailscale: `100.75.19.73`;
- Coolify: `4.1.2`;
- Docker: `29.6.1`; Docker Compose: `5.3.1`;
- arquitectura: `x86_64`;
- puertos libres para el proyecto: `6080` y `8091`;
- `8090` está ocupado por Beszel en todas las interfaces y no debe utilizarse;
- Tailscale Serve y Funnel: sin configuración;
- capacidad disponible: 424 GB de disco y 12 GB de memoria al momento del control.

La publicación `IP:host:contenedor` está soportada por Compose en Coolify. No asignar
dominios a ninguno de los dos servicios ni activar `Connect to Predefined Network`.

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

## Variables sensibles y botón `Lock`

Estas cuatro variables contienen secretos:

| Variable | Contenido |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token vigente de BotFather |
| `SESSION_ENCRYPTION_KEY` | Contenido exacto de `session_encryption.key` |
| `ARCA_CERTIFICATE_B64` | Certificado de producción codificado en base64 |
| `ARCA_PRIVATE_KEY_B64` | Clave privada de producción codificada en base64 |

Para copiarlos desde macOS sin incluir accidentalmente el símbolo `%` del prompt de
zsh, ejecutar cada comando por separado y pegar inmediatamente el portapapeles en la
variable correspondiente de Coolify:

```bash
base64 < secrets/simplearcaprod_7d17b50e91a73602.crt | tr -d '\n' | pbcopy
base64 < secrets/simplearcaprod.key | tr -d '\n' | pbcopy
```

El valor pegado debe contener únicamente caracteres base64. No debe terminar en `%`:
ese carácter es el prompt de zsh, no forma parte del certificado ni de la clave.

No pegar esos valores en el repositorio, logs, Telegram ni documentación. Durante el
arranque se escriben en `/run/contador-secrets`, que es memoria temporal, con modo
`0600`. El entrypoint elimina las variables del entorno antes de ejecutar Python.

En la vista normal de Coolify, para cada una de las cuatro:

1. pegar el valor únicamente en **Production Environment Variables**;
2. desmarcar `Available at Buildtime`;
3. dejar marcado `Available at Runtime`;
4. dejar `Is Multiline?` e `Is Literal?` desmarcados;
5. presionar `Update`;
6. comprobar el valor y, cuando la primera instalación funcione, presionar `Lock`.

`Lock` es irreversible desde la interfaz: si después hay que rotar o corregir el
secreto, se elimina la variable y se vuelve a crear. El icono del ojo sólo oculta o
muestra temporalmente el campo; no equivale a bloquearlo.

No activar `Use Docker Build Secrets`. Esa opción es para secretos que necesita el
proceso de construcción; esta aplicación sólo los necesita al ejecutarse. Las demás
variables del proyecto tampoco necesitan `Available at Buildtime`: pueden quedar
únicamente con `Available at Runtime`.

No cargar valores en **Preview Deployments Environment Variables**: este proyecto no
usa despliegues de preview y no debe existir una segunda instancia con el mismo token
de Telegram.

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
| `TAILSCALE_BIND_IP` | `100.75.19.73` |
| `ARCA_ENROLLMENT_HOST_PORT` | `8091` |
| `CCMA_NOVNC_HOST_PORT` | `6080` |
| `ARCA_ENROLLMENT_BASE_URL` | `http://winston.mandrill-piano.ts.net:8091` |
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
AUTOMATIC_AUTH_REMINDER_SECONDS=86400
AUTOMATIC_SYNC_INTERVAL_SECONDS=21600
ARCA_OFFICIAL_SNAPSHOT_TTL_SECONDS=129600
ARCA_CATEGORY_LIMITS_REFRESH_SECONDS=86400
ARCA_ENROLLMENT_ENABLED=true
ARCA_ENROLLMENT_HOST_PORT=8091
CCMA_NOVNC_HOST_PORT=6080
LOG_LEVEL=INFO
```

Para el acceso de emergencia configurar:

```env
CCMA_LOGIN_URL=http://winston.mandrill-piano.ts.net:6080/vnc.html?autoconnect=true&resize=scale
```

No definir `TAX_MONITOR_DB`, `CCMA_SESSION_DIR`, `ARCA_ENROLLMENT_BIND` ni
`ARCA_ENROLLMENT_PORT` en Coolify: el Compose fija sus rutas y puertos internos.

## Persistencia y copias de seguridad

`tax_data` contiene `/data/tax_monitor.sqlite3`: identidad, snapshots, historial
mensual verificado, facturas, VEP provinciales, sus PDFs oficiales, auditoría,
notificaciones y tickets cifrados.

`portal_state` contiene las credenciales cifradas y el estado renovable de los
navegadores. Para restaurar el acceso sin repetir el alta se necesitan conjuntamente:

1. ambos volúmenes;
2. el mismo `SESSION_ENCRYPTION_KEY`;
3. el certificado y la clave de producción vigentes.

Configurar backups de ambos volúmenes desde Coolify. Antes de migrar datos, detener
los dos servicios para obtener una copia consistente. No copiar únicamente el archivo
SQLite mientras el bot está escribiendo.

### Primer arranque

En un volumen `tax_data` vacío, ambos servicios ejecutan una inicialización
idempotente que crea `/data/tax_monitor.sqlite3` y todas sus tablas. La base no se
versiona ni se descarga desde Git. La inicialización no crea clientes, plantillas,
facturas, CUIT ni importes personales.

No se precargan credenciales, sesiones, snapshots tributarios, mensajes, pagos, VEP,
borradores ni auditoría del Mac. Las consultas oficiales vuelven a obtener el estado
actual; `/conectar CUIT` entrega el formulario para guardar nuevamente la clave fiscal
en el volumen `portal_state`.

### Importación inicial del historial

WSFE no lista las facturas anteriores de otros puntos de venta. Para que la proyección
de categoría sea completa, se importan una sola vez los totales mensuales verificados.
El archivo es privado, no se copia a la imagen ni se guarda en Git. Formato:

```json
{
  "source": "ARCA_COMPROBANTES_VERIFICADOS_YYYY-MM-DD",
  "months": [
    {"period": "2026-01", "total": "0.00"},
    {"period": "2026-02", "total": "123456.78"}
  ]
}
```

Hay que incluir también los meses cerrados sin facturación con total `0`. La importación
rechaza el mes en curso o meses futuros y valida períodos, duplicados, importes no
negativos y precisión de centavos; repetirla actualiza los mismos meses y nunca los
suma dos veces.

Después de `/conectar CUIT`, colocar temporalmente el JSON dentro del volumen
`tax_data` como `/data/monthly-billing-history.json` y ejecutar desde la terminal del
servicio `bot`:

```bash
python -m contador_ai --chat-id CHAT_ID \
  --import-monthly-billing /data/monthly-billing-history.json
```

Debe responder `Meses importados: N`. Borrar luego ese JSON temporal y ejecutar
`/categoria`; la fuente de verdad queda en SQLite. Las facturas que emita el bot se
incorporan solas y los totales mensuales importados prevalecen para su mismo mes, por
lo que no hay doble conteo.

El último mes importado es el corte entre el historial manual y el seguimiento por
WSFE. Desde el mes siguiente, el sistema también registra como completos los meses sin
facturas. No volver a emitir por el punto manual después de ese corte: esas facturas no
aparecen en WSFE y harían incompleta la proyección.

## Red y formulario privado

El host publica:

- `100.75.19.73:8091` → formulario privado, contenedor `8090`;
- `100.75.19.73:6080` → noVNC de diagnóstico.

No abrir esos puertos en la interfaz pública, no usar Tailscale Funnel y no asignarles
un dominio de Coolify. El contenedor escucha en `0.0.0.0`, pero Docker sólo publica en
la IP indicada por `TAILSCALE_BIND_IP`.

La URL que llega por Telegram contiene un token aleatorio de un solo uso, no la clave.
El CUIT y la clave fiscal se envían desde el navegador directamente al worker a través
del tailnet.

Antes de abrir el enlace desde el iPhone, confirmar que la aplicación Tailscale esté
conectada. Durante la inspección del 06/08/2026 ese dispositivo figuraba desconectado.

## Guía rápida de variables

La plantilla [config.coolify-winston.env.example](config.coolify-winston.env.example)
contiene exactamente las variables de este host. En Coolify:

1. copiar sus variables en `Environment Variables`;
2. reemplazar `CHAT_ID` por el mismo chat configurado localmente;
3. reemplazar los cuatro secretos y también `REEMPLAZAR_CUIT` y
   `REEMPLAZAR_NUMERO_IIBB`;
4. dejar todas las variables sólo en `Available at Runtime` y, después del primer
   despliegue correcto, usar `Lock` únicamente en los cuatro secretos;
5. guardar y volver a renderizar el Compose antes de desplegar.

Coolify puede no crear automáticamente las variables que sólo aparecen en la sección
`ports` del Compose. Si faltan `TAILSCALE_BIND_IP`,
`ARCA_ENROLLMENT_HOST_PORT` o `CCMA_NOVNC_HOST_PORT`, agregarlas con `+ Add` o desde
`Developer view` usando los valores de la plantilla. Deben existir en Production,
únicamente en Runtime y no deben bloquearse con `Lock`.

No cargar en Coolify `TAX_MONITOR_DB`, `CCMA_SESSION_DIR`,
`SESSION_ENCRYPTION_KEY_FILE`, `ARCA_CERTIFICATE_PATH`, `ARCA_PRIVATE_KEY_PATH`,
`ARCA_ENROLLMENT_BIND`, `ARCA_ENROLLMENT_PORT` ni `DISPLAY`: sus valores internos ya
están fijados por el Compose y las imágenes.

El contenedor convierte `SESSION_ENCRYPTION_KEY` en un archivo temporal al arrancar.
Por eso `SESSION_ENCRYPTION_KEY_FILE` debe quedar sin configurar en Coolify. Si se
cargan ambas fuentes, el healthcheck del bot falla con el mensaje
`Configurar sólo SESSION_ENCRYPTION_KEY o SESSION_ENCRYPTION_KEY_FILE`.
El Compose de producción ya no pasa la variable de archivo; el override local es el
único que la configura para leer `secrets/session_encryption.key`.

## Primer despliegue

1. Confirmar que esta versión esté confirmada y publicada en la rama que leerá
   Coolify.
2. Detener la instalación local antes de iniciar la de la VPS. Dos procesos usando el
   mismo token de Telegram producen conflictos de `getUpdates` y podrían repartir los
   mensajes entre instalaciones:

   ```bash
   docker compose -f compose.yaml -f compose.local.yaml --profile portal stop bot portal-worker
   ```

3. Crear la aplicación Docker Compose en Coolify.
4. Elegir el repositorio y la rama que contengan esta versión, usando únicamente
   `compose.yaml`.
5. Cargar las variables de la plantilla y marcar los cuatro secretos.
6. Confirmar que no se agregó ningún dominio y que `Connect to Predefined Network`
   permanezca desactivado.
7. Desplegar ambos servicios.
8. Esperar que `bot` y `portal-worker` estén `healthy`.
9. En una instalación nueva, ejecutar `/conectar CUIT`. El bot debe verificar
   el CUIT automáticamente y mostrar `Completar conexión` en la misma respuesta.
10. Abrir ese botón mediante Tailscale y confirmar que el alta termine en “Acceso
    guardado”. El worker debe actualizar las fuentes inmediatamente y Telegram debe
    avisar que la cuenta quedó lista.
11. Si se necesita la proyección histórica, realizar la importación mensual privada
    explicada arriba.
12. Ejecutar `/resumen`, `/categoria` y `/facturas`; verificar Monotributo, Ingresos
    Brutos y que la proyección indique el número esperado de meses cubiertos.
13. Abrir `Pagos y vencimientos` y comprobar que el botón de pago sólo aparece en el
    chat autorizado. No confirmar la generación durante una prueba de despliegue.

Una instalación nueva no copia automáticamente la base SQLite ni las credenciales
cifradas del Mac. No migrarlas durante el primer despliegue: volver a vincular la
cuenta es más seguro. Si luego se necesita conservar auditoría local, realizar una
migración separada con ambos servicios detenidos y la misma clave Fernet.

## Verificaciones

Desde el repositorio, antes de desplegar:

```bash
./scripts/deployment_preflight.sh
.venv/bin/python -m unittest discover -s tests
```

En la VPS, el healthcheck interno del portal consulta `http://127.0.0.1:8090/health`.
El bot valida que la base SQLite pueda abrirse. Los endpoints de salud no contienen
datos personales ni secretos.

Si un build se cancela mientras Playwright descarga Chromium, esperar a que Coolify
termine de cerrar el contenedor auxiliar antes de iniciar otro. Dos builds superpuestos
pueden dejar a BuildKit rechazando la segunda sesión con `only one connection allowed`;
cuando no queda ningún build activo y `docker buildx inspect default --bootstrap`
informa `running`, un redeploy limpio puede reutilizar la caché y continuar.

## Actualización y rollback

Una actualización normal reconstruye las imágenes y conserva ambos volúmenes. Antes
de una versión con cambios de esquema, tomar backup. Para volver atrás, desplegar la
imagen o commit anterior sin eliminar volúmenes. Nunca usar “delete volumes” como
parte de un redeploy o rollback.
