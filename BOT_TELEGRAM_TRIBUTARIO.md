# Bot tributario por Telegram

## Interfaz vigente

El menú de Telegram muestra exactamente tres comandos:

| Comando | Función |
|---|---|
| `/resumen` | Explica qué requiere atención y qué hay que pagar |
| `/facturas` | Lista facturas, permite descargar, repetir y emitir |
| `/ayuda` | Explica el alcance y ofrece la configuración privada |

`/start` abre el resumen cuando la cuenta ya está vinculada. Los comandos técnicos
anteriores se mantienen ocultos para diagnóstico y compatibilidad.

## Resumen

El resumen usa lenguaje cotidiano y no muestra nombres internos de los portales. Se
limita a decisiones:

- próximo pago de Monotributo e Ingresos Brutos, cada uno con fecha e importe;
- deuda vencida sin un pago confirmado que la explique;
- pagos confirmados que todavía no terminaron de aparecer en la cuenta;
- mensajes nuevos de ARCA;
- riesgo de subir o posibilidad de bajar de categoría;
- fuentes que llevan demasiado tiempo sin actualizarse.

Como todavía no se consultan todos los impuestos nacionales, dice “Con lo que hoy
puedo revisar” y no afirma que la persona esté libre de cualquier deuda con ARCA.

Botones del resumen:

- `Pagos y vencimientos`
- `Categoría y facturación`
- `Mensajes de ARCA`
- `Facturas`

La navegación edita el mismo mensaje para no llenar el chat y valida que cada callback
pertenezca a un chat autorizado.

## Pagos y vencimientos

La pantalla unificada muestra:

- qué pagar, cuánto y qué día;
- último pago confirmado de Monotributo;
- último pago de Ingresos Brutos registrado por Santa Fe;
- deuda vencida que realmente requiere atención;
- pagos que todavía están terminando de reflejarse.

Regla de conciliación:

- Si existe un pago confirmado relacionado con el saldo, ese saldo no se presenta
  como una deuda nueva.
- Durante los primeros siete días se informa que la cuenta todavía se actualiza y se
  indica no repetir el pago.
- Después de siete días se informa que el saldo requiere revisión, pero tampoco se
  sugiere pagarlo de nuevo.
- Una deuda sin pago confirmado relacionado sí se muestra como deuda vencida.

Los nombres técnicos de las fuentes sólo aparecen en comandos ocultos de auditoría.

### Pagar Ingresos Brutos

`Pagos y vencimientos` ofrece `Pagar Ingresos Brutos` cuando Santa Fe informó una
cuota pendiente. El flujo es:

1. elegir Pago Mis Cuentas, Red Link o Interbanking;
2. esperar la cotización oficial del capital y los intereses para la fecha actual;
3. revisar período, vencimiento, capital, intereses, total y red;
4. pulsar `Generar VEP` una sola vez;
5. descargar el PDF oficial y abrir ARCA para completar el pago, incluido QR.

Después de elegir la red, la revisión explica también el paso posterior con el nivel
de precisión publicado por cada operador:

