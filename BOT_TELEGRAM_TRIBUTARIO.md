# Monitor tributario personal por Telegram

> Este diseño personal quedó reemplazado como arquitectura de producto por
> [ARQUITECTURA_OFICIAL_MULTIUSUARIO.md](ARQUITECTURA_OFICIAL_MULTIUSUARIO.md).
> Se conserva únicamente como antecedente funcional y tributario.

## Estado y alcance

- Investigación actualizada al **04/08/2026**.
- Titular: un único contribuyente de Santa Fe.
- Alcance: monitoreo, alertas, proyecciones y acceso a trámites oficiales.
- Fuera de alcance por ahora: emitir facturas, presentar recategorizaciones y ejecutar pagos.
- Estado: especificación funcional y técnica; todavía no se creó el bot ni se agregaron dependencias.

## Objetivo

El bot debe responder, con fuente y fecha de actualización, estas seis preguntas:

1. ¿Cuándo debo revisar la recategorización nacional y provincial?
2. ¿Tengo mensajes nuevos en el Domicilio Fiscal Electrónico de ARCA?
3. ¿Debo Monotributo y está bien imputado el débito automático?
4. ¿Cuánto facturé en los últimos doce meses y hacia qué categoría voy?
5. ¿Tengo alguna otra deuda registrada en ARCA?
6. ¿Debo Ingresos Brutos de Santa Fe?

La respuesta principal será `/estado`. El sistema será determinístico, sin modelo de lenguaje y sin consumo de tokens.

## Resumen ejecutivo

La solución es viable, pero no todas las fuentes ofrecen el mismo nivel de automatización:

- **Bandeja ARCA:** automatización oficial mediante WSCCOMU. Se pueden consultar asuntos y estados sin abrir los mensajes.
- **Categorías y vencimientos:** automatización total a partir de las tablas y calendarios oficiales.
- **Facturación acumulada:** cálculo total una vez que se sincronizan los comprobantes emitidos; no se emitirán facturas.
- **Deuda ARCA:** CCMA y, si aparecen otros impuestos, Sistema de Cuentas Tributarias mediante una sesión local autenticada. El sincronizador intentará primero repetir las solicitudes internas con las cookies y tokens temporales del navegador.
- **Ingresos Brutos Santa Fe:** conciliación con VEPAI y los aplicativos provinciales usando el mismo mecanismo local. No se identificó una API pública soportada, pero sí pueden evaluarse sus solicitudes web internas.

El bot no confundirá una estimación con un dato oficial. Cada resultado mostrará `fuente`, `observado_el` y `vigencia`.

## Tablero `/estado`

Ejemplo de respuesta:

```text
ESTADO TRIBUTARIO — actualizado 04/08/2026 18:30

ARCA · Bandeja
🟡 2 comunicaciones no leídas (sin abrir)
Próxima notificación de oficio: lunes 10/08

Monotributo
🟢 Categoría F desde 08/2026
🟡 Pago 08/2026 informado; falta confirmar imputación en CCMA
Próxima revisión: 01–05/02/2027

Facturación y categoría
Últimos 12 meses: $XX.XXX.XXX
Escenario con tabla vigente: E / F / G
Margen al próximo límite: $X.XXX.XXX
Calidad del dato: completa hasta 31/07/2026

ARCA · Otras obligaciones
🟢 Sin deuda observada en la última consulta
Fuente: Sistema de Cuentas Tributarias, 04/08/2026

Ingresos Brutos Santa Fe
🟢 Régimen Simplificado · categoría 3
🟡 Pago consolidado informado; falta confirmar imputación en VEPAI
Próxima revisión: enero de 2027
```

El bot nunca responderá simplemente “estás al día”. Debe indicar qué se revisó, en qué organismo y cuándo.

## 1. Recategorización

### Monotributo ARCA

- Se revisa dos veces por año: **febrero y agosto**.
- El trámite vence el **día 5** de esos meses, salvo prórroga oficial.
- En cada revisión se evalúan los **últimos doce meses de actividad**.
- Si corresponde mantener la categoría, ARCA indica que no hace falta realizar una acción.
- Si los parámetros son superiores o inferiores, corresponde recategorizarse.
- ARCA puede notificar una recategorización de oficio en el Domicilio Fiscal Electrónico.

