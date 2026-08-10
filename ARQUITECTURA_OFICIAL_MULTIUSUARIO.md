# Arquitectura de integración tributaria

## Alcance real

La implementación actual es personal: sólo atiende chats incluidos explícitamente en
la configuración y emite únicamente para el CUIT representado. La base permite separar
datos por `chat_id`, pero no se presenta como SaaS multiusuario ni ofrece onboarding
público.

La regla central es que ningún dato tributario se inventa ni se recupera de esta
conversación. Cada respuesta proviene de una API oficial, un portal autenticado o un
registro fiscal autorizado previamente guardado por el propio sistema.

## Por qué hay dos tipos de conectores

ARCA y Santa Fe no ofrecen una API única para categoría, bandeja, deuda, pagos,
facturación e Ingresos Brutos.

| Información | Fuente | Autenticación |
|---|---|---|
| Situación registral y categoría | Servicio oficial de ARCA | WSAA + certificado |
| Domicilio Fiscal Electrónico | Servicio oficial delegado | WSAA + certificado |
| Factura C y consulta por número | WSFEv1 | WSAA + certificado |
| Historial mensual anterior | Importación privada verificada | Acceso administrativo a la instalación |
| Cuenta de Monotributo | Portal de ARCA | CUIT + clave cifrada |
| Pagos nacionales | Portal de pagos de ARCA | CUIT + clave cifrada |
| Régimen Simplificado Santa Fe | Portal provincial con SSO ARCA | CUIT + clave cifrada |
| Cotización y VEP corriente de Santa Fe | Formulario 5426 provincial | CUIT + clave cifrada y confirmación humana |
| Topes de categoría | Publicación oficial de ARCA | HTTP público + respaldo local |

WSFE permite consultar el último número y un comprobante determinado, pero no ofrece
un listado completo de todas las facturas emitidas en otros puntos de venta. Por eso el
historial combina totales mensuales verificados, las nuevas facturas del bot y las
consultas individuales disponibles. Los totales importados reemplazan el subtotal del
mismo mes para evitar duplicarlo; no se suman encima de las facturas individuales.
El último mes importado establece el corte de migración: los meses cerrados posteriores
se consideran cubiertos por el barrido completo de WSFE, incluso cuando no hubo
facturación. Volver a emitir manualmente fuera de WSFE después de ese corte rompería
esa cobertura y no está soportado.

## Componentes

### `bot`

- long polling de Telegram;
- autorización por chat;
- `/resumen`, `/facturas` y `/ayuda`;
- clientes, plantillas, borradores y auditoría;
- WSAA, situación registral, DFE y WSFE;
- PDF Original y QR fiscal;
- cálculo de categoría proyectada;
- avisos automáticos y deduplicación.

### `portal-worker`

- verificación de CUIT y clave fiscal;
- renovación automática de sesiones;
- consultas de cuenta, pagos e Ingresos Brutos;
- cotización y generación idempotente del VEP provincial;
- servidor HTTP privado de alta;
- Chromium aislado para los pasos que todavía lo requieren;
- noVNC exclusivamente como diagnóstico.

### Persistencia

`tax_data` guarda SQLite. `portal_state` guarda credenciales cifradas y estados de
navegador. Ambos sobreviven al reemplazo de imágenes. Los secretos criptográficos no
se guardan en esos volúmenes como texto plano.

Una base nueva sólo recibe el esquema. No existen seeds personales en la aplicación.
El historial anterior se carga con una operación administrativa idempotente y queda
asociado al chat verificado; los archivos usados para importarlo no entran en Git ni en
la imagen.

## Flujo del enlace privado

1. El usuario solicita configuración desde Telegram.
2. El bot crea un token aleatorio ligado al chat y al CUIT, válido diez minutos.
3. Telegram envía un botón con una URL de Tailscale.
4. El navegador manda CUIT y clave directamente a `portal-worker` por el tailnet.
5. El worker reclama el token para impedir su reutilización.
6. Verifica el ingreso contra ARCA.
7. Cifra la clave con Fernet y la guarda en `portal_state`.
8. Las sincronizaciones siguientes renuevan sesiones sin intervención manual.

