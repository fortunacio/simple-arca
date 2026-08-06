# Monitor tributario — servicio multiusuario

Bot de Telegram desplegable en una VPS privada. Cada usuario debe vincular sus propias cuentas y toda respuesta tributaria debe provenir de una consulta en vivo identificada por fuente y fecha.

La arquitectura objetivo y sus límites oficiales están documentados en
[ARQUITECTURA_OFICIAL_MULTIUSUARIO.md](ARQUITECTURA_OFICIAL_MULTIUSUARIO.md).
El procedimiento técnico de certificados y pruebas está en
[ONBOARDING_ARCA_WS.md](ONBOARDING_ARCA_WS.md).

## Contrato del producto

- No precarga categorías, deudas, pagos ni facturación de ninguna persona.
- No importa datos desde conversaciones o documentos históricos.
- Aísla conexiones y resultados por `chat_id`.
- Si un organismo no está conectado, falla de forma explícita y no inventa una respuesta.
- Los snapshots sólo pueden ser escritos por un conector real y tienen vencimiento.

## Estado actual

Funciona:

- Transporte multiusuario de Telegram.
- Acceso global restringido por `BOT_ALLOWED_CHAT_IDS`: mensajes, botones y grupos
  no autorizados se descartan antes de ejecutar comandos o crear datos.
- Persistencia y aislamiento por usuario.
- Comandos que fallan de forma segura mientras no exista conexión.
- Revocación con `/desconectar`.
- Imagen Docker no privilegiada con OpenSSL y certificados CA.
- Transporte HTTP con conexiones verificadas y límites de tiempo.
- WSAA: TRA, firma CMS mediante OpenSSL, SOAP Faults y caché en memoria por servicio.
- Constancia de Inscripción: `dummy`, consulta oficial de categoría de monotributo,
  impuestos y actividades, con verificación del CUIT representado.
- WSCCOMU: consulta paginada de resúmenes no leídos sin abrir comunicaciones.
- Compatibilidad con las respuestas XOP/MTOM usadas por WSCCOMU en producción.
- WSFEv1: consulta de puntos de venta, parámetros, último comprobante y detalle;
  emisión idempotente de Factura C mediante `FECAESolicitar` y recuperación ante
  respuestas inciertas con `FECompConsultar`.
- Flujo de Telegram para listar, repetir y emitir facturas desde borradores
  editables, con autorización limitada por `INVOICE_EMISSION_CHAT_IDS`.
- Clientes y plantillas, historial inmutable de facturas y auditoría de cada intento.
- PDF Original A4 regenerable, con CAE y QR fiscal según RG 4892. Los borradores
  quedan identificados como no válidos y nunca incluyen CAE ni QR.
- Cálculo de facturación neta, promedio mensual y categoría proyectada para
  recategorización con topes obtenidos de la tabla pública de ARCA, validados y
  guardados en caché. Una tabla de un semestre anterior nunca se usa para proyectar.
- Consolidación sin duplicados entre las facturas nuevas consultadas por WSFE y el
  historial oficial de puntos de venta de Comprobantes en Línea. Cada importación
  registra el origen, el período consultado y su cobertura; no se completa con
  facturas recordadas, pagos ni valores simulados.
- Vinculación Telegram/CUIT en estado pendiente y activación sólo después de una
  respuesta oficial para ese mismo CUIT.
- Persistencia opcional cifrada de Tickets de Acceso para sobrevivir reinicios;
  es obligatoria al seleccionar producción.
- Instalación personal conectada a producción: certificado validado, Constancia de
  Inscripción, bandeja fiscal y autorización WSFE verificadas contra ARCA.
- Punto de venta `00002` activo como Factura Electrónica - Monotributo - Web
  Services y validado mediante `FEParamGetPtosVenta`. El `00001` permanece reservado
  para Comprobantes en Línea.
- CCMA: parser validado que separa deuda vencida, saldo a favor y obligaciones todavía
  no vencidas. El ingreso se renueva por HTTPS y Chromium queda limitado a leer CCMA;
  la sesión y las credenciales verificadas se guardan cifradas fuera de la base principal.