El bot avisará 30, 15, 7, 3 y 1 día antes. Al publicarse una prórroga, la fecha original queda en el historial y se agrega la nueva vigencia.

### Ingresos Brutos Santa Fe

- La revisión es semestral, en **enero y julio**.
- La Resolución General API 43/2024 establece como regla el **31 de enero** y el **31 de julio**.
- La primera recategorización de 2026 fue prorrogada al **27/02/2026**.
- Para julio se usa el 31/07 como fecha base, salvo prórroga publicada específicamente.
- La segunda revisión no exige trámite si el contribuyente permanece en la misma categoría; la primera es obligatoria según la reglamentación provincial.
- El parámetro es el ingreso bruto devengado gravado de los últimos doce meses, excluidos los exentos y gravados a tasa cero.

El bot debe diferenciar `fecha_reglamentaria` de `fecha_prorrogada` y no reutilizar automáticamente la prórroga de un año anterior.

## 2. Bandeja de ARCA sin marcar mensajes como leídos

ARCA publica WSCCOMU para consultar el Domicilio Fiscal Electrónico.

### Integración segura

1. Autenticarse en WSAA con certificado y servicio `veconsumerws`.
2. Llamar a `consultarComunicaciones` por ventanas de hasta 31 días.
3. Guardar sólo el resumen: identificador, fecha de publicación, vencimiento, organismo, estado, asunto, prioridad y existencia de adjuntos.
4. Alertar por Telegram cuando aparezca una comunicación no leída.
5. No llamar automáticamente a `consumirComunicacion`.

`consultarComunicaciones` no registra lectura. `consumirComunicacion` recupera el contenido completo y registra el evento de lectura. Por eso el bot podrá mostrar el asunto, pero sólo abrirá el mensaje después de una orden explícita del usuario.

Consideraciones técnicas:

- Historia consultable por el servicio: hasta 360 días.
- Rango máximo por consulta: 31 días.
- Página máxima: 500 resultados.
- Estados relevantes: `1` no leído y `2` leído.
- WSDL de producción: `https://infraestructura.afip.gob.ar/ve-ws/services/veconsumer?wsdl`.

Consideración legal: ARCA informa que una comunicación se considera notificada al abrirla o el lunes posterior a su disponibilidad, lo que ocurra primero. No abrirla desde el bot no evita la notificación de oficio; sólo evita adelantar la lectura accidentalmente.

Comandos:

- `/bandeja`: lista resúmenes nuevos sin abrirlos.
- `/mensaje <id>`: muestra metadatos y pide confirmación antes de recuperar el contenido.
- `/abrir <id>`: acción explícita que registra lectura; debe requerir un segundo botón de confirmación.

## 3. Deuda de Monotributo

La fuente oficial es la **Cuenta Corriente de Monotributistas y Autónomos (CCMA)**. Debe verificarse por período:

- capital;
- intereses;
- pago registrado;
- fecha del pago;
- imputación al período correcto;
- saldo acreedor, si existe.

No se identificó una API pública de autoservicio para consultar CCMA. El MVP usará una sincronización asistida desde una sesión iniciada por el contribuyente. El proceso local podrá reutilizar temporalmente las cookies y tokens de esa sesión para efectuar directamente las mismas solicitudes que hace la página. La clave fiscal, cookies y tokens no se enviarán a Telegram ni al servidor del bot.

El débito automático no prueba por sí solo la cancelación. El estado será:

1. `EXPECTED`: cuota esperada.
2. `DEBIT_PENDING`: llegó la fecha, todavía dentro del plazo normal de procesamiento.
3. `PAYMENT_REPORTED`: el banco o billetera informó el débito.
4. `OFFICIALLY_POSTED`: CCMA lo imputó correctamente.
5. `DISCREPANCY`: existe el pago, pero continúa la deuda o se imputó a otro período.

Sólo `OFFICIALLY_POSTED` permite informar ese período como cancelado.

## 4. Facturación acumulada y proyección de categoría

El bot no emitirá facturas. Sólo consolidará comprobantes ya emitidos para calcular:

- total por mes;
- total móvil de los últimos doce meses;
- notas de crédito y débito;
- fecha de corte y meses incompletos;
- categoría que correspondería con la tabla vigente;
- margen en pesos y porcentaje hasta la categoría inmediata superior o inferior;
- proyección a la próxima recategorización.