El transporte es HTTP en la capa de aplicación, pero el trayecto dispositivo–VPS está
cifrado por WireGuard/Tailscale. Docker liga el puerto a la IP de Tailscale y no debe
existir dominio, proxy público ni Funnel.

## Flujo de una Factura C

1. Crear o repetir un borrador.
2. Verificar emisor, receptor, IVA, punto de venta y parámetros vigentes.
3. Consultar el último comprobante autorizado.
4. Persistir y bloquear el intento.
5. Solicitar CAE mediante `FECAESolicitar`.
6. Ante timeout, consultar el número pretendido antes de reintentar.
7. Guardar inmutablemente respuesta, CAE y vencimiento.
8. Generar PDF y QR sólo después de la autorización.

La unicidad por CUIT, tipo, punto y número, junto con los estados explícitos del
borrador, evita dobles emisiones por concurrencia o doble pulsación.

## Flujo de un VEP provincial

1. El bot toma la próxima cuota informada por Santa Fe.
2. El usuario elige la red de pago.
3. `portal-worker` cotiza capital e intereses y guarda el resultado.
4. Telegram muestra el total y exige confirmación explícita.
5. El worker vuelve a cotizar; si cambió, pide otra confirmación.
6. Envía una sola solicitud, conserva número y PDF y nunca reintenta un resultado
   incierto.
7. El usuario autoriza el débito fuera del bot, en ARCA, su banco o billetera.
8. El bot considera el período pagado sólo cuando Santa Fe lo registra.

## Flujo de avisos

El worker escribe snapshots cada seis horas. El bot evalúa cambios cada cinco minutos.
Los eventos se deduplican en SQLite mediante clave y huella. La regla de conciliación
de pagos se aplica antes de generar deuda: un pago confirmado pendiente de reflejarse
suprime la alerta contradictoria de deuda para ese mismo saldo.

Los estados rotos son la excepción a la deduplicación permanente: una pérdida de
acceso o una fuente desactualizada se recuerda cada hora hasta que la condición se
resuelva. La hora de la última entrega queda persistida, por lo que reiniciar el bot no
adelanta ni elimina el próximo recordatorio.

La evaluación frecuente trabaja sobre datos locales. Como máximo cada seis horas
dispara la actualización de perfil, mensajes y facturación por API oficial. Los
portales tienen su ciclo independiente de seis horas. La página pública de topes de
Monotributo se verifica una vez por día y conserva la última tabla oficial válida si
la fuente no responde.

## Fronteras de seguridad

- Chats no autorizados se descartan antes de consultar o escribir datos.
- Facturación tiene una lista de chats más restrictiva.
- La generación de VEP tiene su propia lista restrictiva de chats.
- Certificado, clave privada y clave Fernet son secretos de despliegue.
- La clave fiscal nunca pasa por Telegram ni se registra en logs.
- Los tokens del formulario son de un solo uso y se almacenan por hash.
- Los contenedores ejecutan UID/GID `10001`, sin privilegios.
- Los secretos de Coolify viven en un `tmpfs` durante la ejecución.
- SQLite y el estado de navegador son los únicos datos persistentes.
- Los servicios internos no usan una red Compose personalizada.

## Límites

- No se ejecutan pagos ni se operan cuentas bancarias.
- Sólo se genera el VEP corriente de Ingresos Brutos de Santa Fe; no se generan planes
  ni liquidaciones consolidadas.
- No se presentan declaraciones juradas ni recategorizaciones.
- No se cubren todas las obligaciones nacionales fuera de Monotributo.
- Los portales pueden cambiar sin aviso; el bot detecta fuentes atrasadas y avisa.
- CAPTCHA o una validación extraordinaria de ARCA puede requerir intervención manual.

## Evolución a múltiples usuarios

Antes de ofrecerlo a terceros sería necesario separar certificados y autorización por
contribuyente, incorporar gestión de consentimiento y revocación, aislamiento de
secretos por tenant, límites de recursos para navegadores, observabilidad, política de
retención y revisión legal. La instalación actual no promete esas garantías.
