# Simple ARCA

Bot personal de Telegram para consultar obligaciones tributarias de ARCA y Santa Fe,
recibir avisos útiles y emitir Facturas C mediante la API oficial WSFEv1.

La interfaz visible tiene tres comandos:

- `/resumen`: pagos, deuda que requiere atención, mensajes y riesgo de recategorización.
- `/facturas`: historial, PDF, repetición como borrador y nueva Factura C.
- `/ayuda`: explicación breve y acceso a la configuración privada.

Los comandos técnicos anteriores siguen disponibles como alias ocultos para auditoría,
pero no aparecen en el menú de Telegram.

## Qué consulta

- Situación registral y categoría de Monotributo mediante servicios oficiales de ARCA.
- Domicilio Fiscal Electrónico sin abrir comunicaciones.
- Facturas emitidas por WSFE y las nuevas facturas generadas por el bot.
- Historial mensual verificado de puntos de venta que WSFE no puede listar.
- Cuenta corriente de Monotributo y pagos confirmados en los portales de ARCA.
- Régimen Simplificado de Ingresos Brutos de Santa Fe y pagos registrados.
- Topes vigentes de Monotributo publicados por ARCA, con respaldo local de la última
  tabla válida.

No existe una API pública única para toda esa información. El bot combina APIs
oficiales con un worker aislado para los portales que requieren CUIT y clave fiscal.

## Qué automatiza

- Actualización de APIs oficiales cada seis horas.
- Actualización de Monotributo, pagos e Ingresos Brutos cada seis horas.
- Evaluación de alertas cada cinco minutos, con deduplicación persistente.
- Verificación diaria de la tabla oficial de topes de Monotributo.
- Recordatorios de vencimiento a 14, 7, 3, 1 y 0 días.
- Avisos si una fuente deja de actualizarse.
- Conciliación de pagos: si un pago está confirmado pero la cuenta aún no terminó de
  reflejarlo, el bot avisa y nunca recomienda pagarlo otra vez.
- Emisión de Factura C con CAE, QR fiscal y PDF Original.
- Preparación y generación de VEP para la cuota corriente de Ingresos Brutos de
  Santa Fe, siempre con revisión y confirmación expresa.

Los intervalos se pueden cambiar por variables de entorno. El detalle funcional está
en [BOT_TELEGRAM_TRIBUTARIO.md](BOT_TELEGRAM_TRIBUTARIO.md).

## Arquitectura

El Compose ejecuta dos contenedores no privilegiados:

- `bot`: Telegram, APIs oficiales, WSAA/WSFE, PDF y avisos automáticos.
- `portal-worker`: ingreso a portales, sincronización periódica y servidor privado de
  configuración de credenciales.

Comparten únicamente la base SQLite persistente. El estado del navegador se conserva
en un volumen separado. Los certificados y la clave de cifrado no se guardan en la
imagen ni en la base.

En producción, Coolify entrega los secretos como variables protegidas. El entrypoint
los escribe con modo `0600` dentro de un `tmpfs`, elimina sus valores del entorno y
recién después inicia la aplicación.

La base se crea vacía: el código no contiene clientes, facturas, importes ni CUIT
personales precargados. El historial anterior se incorpora una sola vez mediante una
importación privada a SQLite; desde entonces, cada factura nueva emitida por el bot se
agrega automáticamente.

## Configuración local

Requisitos: Docker Compose o Python 3.12 con las dependencias del proyecto.

```bash
cp config.example.env .env
./scripts/generate_session_encryption_key.sh
docker compose -f compose.yaml -f compose.local.yaml up --build -d bot
docker compose -f compose.yaml -f compose.local.yaml --profile portal up --build -d portal-worker
```

El override local monta `./secrets` como sólo lectura en `/run/local-secrets`. En el
repositorio local se conservan únicamente:

- `session_encryption.key`
- `simplearcaprod.key`
- `simplearcaprod_7d17b50e91a73602.crt`

`secrets/`, `.env` y todos los formatos habituales de claves y certificados están
ignorados por Git.

Para ejecutar comandos o pruebas sin Docker:

```bash
.venv/bin/python -m contador_ai --command /resumen --chat-id CHAT_ID
.venv/bin/python -m unittest discover -s tests
```