- Ingresos Brutos Santa Fe: consulta automática del Régimen Simplificado y VEPAI con
  la misma clave cifrada. Si VEPAI está en mantenimiento, el bot conserva la categoría
  y los pagos visibles, pero no afirma que la cuenta esté al día.

Pendiente:

- Conciliación automática de períodos e importes de Ingresos Brutos Santa Fe.
- Delegación de servicios a usuarios operadores y pool acotado de sesiones para CCMA,
  Comprobantes en Línea y Santa Fe.
- Extracción y normalización de respuestas de cada organismo.

## Comandos

- `/conectar`
- `/estado`
- `/bandeja`
- `/monotributo`
- `/facturacion`
- `/facturas`
- `/nueva_factura`
- `/nuevo_cliente`
- `/categoria`
- `/deuda_arca`
- `/iibb`
- `/vencimientos`
- `/desconectar`

## Desarrollo local

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -v
python3 -m contador_ai --command /estado --chat-id 123
python3 -m contador_ai --arca-dummy
```

## Docker

Completar `TELEGRAM_BOT_TOKEN` y `BOT_ALLOWED_CHAT_IDS` en `.env`. El bot no
arranca si la lista global está vacía. Para emitir también se requieren
`ARCA_WSFE_POINT_OF_SALE` e `INVOICE_EMISSION_CHAT_IDS`; una lista vacía bloquea
toda emisión.

Ejecutar:

```bash
docker compose up --build
```

La base se almacena en el volumen `tax_data` y `./secrets` se monta como sólo
lectura en `/run/secrets`.

### Conector personal de CCMA

El navegador de CCMA se ejecuta en un contenedor separado y su pantalla no se
publica en la red: Docker enlaza noVNC exclusivamente con `127.0.0.1:6080`.

El alta normal no usa noVNC. Configurar `ARCA_ENROLLMENT_BASE_URL` con una URL
HTTPS del proxy de la VPS dirigida al puerto interno `8090`. Desde Telegram usar:

```text
/configurar_arca
```

El bot genera un enlace aleatorio válido por diez minutos y por un único intento.
La clave se carga en esa página, viaja por HTTPS directamente al worker y nunca se
envía por Telegram. El worker verifica el ingreso y el CUIT antes de cifrarla. En
desarrollo puede usarse `http://127.0.0.1:8090`; cualquier URL remota sin HTTPS hace
que el bot se niegue a iniciar.

noVNC queda sólo como recuperación excepcional si ARCA solicita CAPTCHA u otra
validación manual. En una VPS se accede únicamente mediante un túnel SSH:

```bash
ssh -L 6080:127.0.0.1:6080 usuario@vps
```

Después iniciar el sincronizador periódico:

```bash
docker compose --profile portal up -d portal-worker
```

La sesión y las credenciales se cifran con `SESSION_ENCRYPTION_KEY` y quedan en el
volumen `portal_state`, con permisos `0600`. Cuando la sesión vence, el worker vuelve
a ingresar automáticamente. Si ARCA rechaza la clave o exige CAPTCHA/otra validación,
no insiste en bucle y lo informa. El worker consulta CCMA, Régimen Simplificado y
VEPAI cada seis horas y actualiza los datos que usan `/monotributo`, `/deuda_arca`
y `/iibb`. No genera VEP ni realiza pagos.

## Autenticación prevista

1. El usuario ejecuta `/conectar`.
2. El bot genera un enlace HTTPS de un solo uso asociado al `chat_id`.
3. El usuario delega los servicios necesarios a la identidad operadora desde ARCA.
4. El servicio utiliza certificados oficiales. Para CCMA personal puede conservar
   credenciales cifradas, dadas de alta fuera de Telegram, porque no existe una API
   oficial equivalente para esa cuenta corriente.
5. Cada comando consulta la fuente en vivo y conserva únicamente un resultado con vencimiento corto.

Las APIs oficiales con certificado son el camino principal. Los portales sin API
pública usarán, como transición, un conector interactivo delegado y centralizado; no
se presentarán endpoints privados como APIs soportadas.