### Fuente de comprobantes

El usuario actualmente utiliza Comprobantes en Línea. No debe asumirse que `FECompConsultar` de WSFEv1 puede recuperar comprobantes de un punto de venta perteneciente a otro sistema: ARCA exige puntos de venta distintos para cada modalidad.

Orden de preferencia para el MVP:

1. Exportación o consulta asistida de comprobantes emitidos desde el servicio oficial.
2. Importación de archivo oficial si el portal ofrece una descarga reutilizable.
3. Carga manual del total mensual, marcada como `MANUAL_UNVERIFIED`, hasta la próxima sincronización.

El cálculo debe usar importes efectivamente computables y descontar notas de crédito. No se mezclan presupuestos, transferencias ni pagos sin comprobante fiscal.

### Estado actual y simulación

Datos confirmados al 04/08/2026:

- categoría nacional: **F**, servicios, desde 08/2026;
- límite vigente de F: **$45.151.659,41**;
- límite vigente de E: **$36.028.231,33**;
- facturación futura informada: **$3.000.000 mensuales hasta diciembre de 2026**.

Una actividad estable de $3.000.000 durante doce meses totaliza $36.000.000. Con la tabla nacional vigente al 01/08/2026, eso quedaría apenas dentro de E, con sólo $28.231,33 de margen. Es una **simulación**, no una recomendación de baja: faltan los comprobantes reales de los doce meses que correspondan a cada corte y las tablas pueden actualizarse antes de febrero de 2027.

El semáforo propuesto:

- 🟢 margen mayor al 10% del límite;
- 🟡 margen entre 3% y 10%;
- 🔴 margen menor al 3% o datos incompletos cerca del límite.

Con $36.000.000 y la tabla actual, el resultado es rojo por proximidad al límite, aunque todavía encuadre en E.

## 5. Otras deudas con ARCA

Para el perfil actual, el control nacional tendrá dos capas:

1. CCMA para Monotributo.
2. Sistema de Cuentas Tributarias para cualquier otro impuesto u obligación que aparezca registrado.

La bandeja DFE agrega alertas sobre intimaciones y diferencias, pero no sustituye la cuenta corriente. Tampoco se considerará prueba suficiente una constancia genérica de inscripción.

No se identificó una API pública para consultar toda la deuda de un contribuyente. El servicio de deuda publicado por ARCA para terceros está restringido a entidades habilitadas. Esta parte seguirá siendo una consulta asistida y mostrará la fecha de la última sesión oficial.

## 6. Deuda de Ingresos Brutos Santa Fe

Fuentes oficiales:

- **VEPAI:** pagos registrados por API Santa Fe.
- **Régimen Simplificado:** categoría, cuotas y boletas/VEP.
- **LDAE:** deuda de varios períodos y liquidación consolidada.
- **IBINT:** intereses aislados a una fecha de pago.

VEPAI muestra pagos, no garantiza por sí solo que no exista una cuota omitida. El bot debe construir los períodos esperados y conciliarlos con VEPAI:

```text
obligaciones esperadas − pagos imputados = períodos presuntamente adeudados
```

Si falta un período, el importe exacto de capital e intereses se confirmará en Régimen Simplificado, LDAE o IBINT antes de sugerir cualquier pago.

Datos confirmados al 04/08/2026:

- contribuyente local adherido al Régimen Simplificado;
- categoría provincial **3**;
- escala del segundo semestre 2026: de $24.670.494,32 a $36.028.231,33;
- cuota mensual: **$42.882**;
- liquidación consolidada informada como pagada el 04/08/2026: **$672.973,38**;
- al último control, la imputación en VEPAI todavía estaba pendiente.

Una facturación estable de $36.000.000 durante doce meses también quedaría apenas dentro de la categoría provincial 3 según la tabla actual, con $28.231,33 de margen. Debe usarse el total real gravado y la tabla vigente en cada revisión.

## Comandos del MVP

