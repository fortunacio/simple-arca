# Procedimientos y posibilidades de automatización de ARCA

## Objetivo y criterio de seguridad

Este documento registra procedimientos reproducibles, URLs relevantes, datos de entrada y salida y alternativas de automatización observadas durante la gestión tributaria. Fecha de inicio: **04/08/2026**.

No se guardan:

- clave fiscal, contraseña, códigos de autenticación o cookies de sesión;
- certificados privados, claves privadas, token/sign de WSAA ni JWT;
- CBU, credenciales bancarias o datos completos de medios de pago.

Toda operación que emita un comprobante, presente una declaración, genere un plan o produzca un pago debe tener una instancia de revisión humana del importe y los datos antes de confirmarse.

## Matriz de automatización verificada

| Necesidad | Alternativa oficial | Acceso para este contribuyente | Decisión técnica inicial |
|---|---|---|---|
| Emitir facturas C | `WSFEv1` | Sí, con certificado X.509 y relación al servicio `wsfe` | Automatizable por API oficial |
| Consultar último comprobante | `FECompUltimoAutorizado` de `WSFEv1` | Sí | Usar antes de emitir para conservar correlatividad |
| Consultar una factura/CAE | `FECompConsultar` de `WSFEv1` | Sí | Usar para idempotencia y recuperación ante timeouts |
| Consultar deuda propia de Monotributo | No se encontró WS público de CCMA | No por API oficial general | Consultar CCMA mediante sesión interactiva o exportación; no guardar clave fiscal |
| Consultar deuda por CUIT (`sud_restricciones`) | WS oficial | Reservado a entidades bancarias | No utilizable por un bot personal |
| Crear y consultar VEP (`SETIWS-PAGO-API`) | API REST oficial | Uso exclusivo de organismos con convenio vigente con ARCA | No utilizable directamente por un contribuyente individual |
| Presentar DDJJ | WS oficial de Presentación de DDJJ | Existe, pero debe validarse por formulario/impuesto | Evaluar sólo si luego se automatizan DDJJ específicas |
| Consultar inscripción/impuestos | WS Padrón A4 / Constancia de Inscripción | Posible con autorización correspondiente | Útil para configuración y controles del bot, no reemplaza CCMA |
| Generar cuota/VEP de IIBB Santa Fe | Formulario 5426 del Régimen Simplificado | Sí, mediante la relación ya habilitada | Automatizado con confirmación humana; el pago bancario queda fuera del bot |

## Arquitectura oficial para facturación

1. Generar un par de claves y solicitar un certificado digital X.509 de producción mediante `Administrador de Certificados Digitales`.
2. Asociar el certificado al Web Service de negocio de Facturación Electrónica (`wsfe`).
3. Solicitar a WSAA un Ticket de Acceso para el servicio `wsfe`. El TA contiene `Token` y `Sign` y actualmente tiene una duración limitada de aproximadamente 12 horas.
4. Consultar `FECompUltimoAutorizado` para el punto de venta y tipo de comprobante.
5. Enviar `FECAESolicitar` con el siguiente número correlativo.
6. Ante timeout, consultar `FECompConsultar` antes de reintentar, para evitar duplicidad o error de correlatividad.
7. Guardar CAE, vencimiento del CAE, punto de venta, tipo y número de comprobante como resultado inmutable de la emisión.

Endpoints y documentación oficial:

- WSAA homologación: `https://wsaahomo.afip.gov.ar/ws/services/LoginCms`
- WSAA producción: `https://wsaa.afip.gov.ar/ws/services/LoginCms`
- WSFEv1 homologación: `https://wswhomo.afip.gov.ar/wsfev1/service.asmx`
- Manual vigente de WSFEv1: `https://www.arca.gob.ar/ws/documentacion/manuales/manual-desarrollador-ARCA-COMPG.pdf`
- Documentación WSAA: `https://arca.gob.ar/ws/documentacion/wsaa.asp`
- Arquitectura general: `https://www.arca.gob.ar/ws/documentacion/`
- Ayuda de Factura Electrónica: `https://www.arca.gob.ar/fe/ayuda/webservice.asp`

Para producción se debe confirmar la URL exacta del WSFEv1 en el manual vigente al momento de implementar. No se debe reutilizar la clave fiscal como credencial del bot: la integración correcta usa certificado y WSAA.

### Flujo implementado para Factura C