- `Santander / Pago Mis Cuentas`: indica el recorrido de la app, los CUIT solicitados,
  la posible demora de tres horas y que el VEP puede requerir `Nuevo pago` aunque no
  aparezca en el calendario. Fuente: [Centro de ayuda Santander](https://ayuda.santander.com.ar/3WR6AZDV3B-1/article/KFCQ44ND0R-que-es-vep-como-pagarlo/).
- `Red Link`: indica ingresar al home banking adherido y buscar VEP/ARCA, aclarando
  que el menú exacto depende del banco y evitando nombres de menú no verificados.
  Fuente general: [guía de VEP de ARCA](https://serviciosweb.arca.gob.ar/genericos/guiasPasoPaso/VerGuia.aspx?id=66).
- `Interbanking`: indica localizar y autorizar el VEP y advierte el corte de las
  16:00 para procesamiento en el día. Fuente: [información operativa de Interbanking](https://prehome.interbanking.com.ar/nuevo-horario).

Una vez generado, el mensaje conserva esas instrucciones, enlaza la fuente oficial y
ofrece por separado abrir ARCA para pagar con QR. La guía oficial de [E-Pagos de
ARCA](https://serviciosweb.arca.gob.ar/genericos/guiasPasoPaso/VerGuia.aspx?id=479)
confirma que el VEP debe existir previamente y que luego se selecciona `Pago con QR`.

La generación se ejecuta en `portal-worker`, donde está la clave fiscal cifrada. El
bot de Telegram sólo intercambia trabajos por la base compartida y nunca recibe esa
clave. Una caída o timeout después de enviar la solicitud deja el resultado como
incierto y bloquea reintentos automáticos. Si el interés cambia entre la revisión y la
confirmación, el bot muestra la nueva cotización y exige otra confirmación.

El bot no considera pagado un VEP por haberlo generado ni por haber abierto ARCA. El
estado cambia a pagado únicamente cuando la consulta posterior de Santa Fe registra
el período. No se almacenan datos bancarios.

## Categoría y facturación

Combina la categoría oficial actual con:

- total registrado en los últimos doce meses, combinando un historial mensual
  verificado, las facturas guardadas por el bot y las consultas individuales de WSFE
  sin duplicarlas;
- promedio mensual de los meses registrados, indicando cuántos meses respaldan el
  cálculo cuando el historial todavía es parcial;
- categoría proyectada con la tabla vigente obtenida desde ARCA;
- próxima fecha de revisión.

Si falta parte del historial de otro punto de venta, muestra el total conocido como
`facturación registrada`. Si falta siquiera un mes ya cerrado dentro del período que
evaluará la próxima recategorización, no asigna una categoría proyectada: indica qué
meses faltan y cuántos quedan por transcurrir.

Cuando todos los meses cerrados están cubiertos, la proyección se calcula como:

`facturación acumulada + promedio real de los meses cerrados × meses por cerrar`.

El promedio sale únicamente de facturación ya registrada. El sistema no guarda
importes futuros, presupuestos ni una facturación mensual “prevista”. La comparación
usa la última tabla oficial de ARCA disponible y aclara que ARCA puede publicar otros
topes antes de la recategorización.

La regla de los últimos doce meses y las revisiones semestrales proviene de la
[guía oficial de recategorización](https://www.arca.gob.ar/monotributo/ayuda/recategorizacion.asp).
Los importes se descargan de la
[tabla oficial de categorías](https://www.arca.gob.ar/monotributo/categorias.asp),
se validan y se guardan como respaldo; no están hardcodeados en Python.

La conclusión distingue tres casos: seguir en la misma categoría, bajar o subir. Las
descripciones largas de ARCA se normalizan a la letra antes de comparar, por lo que no
puede decir “bajar de F a F”.

Toda factura emitida por el bot se incorpora automáticamente al historial y a los
cálculos. Las facturas de puntos de venta externos sólo se incorporan cuando WSFE
permite consultarlas individualmente o mediante una importación privada; WSFE no
ofrece un endpoint de listado completo. No hay datos personales ni importes de ejemplo
precargados en el código.

El último mes importado funciona como corte. Desde el mes siguiente, cada mes cerrado
queda cubierto por la consulta completa de WSFE, incluso si el total es cero. Para que
esa continuidad sea válida, después del corte las facturas nuevas deben emitirse por
el punto Web Services del bot; una factura manual posterior no sería visible por esta
integración.

## Mensajes de ARCA

Consulta el Domicilio Fiscal Electrónico mediante el servicio oficial delegado. El bot
muestra resúmenes no leídos sin abrir la comunicación ni alterar su estado.

## Facturas

`/facturas` lista de a tres comprobantes y permite:

- ver el detalle;
- descargar el PDF Original;
- repetir una factura como borrador editable;
- crear una Factura C nueva.

Repetir nunca emite directamente. El usuario puede editar descripción, importe,
período y vencimiento, ve un resumen completo y debe pulsar una única acción
irreversible: `Emitir Factura C`.

La emisión usa WSFEv1, punto de venta `00002`, tipo `011`, control del último número,
CAE, vencimiento y recuperación ante timeouts. El punto manual `00001` no se modifica.
El PDF A4 incluye los datos autorizados, CAE y QR de la RG 4892. Si falla el PDF, se
regenera sin volver a emitir.

## Avisos automáticos

El monitor se ejecuta al iniciar el bot y luego cada cinco minutos. No vuelve a
consultar todos los sitios cada cinco minutos: evalúa la información almacenada y
actualiza las APIs oficiales como máximo cada seis horas.

`portal-worker` actualiza Monotributo, pagos e Ingresos Brutos cada seis horas. Los
valores por defecto son:

| Proceso | Intervalo |
|---|---:|
| Evaluación de alertas | 300 segundos |
| Recordatorio de servicios rotos | 3.600 segundos |
| APIs oficiales | 21.600 segundos |
| Portales ARCA/Santa Fe | 21.600 segundos |
| Tabla oficial de topes | 86.400 segundos |

Los snapshots de APIs oficiales duran treinta y seis horas y los de portales
veinticuatro horas. El bot avisa a las treinta horas si una API oficial sigue sin
actualizarse y a las doce si falla un portal, antes de que venza el dato guardado. La
evaluación de cinco minutos normalmente sólo lee SQLite; abre las APIs oficiales
únicamente cuando ya transcurrió el intervalo de seis horas. Los portales los actualiza
exclusivamente el worker en su propio ciclo.

Se recuerdan vencimientos a 14, 7, 3, 1 y 0 días. Una fuente se considera atrasada a
las 12 horas para cuentas y pagos, o a las 30 horas para perfil, mensajes y facturas.

Cada evento se guarda con una clave y una huella. Deudas, vencimientos, mensajes y
cambios de categoría no se repiten mientras su contenido no cambie. Una pérdida de
acceso o una fuente desactualizada sí se vuelve a enviar cada hora mientras el problema
continúe; al recuperarse deja de generar recordatorios. El intervalo se configura con
`AUTOMATIC_BROKEN_REMINDER_SECONDS` y por defecto es `3600`.

Las notificaciones usan las mismas reglas de conciliación que `/resumen` y nunca
muestran simultáneamente “deuda vencida” y “no vuelvas a pagar” para el mismo pago.

## Alta de credenciales

Desde `/ayuda` se genera un botón clickeable con un enlace privado. El enlace:

- vence a los diez minutos;
- puede usarse una sola vez;
- muestra el CUIT parcialmente oculto;
- envía la clave directamente a `portal-worker`;
- verifica el ingreso contra ARCA antes de guardar;
- cifra la clave con Fernet en el volumen de estado.

En producción, el formulario usa HTTP exclusivamente dentro del tailnet de Tailscale.
No se publica con un dominio o proxy de Internet. Tailscale cifra el transporte entre
el dispositivo y la VPS.

## Límites actuales

- Es una instalación personal con una lista cerrada de chats.
- Genera el VEP de la cuota corriente del Régimen Simplificado de Santa Fe. No pulsa
  la confirmación bancaria ni opera planes de pago o liquidaciones consolidadas.
- No presenta declaraciones juradas ni recategorizaciones.
- No consulta todavía todas las obligaciones nacionales fuera de Monotributo.
- No emite notas de crédito/débito ni facturas con múltiples ítems.