| Comando | Resultado |
|---|---|
| `/estado` | Los seis controles, semáforos, fuente y fecha de actualización |
| `/bandeja` | Resúmenes no leídos del DFE sin abrir comunicaciones |
| `/monotributo` | Categoría, cuota, períodos y conciliación CCMA |
| `/facturacion` | Total mensual, últimos doce meses y calidad de los datos |
| `/categoria` | Proyección nacional y provincial, márgenes y próximos cortes |
| `/deuda_arca` | CCMA y demás obligaciones oficiales sincronizadas |
| `/iibb` | Categoría, cuotas esperadas y conciliación VEPAI |
| `/vencimientos` | Próximas revisiones y pagos mensuales |
| `/sincronizar` | Inicia una consulta local asistida a los portales oficiales |
| `/registrar_pago` | Adjunta evidencia sin afirmar imputación oficial |

No existirán `/facturar`, `/recategorizar` ni comandos que ejecuten pagos.

## Arquitectura

```text
Telegram
   |
   v
Bot determinístico -------> SQLite
   |                         |-- obligaciones y pagos
   |                         |-- comprobantes emitidos
   |                         |-- comunicaciones resumidas
   |                         `-- historial de reglas
   |
   |-- fuentes públicas: categorías, calendarios y prórrogas
   |-- WSAA + WSCCOMU: resúmenes del DFE
   `-- sincronizador local: CCMA, Cuentas Tributarias, RCEL y VEPAI
```

### Uso de las URLs y sesiones observadas

Para los portales sin API pública se probará este orden:

1. **Solicitud directa autenticada:** identificar la petición real de la página y repetirla dentro del perfil local que ya posee cookies, token anti-CSRF y demás datos temporales.
2. **Navegación local automatizada:** si el servidor exige pasos previos, formularios encadenados o tokens rotativos, ejecutarlos en un navegador local persistente y extraer el resultado.
3. **Lectura del DOM:** usar los datos visibles como último recurso cuando no exista una respuesta estructurada reutilizable.

Las URLs anotadas sirven como punto de partida, pero una URL por sí sola no alcanza: CCMA contiene formularios ASP heredados y los aplicativos pueden validar cookie de sesión, encabezados, token anti-CSRF, origen y secuencia de navegación. Antes de implementar se capturará una sesión de red autorizada para documentar método, parámetros, respuesta, vencimiento y dependencia de cada token.

Este acceso no se presentará como una API oficial. Es una integración web privada y puede cambiar sin aviso. Por seguridad, las solicitudes se ejecutarán en la computadora del contribuyente; el bot recibirá únicamente el resumen tributario resultante.

### Modelo mínimo de datos

`tax_obligations`

- jurisdicción, concepto, período y vencimiento;
- importe esperado y oficial;
- estado y referencia externa;
- fuente y fecha de observación.

`issued_receipts`

- fecha, tipo, punto de venta y número;
- importe, moneda y ajustes;
- origen del dato y fecha de sincronización;
- hash único para evitar duplicados.

`tax_messages`

- identificador WSCCOMU;
- fecha de publicación y vencimiento;
- organismo, asunto, prioridad y estado;
- indicador de adjuntos;
- fecha en que el usuario ordenó abrirlo, si ocurrió.

`tax_rules`

- jurisdicción y categoría;
- límites desde/hasta, cuota y vigencia;
- URL oficial y fecha de lectura.

`audit_log`

- fecha, actor, acción, resultado y hash de solicitud.

## Seguridad y límites operativos

1. La clave fiscal no se almacena ni se envía por Telegram.
2. Las consultas automáticas oficiales usarán certificado limitado al servicio necesario.
3. El bot acepta un único `chat_id` autorizado.
4. El webhook valida el secreto de Telegram.
5. El token y las claves privadas permanecen fuera de Git y cifrados en reposo.
6. El bot no abre mensajes DFE automáticamente.
7. El bot no presenta recategorizaciones ni ejecuta pagos.
8. Una deuda estimada nunca se muestra como deuda oficial.
9. Un pago bancario no se muestra como imputado hasta verlo en CCMA o VEPAI.
10. Toda respuesta sensible incluye fuente y antigüedad del dato.

## Fases recomendadas

### Fase 1 — tablero y calendario

- Crear bot para un único usuario.
- Cargar reglas vigentes y fechas de recategorización.
- Implementar `/estado`, `/vencimientos`, `/categoria` y registro de pagos.
- Importar el historial ya documentado en este proyecto.

### Fase 2 — bandeja ARCA