- El punto de venta configurado en `ARCA_WSFE_POINT_OF_SALE` debe ser exclusivo de
  Web Services y distinto de `00001`.
- Sólo pueden emitir los chats incluidos en `INVOICE_EMISSION_CHAT_IDS`.
- `Repetir` crea un borrador editable; nunca solicita un CAE directamente.
- Al confirmar, el borrador se bloquea antes de consultar el último número y enviar
  `FECAESolicitar`.
- Si la respuesta es incierta, se consulta el número pretendido con
  `FECompConsultar`; no se reintenta la emisión sin conocer primero el resultado.
- El registro autorizado es inmutable y constituye la fuente de verdad. El PDF
  Original se regenera desde ese registro e incluye el QR fiscal de la RG 4892.
- La unicidad se aplica por CUIT emisor, tipo, punto de venta y número. Además se
  advierte si coincide cliente, período e importe con una factura ya registrada.
- La primera factura de producción requiere revisión expresa del borrador en
  Telegram. La puesta en marcha y las pruebas de lectura no emiten comprobantes.

### Alta y validación de producción — 05/08/2026

- Punto creado: `00002`.
- Nombre de fantasía: `SIMPLE ARCA`.
- Sistema: `Factura Electrónica - Monotributo - Web Services`.
- Domicilio asociado: domicilio fiscal vigente informado por ARCA.
- `FEParamGetPtosVenta`: `CAE - Monotributo`, no bloqueado y sin fecha de baja.
- `FECompUltimoAutorizado`, Factura C código 011: `0`.
- `FEParamGetActividades`: actividad principal `639900 - Servicios de información
  n.c.p.`.
- Condiciones IVA de Factura C y receptor Concept Design S.A. validados en
  producción.
- No se solicitó ningún CAE durante el alta ni durante estas pruebas.

## APIs de deuda y VEP: limitaciones oficiales

El catálogo oficial de ARCA (`https://www.arca.gob.ar/ws/documentacion/catalogo.asp`) informa:

- `sud_restricciones` permite consultar si un CUIT tiene deuda, pero su uso está **reservado a entidades bancarias**. Además, la descripción pública no promete el detalle mensual y por componente que entrega CCMA.
- `SETIWS-PAGO-API` reemplaza al antiguo `WSCREATEVEP`. Es una API REST para crear VEP, enviarlo a una entidad de pago y consultar su comprobante de pago.
- El manual versión 1.1.0 de `SETIWS-PAGO-API` indica que es de uso exclusivo de **organismos con convenio de intercambio de información vigente con ARCA**. Por eso no constituye una API de autoservicio para un monotributista.

Referencia técnica de `SETIWS-PAGO-API`, sólo para evaluar su alcance:

- Producción: `https://setiws-pago-api.arca.gob.ar/`
- Homologación: `https://seti-setipago-api-qaext.arca.gob.ar/`
- Crear VEP: `POST /api/v1/veps`
- Consultar VEP/comprobante de pago: `GET /api/v1/veps`
- Autenticación: JWT de WSAUTH y cabecera `WSAA-AUTH-PROXY-REPRESENTADO`
- Manual: `https://www.arca.gob.ar/ws/SETIWS-PAGO-API/Manual_para_el_desarrollador_setiws-pago-api.pdf`

Estos endpoints no deben probarse con credenciales personales sin que ARCA haya otorgado la relación y el convenio requeridos.

## VEP de Ingresos Brutos de Santa Fe

El Régimen Simplificado provincial expone el Formulario 5426 desde
`https://www.santafe.gob.ar/e-regsim/index.php?action=Registro::boletasPago`. La
aplicación oficial permite enviar el VEP a:

- `1001`: Red Link;
- `1002`: Pago Mis Cuentas;
- `1003`: Interbanking.

La integración implementada usa la misma sesión autenticada que las consultas de
Santa Fe y conserva este orden:

1. consultar el período con `Registro::actualizarPeriodoVep`;
2. calcular los intereses a la fecha con `Registro::actualizarInteres`;
3. guardar la cotización y solicitar confirmación en Telegram;
4. volver a cotizar en la misma sesión antes de generar;
5. si no cambió, enviar una sola vez a `Registro::liquidar_VEP`;
6. guardar el número y el PDF de `ImpresionReportes::imprimirVEP`;
7. abrir `https://seti.afip.gob.ar/setiweb/#/pago/veps-a-enviar` para que el usuario
   confirme el pago, incluso mediante QR;