La importación mensual y su formato están documentados en
[DEPLOY_COOLIFY.md](DEPLOY_COOLIFY.md#importación-inicial-del-historial).

## Alta privada de CUIT y clave fiscal

La conexión tiene dos mecanismos internos, pero el usuario completa un solo flujo:

- `/conectar CUIT` verifica automáticamente la identidad mediante el certificado ya
  autorizado en ARCA. Si todavía falta el acceso web, la misma respuesta incluye el
  botón `Completar conexión`.
- El formulario guarda una única clave fiscal cifrada para consultar tanto Monotributo
  como Ingresos Brutos. Al terminar, ambos servicios se actualizan y Telegram confirma
  que la cuenta quedó lista.

El enlace es de un solo uso y dura diez minutos. El
formulario manda el CUIT y la clave directamente a `portal-worker`; esos datos no
pasan por Telegram. La clave se valida contra ARCA y sólo se conserva cifrada.
`/configurar_arca` queda disponible como comando oculto para reemplazar una clave que
haya cambiado o haya sido rechazada.

`/desconectar` deja de consultar y mostrar la cuenta inmediatamente. Las facturas
emitidas y la clave cifrada se conservan para permitir una reconexión sin repetir toda
la configuración, pero no quedan accesibles mientras el CUIT está desvinculado.

En la VPS el enlace usa HTTP sobre Tailscale. El cifrado del transporte lo aporta el
túnel WireGuard de Tailscale y Docker publica los puertos exclusivamente en la IP del
tailnet. La aplicación sólo admite este modo si:

- `ARCA_ENROLLMENT_ALLOW_TAILSCALE_HTTP=true`; y
- la URL usa una IP `100.64.0.0/10` o un nombre completo terminado en `.ts.net`.

En `winston`, el formulario se publica en `100.75.19.73:8091` porque `8090` ya está
ocupado por Beszel; noVNC usa `100.75.19.73:6080`. No se debe asignar un dominio
público de Coolify a ninguno de esos puertos.

## Producción en Coolify

El procedimiento completo, variables, secretos, persistencia, verificaciones y
rollback está en [DEPLOY_COOLIFY.md](DEPLOY_COOLIFY.md).
La plantilla lista para copiar en Winston es
[config.coolify-winston.env.example](config.coolify-winston.env.example).

La configuración de producción es [compose.yaml](compose.yaml). El archivo
[compose.local.yaml](compose.local.yaml) existe solamente para desarrollo y nunca se
debe agregar como Compose adicional en Coolify.

## Seguridad

- El bot rechaza chats y grupos no incluidos en `BOT_ALLOWED_CHAT_IDS`.
- Sólo `INVOICE_EMISSION_CHAT_IDS` puede emitir facturas.
- Sólo `IIBB_PAYMENT_CHAT_IDS` puede generar VEP provinciales. Generar el VEP no
  debita dinero; el pago se confirma en ARCA o en la entidad elegida.
- El bot nunca guarda CBU, tarjetas, claves bancarias ni credenciales de billeteras.
- La Factura C se autoriza exclusivamente mediante WSFE; nunca se automatiza
  Comprobantes en Línea con cookies.
- El punto `00001` queda reservado al sistema manual. WSFE usa el punto `00002`.
- CUIT, período, cliente e importe se controlan antes de emitir para advertir
  duplicados.
- Los borradores nunca incluyen CAE ni QR fiscal.
- Las credenciales guardadas y los tickets WSAA están cifrados con Fernet.
- El formulario privado tiene token de un solo uso, límite de tamaño, encabezados
  contra caché y framing, y no registra la clave.
- Los logs van a stdout/stderr y Docker limita su rotación.

## Documentación

- [BOT_TELEGRAM_TRIBUTARIO.md](BOT_TELEGRAM_TRIBUTARIO.md): comportamiento del bot.
- [DEPLOY_COOLIFY.md](DEPLOY_COOLIFY.md): despliegue en la VPS.
- [ONBOARDING_ARCA_WS.md](ONBOARDING_ARCA_WS.md): certificados, relaciones y renovación.
- [ARQUITECTURA_OFICIAL_MULTIUSUARIO.md](ARQUITECTURA_OFICIAL_MULTIUSUARIO.md): fuentes y límites técnicos.
- [PROCEDIMIENTOS_ARCA.md](PROCEDIMIENTOS_ARCA.md): procedimientos y evidencia histórica.
- [CONSULTA_API_SANTA_FE.md](CONSULTA_API_SANTA_FE.md): respuesta administrativa provincial.
- [CONTEXTO_TRIBUTARIO.md](CONTEXTO_TRIBUTARIO.md): contexto tributario personal verificado.