- Crear certificado y asociación a `veconsumerws`.
- Consultar únicamente resúmenes mediante `consultarComunicaciones`.
- Alertar mensajes nuevos sin marcarlos como leídos.
- Implementar apertura con doble confirmación.

### Fase 3 — facturación acumulada

- Sincronizar comprobantes emitidos en Comprobantes en Línea.
- Calcular ventanas móviles de doce meses.
- Proyectar categoría nacional y provincial con reglas versionadas.
- Alertar datos faltantes y proximidad a límites.

### Fase 4 — conciliación de deuda

- Crear sincronizador local para sesiones iniciadas por el contribuyente.
- Capturar y documentar las solicitudes de CCMA, Cuentas Tributarias y VEPAI.
- Reutilizar cookies y tokens sólo dentro del proceso local y durante su vigencia.
- Priorizar peticiones HTTP directas; usar navegación automatizada como respaldo.
- Consultar CCMA, Sistema de Cuentas Tributarias y VEPAI.
- Comparar obligaciones esperadas, pagos informados e imputaciones oficiales.
- Conservar evidencia sin guardar cookies ni credenciales.

## Criterios de aceptación

1. `/estado` cubre los seis controles solicitados.
2. Cada importe muestra fuente y fecha de lectura.
3. El total de doce meses identifica meses incompletos o datos manuales.
4. La categoría proyectada distingue resultado real de simulación.
5. Ninguna consulta automática marca un mensaje DFE como leído.
6. El bot nunca afirma “sin deuda” con una fuente oficial vencida.
7. Los recordatorios de recategorización respetan prórrogas versionadas.
8. No se guardan clave fiscal, cookies, datos bancarios ni tokens en logs.
9. Un pago no se repite mientras su imputación esté pendiente.
10. No existe capacidad de emitir facturas, presentar declaraciones ni pagar.

## Tecnología propuesta, pendiente de aprobación

- Backend: Node.js con TypeScript.
- Persistencia inicial: SQLite con backup.
- Planificador: cron.
- Telegram: webhook HTTPS.
- Sincronizador asistido: proceso local en la computadora del contribuyente.
- Despliegue: contenedor pequeño en infraestructura propia.

No se agregará ninguna dependencia sin aprobación previa.

## Fuentes oficiales

### ARCA

- [Recategorización del Monotributo](https://www.arca.gob.ar/monotributo/ayuda/recategorizacion.asp)
- [Categorías vigentes](https://arca.gob.ar/monotributo/categorias.asp)
- [Domicilio Fiscal Electrónico y efectos de las notificaciones](https://arca.gob.ar/DomicilioFiscalElectronico/notificaciones/default.asp)
- [Catálogo de web services](https://www.arca.gob.ar/ws/documentacion/catalogo.asp)
- [Manual WSCCOMU 1.4.0](https://www.arca.gob.ar/ws/WSCComu/vecuwsconcomunicaciones.pdf)
- [WSAA y certificados](https://arca.gob.ar/ws/documentacion/wsaa.asp)
- [Pagos y consulta mediante CCMA](https://www.arca.gob.ar/monotributo/ayuda/pagos.asp)
- [Modalidades de facturación del Monotributo](https://arca.gob.ar/facturacion/monotributo/modalidades.asp)
- [Puntos de venta y autorización electrónica](https://www.arca.gob.ar/fe/emision-autorizacion/solicitud-autorizacion.asp)

### Santa Fe

- [Régimen Simplificado y escala del segundo semestre 2026](https://www.santafe.gob.ar/index.php/web/content/view/full/211373)
- [Preguntas frecuentes](https://www.santafe.gob.ar/index.php/web/content/view/full/224945/%28subtema%29/)
- [Resolución General API 43/2024](https://www.santafe.gob.ar/boletinoficial/ver.php?seccion=2025%2F2025-01-21avisos.html)
- [VEPAI](https://www.santafe.gob.ar/index.php/tramites/modul1/index?id=97354&m=descripcion)
- [LDAE](https://www.santafe.gob.ar/index.php/tramites/modul1/index?id=97267&m=descripcion)
- [IBINT](https://www.santafe.gov.ar/e-ibint/index.php)

### Telegram

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Webhooks](https://core.telegram.org/bots/webhooks)