8. considerar el período pagado únicamente cuando Santa Fe lo refleje.

Los parámetros de generación verificados son `nro_iibb`, `id_entidad`, `periodo`,
`importe`, `fe_vencimi`, `fe_pago` e `interes`. Estas rutas son formularios web del
organismo, no una API pública con contrato estable; por eso se monitorean los errores
y no se interpretan respuestas ambiguas como éxito.

Controles de seguridad e idempotencia:

- el bot no posee la clave fiscal; el trabajo lo ejecuta `portal-worker`;
- sólo pueden generar los chats de `IIBB_PAYMENT_CHAT_IDS`;
- una cotización modificada exige una segunda confirmación;
- doble pulsación y concurrencia se bloquean por cuenta y período;
- un timeout o reinicio durante el envío queda `UNKNOWN` y nunca se reintenta solo;
- generar un VEP no debita fondos;
- no se guardan CBU, tarjetas, claves bancarias ni tokens de billeteras.

La documentación de ARCA sobre billeteras virtuales indica que un VEP ya generado
puede pagarse por QR aunque al crearlo se haya elegido otra entidad:
`https://www.afip.gob.ar/comoPagarImpuestos/documentos/billeteras-virtuales.pdf`.
El MVP abre el flujo oficial; no intenta fabricar ni extraer un QR sin una interfaz
pública documentada.

### Información posterior según la red

- Para Santander se utiliza `1002` — Pago Mis Cuentas. Santander indica `Transacciones
  → Pago de Servicios → Nuevo pago`, buscar `VEP` e informar CUIT contribuyente y CUIT
  generador. También advierte que puede tardar al menos tres horas y no siempre aparece
  en el calendario: `https://ayuda.santander.com.ar/3WR6AZDV3B-1/article/KFCQ44ND0R-que-es-vep-como-pagarlo/`.
- Para Red Link sólo se informa el procedimiento general de ARCA y se aclara que el
  menú depende del banco. No se atribuye a todos los bancos una ruta que no fue
  verificada: `https://serviciosweb.arca.gob.ar/genericos/guiasPasoPaso/VerGuia.aspx?id=66`.
- Para Interbanking se informa únicamente lo publicado: el VEP se autoriza en la
  plataforma y el corte para procesamiento en el mismo día hábil es a las 16:00. Si
  vence ese día y se envía después, debe generarse otro:
  `https://prehome.interbanking.com.ar/nuevo-horario`.

## Procedimiento de pago de deuda de Monotributo — ejecución 04/08/2026

### Alcance autorizado

- Regularizar únicamente la deuda nacional de Monotributo ya verificada en CCMA.
- Períodos conocidos antes del recálculo: **02/2026 y 03/2026**.
- Origen: diferencias entre la categoría F debitada automáticamente y la categoría G vigente para esos meses, más intereses.
- Último saldo observado antes de esta ejecución: **$154.843,13**, sujeto a recálculo al día.
- Crédito separado observado en CCMA: **$1.041,22** de 06/2024. No imputar ni compensar sin revisar el efecto y obtener autorización específica.

### Registro de navegación

| Paso | URL o servicio | Entrada | Salida/validación | Automatización posible |
|---|---|---|---|---|
| 1 | Portal de Clave Fiscal: `https://portalcf.cloud.afip.gob.ar/portal/app/` | Inicio de sesión realizado por el contribuyente | Sesión autenticada | Mantener inicio humano; no almacenar contraseña |
| 2 | `CCMA - Cuenta corriente de monotributistas y autónomos`: `https://servicios2.afip.gob.ar/tramites_con_clave_fiscal/ccam/P02_ctacte.asp` | CUIT representado | Formulario de recálculo | Sin API pública identificada |
| 3 | Resultado del recálculo: `https://servicios2.afip.gob.ar/tramites_con_clave_fiscal/ccam/P04_ctacte.asp` | Desde `02/2026`, hasta `03/2026`, fecha `04/08/2026` | Deuda $154.843,13 | Formulario HTML interno; no API soportada |
| 4 | Selección en CCMA: `https://servicios2.afip.gob.ar/tramites_con_clave_fiscal/ccam/VolPag_ctacte.asp` | Seis capitales y seis intereses | 12 conceptos seleccionados | Página heredada que entrega el flujo a SETI |
| 5 | Selección de entidad: `https://seti.afip.gob.ar/setiweb/#/pago/veps-a-enviar` | 12 conceptos, total $154.843,13 | ARCA-QR confirmado | La API pública de VEP no está habilitada para contribuyentes individuales |
| 6 | Pago del VEP 1661690467 | Escaneo del QR y acción del contribuyente en banco/billetera | Pagado vía Mercado Pago el 04/08/2026 15:11:23 | No almacenar credenciales bancarias |
| 7 | Consulta SETI: `https://seti.afip.gob.ar/setiweb/#/pago/consulta-veps` | Filtro por VEP `1661690467` | VEP consolidado y 12 sub-VEP en estado `Pagado` | Flujo UI autenticado; no API pública para contribuyente individual |
| 8 | Reconsulta CCMA a las 15:19 | Recálculo 02/2026–03/2026 al 04/08/2026 | Aún muestra $154.843,13: imputación pendiente | Reconsultar; no generar otro VEP |

