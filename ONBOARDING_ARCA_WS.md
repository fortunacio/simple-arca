# Onboarding de ARCA Web Services

## Estado implementado

El servicio ya puede:

- validar el formato y dígito verificador de un CUIT;
- mantener la relación Telegram/CUIT en estado pendiente;
- crear y firmar un TRA para WSAA;
- reutilizar un Ticket de Acceso mientras continúe vigente;
- persistir Tickets de Acceso cifrados para sobrevivir reinicios;
- consultar la Constancia de Inscripción y verificar que ARCA responda por el mismo CUIT;
- consultar resúmenes no leídos de WSCCOMU sin abrir comunicaciones;
- consultar comprobantes existentes mediante WSFEv1 sin emitir nuevos;
- rechazar datos anteriores cuando una consulta en vivo falla.

## Preparar homologación

### 1. Crear clave privada y CSR

Desde la raíz del proyecto:

```bash
chmod +x scripts/generate_arca_csr.sh
./scripts/generate_arca_csr.sh CUIT alias-homologacion
```

Se generan dos archivos dentro de `secrets/`:

- `alias-homologacion.key`: clave privada; no sale del servidor.
- `alias-homologacion.csr`: solicitud que se carga en WSASS.

El script no sobrescribe claves existentes.

WSASS sólo admite letras y números en el nombre simbólico. El alias visible puede
diferir del nombre local de los archivos; por ejemplo, `simplearca` es válido y
`simple_arca` no.

### 2. Emitir el certificado de homologación

1. Adherir el servicio WSASS desde Administrador de Relaciones de ARCA.
2. Ingresar en WSASS.
3. Crear el certificado utilizando el CSR generado.
4. Descargar el certificado emitido dentro de `secrets/`.
5. Autorizar el mismo DN para estos servicios:
   - `ws_sr_constancia_inscripcion`
   - `veconsumerws`
   - `wsfe`

Para una prueba multi-CUIT, el certificado/DN debe quedar autorizado para el CUIT
representado según el procedimiento de WSASS. Producción usa Administrador de
Certificados Digitales y Administrador de Relaciones, no los certificados de WSASS.

### 3. Configurar el proceso

```dotenv
ARCA_ENVIRONMENT=homologation
ARCA_OPERATOR_CUIT=CUIT_DEL_OPERADOR
ARCA_CERTIFICATE_ALIAS=alias-homologacion
ARCA_CERTIFICATE_PATH=./secrets/alias-homologacion.crt
ARCA_PRIVATE_KEY_PATH=./secrets/alias-homologacion.key
```

No copiar claves privadas dentro de la imagen Docker. Deben montarse como secretos
de sólo lectura.

Generar una clave diferente por instalación y guardarla únicamente en el gestor de
secretos o `.env` de la VPS:

```bash
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Asignar el resultado a `SESSION_ENCRYPTION_KEY`. Si se pierde esa clave, los Tickets
de Acceso guardados dejan de ser recuperables y WSAA deberá emitir otros; no se
pierden datos tributarios.

Para Docker es preferible generar el archivo directamente y montarlo como secreto:

```bash
./scripts/generate_session_encryption_key.sh
```

Luego configurar `SESSION_ENCRYPTION_KEY_FILE=/run/secrets/session_encryption.key`.
No configurar simultáneamente la variable directa y la variable terminada en
`_FILE`.

## Verificación

### Disponibilidad sin datos personales

```bash
python3 -m contador_ai --arca-dummy
```

Resultado esperado:

```text
appserver=OK authserver=OK dbserver=OK
```

### Vincular un CUIT desde Telegram

```text
/conectar 20123456786
```

Esto crea una solicitud `PENDING`. No demuestra autorización.

### Verificar delegación y Padrón

```bash
python3 -m contador_ai --sync-arca-profile --chat-id CHAT_ID
```

La identidad pasa a `VERIFIED` únicamente si WSAA concede el ticket y la Constancia
devuelve exactamente el CUIT vinculado.

### Consultar bandeja sin abrir mensajes

```bash
python3 -m contador_ai --sync-arca-dfe --chat-id CHAT_ID
```

El conector sólo invoca `consultarComunicaciones`. No existe código que invoque
`consumirComunicacion`.

## Producción

No cambiar `ARCA_ENVIRONMENT` a `production` hasta completar:

- certificado de producción;
- delegación de ambos WSN;
- backup y rotación de claves;
- rate limiting y observabilidad sin datos tributarios en logs.

## Estado de la instalación de homologación

- WSASS adherido.
- Certificado `simplearca` emitido y verificado contra su clave privada.
- Autorizaciones creadas para `ws_sr_padron_a4` y `veconsumerws` sobre el CUIT del
  operador.
- WSAA concedió Tickets de Acceso para ambos servicios.
- Padrón A4 de homologación no contiene el CUIT real del operador; respondió
  `No existe persona con ese Id`. Esto no es un rechazo de certificado.
- La caché persistente cifrada quedó habilitada para evitar el error
  `coe.alreadyAuthenticated` después de reinicios.
- CSR de producción `simplearcaprod.csr` y clave privada asociada preparados
  localmente.

## Estado de la instalación de producción

- Certificado `simplearcaprod` emitido por ARCA y válido hasta agosto de 2028.
- Relaciones aceptadas para `ws_sr_constancia_inscripcion`, `ws_sr_padron_a13` y
  `veconsumerws` sobre el CUIT del operador.
- `ws_sr_padron_a4` rechazó la relación en producción con el mensaje oficial
  `La persona no se encuentra habilitada para operar el servicio`; no se utiliza.
- La Constancia de Inscripción reemplaza A4 porque devuelve expresamente
  `categoriaMonotributo`, impuestos y actividades.
- El manual publica `aws.arca.gov.ar`, pero ese nombre no resolvía por DNS al
  05/08/2026; el endpoint operativo comprobado continúa en `aws.afip.gov.ar`.
- Certificado `simplearcaprod_7d17b50e91a73602.crt` guardado localmente; su clave
  pública coincide con `simplearcaprod.key` y el proceso ya usa producción.
- La consulta real de Constancia quedó validada: ARCA informó estado activo,
  categoría F de servicios y período 202608.
- WSCCOMU quedó validado en producción mediante `consultarComunicaciones`: respondió
  sin mensajes no leídos para la ventana 06/07/2026–05/08/2026 y no se invocó
  `consumirComunicacion`.
- WSCCOMU entrega la respuesta SOAP dentro de `multipart/related` XOP/MTOM. El
  transporte extrae la parte `application/xop+xml` antes de parsear el XML.
- La relación `wsfe` para `simplearcaprod` quedó creada y WSAA concedió un Ticket de
  Acceso de producción.
- WSFE de producción requiere compatibilidad con parámetros DH antiguos. El cliente
  reduce `SECLEVEL` únicamente para ese endpoint y mantiene la validación del
  certificado TLS.
- La consulta real `FEParamGetPtosVenta` respondió `602: Sin Resultados`. El punto de
  venta usado por Comprobantes en Línea no aparece en WSFE y no debe informarse una
  facturación igual a cero.
- Para calcular la proyección se necesita importar una vez el historial oficial y,
  para la continuidad automática, emitir desde un punto de venta Web Services.