### Evidencias a capturar durante la ejecución

- URL exacta de CCMA y de cada pantalla relevante.
- Período, impuesto/concepto/subconcepto, capital, intereses y total actualizado.
- Existencia de opciones de reimputación, pago parcial, selección de períodos o cálculo a una fecha.
- Número de VEP, vencimiento y entidad de pago, si se genera.
- Mensaje de confirmación y fecha/hora de generación.
- Estado posterior en CCMA, respetando que la conciliación bancaria puede demorar.

No se copiarán cookies, tokens de sesión ni solicitudes internas no documentadas como API pública. Las llamadas internas de una aplicación web pueden cambiar sin aviso y no constituyen por sí mismas una API soportada.

### Recálculo verificado el 04/08/2026 a las 15:04

Campos HTML visibles del formulario `P02_ctacte.asp`:

| Campo | `name` HTML | Valor usado |
|---|---|---|
| Período desde | `perdesde2` | `02/2026` |
| Período hasta | `perhasta2` | `03/2026` |
| Fecha de cálculo | `feccalculo` | `04/08/2026` |
| Acción | `CalDeud` | `CALCULO DE DEUDA` |

Resultado:

| Período | Impuesto 020 | Impuesto 021 | Impuesto 024 | Total período |
|---|---:|---:|---:|---:|
| 02/2026 | $63.473,74 | $11.581,58 | $3.302,11 | **$78.357,43** |
| 03/2026 | $61.957,54 | $11.304,93 | $3.223,23 | **$76.485,70** |
| **Total** |  |  |  | **$154.843,13** |

Composición total:

- diferencias de obligación mensual: **$136.125,82**;
- intereses resarcitorios calculados al 04/08/2026: **$18.717,31**;
- total a regularizar: **$154.843,13**.

Los códigos visibles son:

- impuesto `020`: componente impositivo de Monotributo;
- impuesto `021`: aporte previsional;
- impuesto `024`: obra social;
- concepto `019`, subconcepto `019`: obligación mensual;
- concepto `019`, subconcepto `051`: intereses resarcitorios.

Advertencia: el encabezado de esta consulta muestra `Total Saldo Acreedor: 197.108,23`, equivalente a los pagos de categoría F registrados en febrero y marzo. No es un crédito libre adicional: esos pagos ya están aplicados en las filas para obtener la diferencia de $154.843,13.

### Preparación del VEP

La acción `VOLANTE DE PAGO` de `P04_ctacte.asp` abre `VolPag_ctacte.asp` y muestra dos grupos seleccionables:

- `MONOTRIBUTO - OBLIGACIONES`: seis diferencias de capital, una por impuesto 020/021/024 para cada período 02/2026 y 03/2026;
- `MONOTRIBUTO - INTERESES`: seis accesorios con subconcepto 051 para los mismos impuestos y períodos.

Se seleccionaron los doce conceptos. Los totales continuaron siendo:

- capital: $136.125,82;
- intereses: $18.717,31;
- total: **$154.843,13**.

La acción visible `GENERAR VEP O QR` no produjo todavía un pago: transfirió la selección al frontend moderno de SETI, en `https://seti.afip.gob.ar/setiweb/#/pago/veps-a-enviar`.

La pantalla de SETI confirma `12 seleccionadas` y el total `154.843,13`. Ofrece enviar los VEP a:

1. ARCA-QR;
2. Red Link;
3. Banelco / PagoMisCuentas;
4. Interbanking;
5. XNATIVA.

La selección de una entidad es el punto en el que los VEP son enviados al canal de pago. Debe elegirse con autorización del contribuyente. La existencia de este frontend no habilita a utilizar su API interna: el servicio público documentado `SETIWS-PAGO-API` continúa siendo exclusivo para organismos con convenio.

### VEP generado y enviado a ARCA-QR

Con autorización del contribuyente se eligió `ARCA-QR` y se aceptó el diálogo `¿Querés enviar el VEP a ARCA-QR?`.

Confirmación visible de SETI:

- mensaje: `Enviaste el VEP a la entidad de pago.`;
- número de VEP: **1661690467**;
- descripción: `VCON#12`;
- importe: **$154.843,13**;
- cantidad de conceptos consolidados: 12;
- medio: ARCA-QR;
- estado al registrar: **pendiente de pago bancario**.

SETI presentó un único código QR para el VEP consolidado. El pago debe completarlo el contribuyente escaneando el QR desde una billetera o aplicación bancaria habilitada. Después se debe consultar nuevamente el VEP y CCMA: la generación y envío a ARCA-QR no equivalen a pago acreditado.

### Verificación posterior del pago

Con una sesión nueva se consultó `https://seti.afip.gob.ar/setiweb/#/pago/consulta-veps` filtrando por el número de VEP. SETI devolvió:

- estado: **Pagado**;
- enviado a: **Mercado Pago**;
- VEP consolidado: **1661690467**;
- descripción: `VCON#12`;
- importe: **$154.843,13**;
- fecha de pago: **04/08/2026 15:11:23**.

El detalle expandido mostró los 12 sub-VEP también en estado `Pagado`: 1661690468 ($6.822,91), 1661690469 ($1.244,93), 1661690500 ($354,95), 1661690501 ($8.339,11), 1661690502 ($1.521,58), 1661690503 ($433,83), 1661690504 ($2.868,28), 1661690505 ($10.060,00), 1661690506 ($55.134,63), 1661690507 ($2.868,28), 1661690508 ($10.060,00) y 1661690509 ($55.134,63). La suma coincide con el total consolidado.

A las 15:19 se recalculó CCMA para 02/2026–03/2026 con fecha 04/08/2026. Todavía mostró `Total Saldo Deudor: 154.843,13`, con capital $136.125,82 e intereses $18.717,31. Como SETI ya confirmó el pago apenas ocho minutos antes, se registra como **VEP pagado con imputación pendiente en CCMA**. No se generó un segundo VEP. La próxima acción segura es volver a consultar CCMA; sólo cuando el saldo deudor desaparezca o el pago figure en los movimientos corresponde dar la deuda por conciliada.

## Bot de Telegram implementado

La interfaz vigente muestra `/resumen`, `/facturas` y `/ayuda`. Las consultas de
cuenta, pagos e Ingresos Brutos se renuevan automáticamente con credenciales cifradas;
la clave fiscal se carga mediante un formulario privado y nunca pasa por Telegram.

La facturación prepara un borrador editable y exige confirmación explícita antes de
solicitar CAE por WSFEv1. La aplicación conserva número, CAE, respuesta y auditoría para
resolver timeouts sin duplicar comprobantes.

El bot puede cotizar y generar el VEP de la cuota corriente del Régimen Simplificado
de Santa Fe después de una confirmación explícita. No opera cuentas bancarias ni
confirma el débito: esa autorización continúa en ARCA, la billetera o el banco. Cuando
detecta un pago confirmado que todavía no terminó de reflejarse en la cuenta, informa
que no debe repetirse y suprime la alerta contradictoria de deuda para ese saldo.

Controles implementados:

- lista cerrada de chats y listas más restrictivas para emitir facturas o generar VEP;
- certificados y claves fuera del repositorio;
- cifrado de credenciales y tickets;
- registro de auditoría sin tokens ni datos bancarios;
- idempotencia de facturación;
- avisos deduplicados y control de fuentes atrasadas;
- confirmación humana para toda emisión fiscal irreversible.

La operación actual está documentada en `README.md`, `BOT_TELEGRAM_TRIBUTARIO.md` y
`DEPLOY_COOLIFY.md`.
