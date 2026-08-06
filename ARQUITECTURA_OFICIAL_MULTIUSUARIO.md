# Arquitectura de integración tributaria multiusuario

## Conclusión

No existe una API pública única que entregue bandeja, deuda, facturación y estado de
Ingresos Brutos. La arquitectura robusta para este producto es un **operador
autorizado**, no una colección de claves fiscales ni un navegador por cliente.

Cada usuario deberá:

1. identificarse en el producto;
2. vincular su chat de Telegram;
3. delegar únicamente los servicios tributarios necesarios a la identidad operadora;
4. poder revocar esas delegaciones desde ARCA.

El backend operará con dos clases de conectores:

- **Web services oficiales:** WSAA, padrón, WSCCOMU y WSFE.
- **Servicios interactivos delegados:** CCMA, Mis Comprobantes/Portal IVA y los
  aplicativos de Santa Fe, mientras no exista un acceso máquina-a-máquina aprobado.

Las credenciales fiscales de cada contribuyente no se solicitan ni se almacenan.

## Matriz de cobertura

| Necesidad | Fuente robusta | Cobertura | Limitación confirmada |
|---|---|---:|---|
| Identidad del usuario | AUTENTICAR, mediante OIDC | Completa para identidad | No autoriza acceso a datos tributarios |
| Situación tributaria | Padrón ARCA A4/A10/A13 y constancia | Oficial | Debe validarse qué alcance devuelve la categoría exacta |
| Bandeja fiscal | WSCCOMU, servicio `veconsumerws` | Oficial | Abrir contenido puede registrar lectura |
| Facturas emitidas por el producto | WSFEv1 | Oficial | Sólo cubre puntos de venta Web Services |
| Facturas históricas de Comprobantes en Línea | Mis Comprobantes o Portal IVA | Interactiva | No se publicó una API general de lectura |
| Deuda de Monotributo | CCMA | Interactiva | No se publicó una API de autoservicio |
| Otra deuda ARCA | Sistema de Cuentas Tributarias | Interactiva | `sud_restricciones` está reservado a bancos |
| Estado y deuda de IIBB Santa Fe | Régimen Simplificado, VEPAI y LDAE | Interactiva | No se encontró una API pública para contribuyentes |

## Autenticación y autorización

### Identidad del producto

AUTENTICAR admite aplicaciones del sector privado y usa OpenID Connect. Puede
resolver un alta confiable del usuario sin que el producto reciba su clave fiscal.
Este login prueba identidad; no concede por sí solo acceso a CCMA, DFE ni Santa Fe.

### Delegación tributaria

ARCA permite que un Administrador de Relaciones designe usuarios externos y asocie
certificados X.509 a Web Services de Negocio. Son dos mecanismos relacionados pero
distintos: los WSN se delegan al certificado/DN; los servicios interactivos se
delegan a un usuario externo, que según la normativa vigente es una persona humana.
En producción, el certificado se gestiona en Administración de Certificados
Digitales y la relación se crea desde el Administrador de Relaciones.

Para cada contribuyente y servicio, el backend obtiene en WSAA un Ticket de Acceso
de duración limitada. Los tickets deben almacenarse cifrados y cachearse por:

```text
(CUIT representado, certificado/DN, servicio, ambiente)
```

La autorización se puede revocar desde ARCA sin modificar el usuario de Telegram.

## Conectores oficiales

### Domicilio Fiscal Electrónico

WSCCOMU recibe una `cuitRepresentada` previamente autorizada en el Ticket de
Acceso. `consultarComunicaciones` permite recuperar metadatos y estados. El bot no
debe ejecutar `consumirComunicacion` en segundo plano porque esa operación puede
registrar la lectura.

### Situación y categoría

Los servicios de padrón permiten consultar impuestos, regímenes y datos registrales.
Antes de prometer la categoría de Monotributo debe hacerse una prueba de
homologación con A4, A10, A13 y constancia de inscripción; el producto debe exponer
el dato exacto sólo si el alcance oficial lo devuelve.

### Facturación

WSFEv1 permite emitir y consultar comprobantes del punto de venta asociado a Web
Services. ARCA exige puntos de venta distintos para Comprobantes en Línea y Web
Services. Por lo tanto, WSFEv1 no debe presentarse como una consulta universal de
todos los comprobantes históricos.

La prueba de producción del 05/08/2026 confirmó que el punto usado por Comprobantes
en Línea no aparece en `FEParamGetPtosVenta`: ARCA respondió el código 602, “Sin
Resultados”. La estrategia sostenible es:

1. importar el historial oficial de Comprobantes en Línea o Portal
   IVA;
2. no crear un punto de venta Web Services mientras el proyecto no incluya emisión
   de facturas.

## Deuda exacta

### ARCA

ARCA publica `sud_restricciones`, que informa deuda por CUIT, impuesto y período,
pero su catálogo indica expresamente que el uso está reservado a entidades
bancarias. CCMA continúa siendo la fuente oficial para Monotributo y es un servicio
interactivo con clave fiscal.

En el catálogo interno de WSASS de homologación aparece además el servicio
`siam-ccma-ws-deudas`, descrito como “Webservice con deudas de CCMA para organismos
externos”. No aparece documentado en el catálogo público de WSN y su presencia en
WSASS no demuestra habilitación para una empresa privada ni disponibilidad en
producción. Es una vía institucional concreta para consultar a ARCA antes de
implementar un adaptador interactivo.

La solución de producción debe seguir dos caminos en paralelo:

1. solicitar a ARCA acceso institucional a un servicio de consulta de deuda o la
   habilitación de un Web Service de Negocio aplicable al producto;
2. mientras ese acceso no exista, operar CCMA como usuario externo expresamente
   delegado, con un adaptador aislado y auditable.

No corresponde presentar las llamadas internas de CCMA como API pública ni basar
en ellas una garantía de disponibilidad.

### Santa Fe

Los servicios provinciales se habilitan y pueden delegarse desde el Administrador
de Relaciones de ARCA. Esto permite que uno o más usuarios externos expresamente
autorizados atiendan contribuyentes representados sin conocer sus claves fiscales.

No se encontró una API pública para consultar VEPAI, Régimen Simplificado o LDAE.
Santa Fe sí publica accesos de datos para municipios y comunas mediante convenios,
lo que confirma que el canal institucional existe, aunque no prueba que esté
disponible para una empresa privada.

Se debe solicitar formalmente:

- autenticación máquina-a-máquina;
- consulta de inscripción, categoría y cuotas;
- consulta de pagos imputados;
- consulta de deuda por período e intereses;
- ambiente de homologación, límites, SLA y condiciones de uso.

## Adaptador interactivo escalable

Si ARCA o Santa Fe no conceden una API para deuda, la alternativa transitoria no es
un Chromium por usuario. Es un **pool pequeño de sesiones del operador autorizado**:

```text
Usuarios de Telegram
        |
        v
API del producto ---- cola por organismo y CUIT
        |                         |
        |                         v
        |                 workers con límites
        |                         |
        +--> WSAA/WSN oficiales  +--> sesión delegada ARCA/Santa Fe
```

Características mínimas:

- una sesión por usuario externo autorizado o shard, no por contribuyente;
- selección explícita del CUIT representado;
- cookies cifradas, de vida corta y fuera de logs;
- navegador sólo para iniciar o renovar la sesión;
- solicitudes HTTP directas únicamente dentro de una sesión válida;
- cola serializada por CUIT para evitar cruces de contexto;
- límites de concurrencia por organismo;
- pruebas de contrato diarias y circuit breaker ante cambios;
- evidencia de fuente, fecha y CUIT en cada resultado;
- ninguna respuesta desde datos históricos cuando falle la consulta oficial.

Este diseño escala mucho mejor que una sesión por cliente, pero sigue siendo un
conector privado y debe tratarse como contingencia hasta obtener un convenio.

## Diseño recomendado del MVP

### Etapa 1: oficial y estable

- onboarding multiusuario;
- asociación Telegram ↔ identidad;
- WSAA y administración de certificados;
- padrón/constancia;
- WSCCOMU sin apertura automática;
- calendario y reglas versionadas;
- estado `NO_DISPONIBLE` para cualquier fuente que no haya respondido en vivo.

### Etapa 2: operador delegado

- instructivo para delegar CCMA y servicios de Santa Fe al usuario externo operador;
- pool de sesiones con un solo adapter por organismo;
- lectura de deuda y pagos;
- reconciliación con obligaciones esperadas;
- auditoría y revocación.

### Etapa 3: acceso institucional

- presentar las solicitudes técnicas a ARCA y API Santa Fe;
- sustituir cada adaptador interactivo por el endpoint concedido;
- conservar la misma interfaz interna para no modificar el bot.

## Decisiones de seguridad

- No almacenar CUIT + clave fiscal de clientes.
- No pedir tokens de ARCA por Telegram.
- No reutilizar cookies capturadas del navegador personal del cliente.
- Separar certificados, tickets WSAA y sesiones interactivas.
- Cifrar secretos con una clave fuera de la base de datos.
- Registrar quién consultó, a qué CUIT, qué fuente respondió y cuándo.
- Exigir confirmación reforzada para abrir DFE, generar VEP o iniciar pagos.
- Mantener el MVP en modo consulta; no ejecutar actos fiscales irreversibles.

## Consultas institucionales pendientes

### ARCA

Canal publicado para producción: `sri@arca.gob.ar`.

Preguntas:

1. ¿Puede un proveedor privado obtener acceso a un WSN para consultar deuda de
   contribuyentes que lo autoricen mediante el Administrador de Relaciones?
2. ¿Existe un WSN de CCMA o una modalidad distinta de `sud_restricciones` para
   software contable multi-CUIT?
   En particular, ¿qué convenio y documentación habilitan
   `siam-ccma-ws-deudas`?
3. ¿`veconsumerws` puede ser delegado por cualquier contribuyente a un certificado
   de operador externo para consultar su DFE?
4. ¿Qué servicios de padrón devuelven condición y categoría de Monotributo con
   vigencia?

### API Santa Fe

Canales publicados: `regimensimplificado@santafe.gob.ar`,
`ingresosbrutosrosario@santafe.gov.ar` y
`coordgralcomunicaciones@santafe.gov.ar`.

Preguntas:

1. ¿Existe web service o API para integrar VEPAI, Régimen Simplificado o LDAE?
2. ¿Puede contratarlo o solicitarlo un proveedor privado con delegación expresa de
   cada contribuyente?
3. ¿Qué trámite o convenio habilita homologación y producción?
4. ¿Existe documentación sobre autenticación, cuotas, límites y soporte técnico?

## Fuentes oficiales

- [Arquitectura de Web Services ARCA](https://arca.gob.ar/ws/documentacion/arquitectura-general.asp)
- [WSAA y certificados](https://arca.gob.ar/ws/documentacion/wsaa.asp)
- [Delegación de Web Services](https://www.arca.gob.ar/ws/WSAA/ADMINREL.DelegarWS.pdf)
- [Catálogo de Web Services](https://www.arca.gob.ar/ws/documentacion/catalogo.asp)
- [Manual WSCCOMU](https://www.arca.gob.ar/ws/WSCComu/vecuwsconcomunicaciones.pdf)
- [Administrador de Relaciones y usuarios externos](https://biblioteca.arca.gob.ar/search/query/norma.aspx?p=t%3ARAG%7Cn%3A5048%7Co%3A3%7Ca%3A2021%7Cf%3A11%2F08%2F2021)
- [Web Services de factura electrónica](https://arca.gob.ar/ws/documentacion/ws-factura-electronica.asp)
- [Puntos de venta por sistema de emisión](https://arca.gob.ar/fe/emision-autorizacion/solicitud-autorizacion.asp)
- [CCMA como fuente de deuda de Monotributo](https://www.arca.gob.ar/monotributo/ayuda/pagos.asp)
- [Alta de aplicación privada en AUTENTICAR](https://www.argentina.gob.ar/servicio/solicitar-el-alta-de-una-aplicacion-cliente-por-tramites-distancia)
- [Protocolo OIDC de AUTENTICAR](https://www.argentina.gob.ar/jefatura/innovacion-publica/innovacion-administrativa/autenticar/desarrolladores-autenticar-3)
- [Servicios API Santa Fe para municipios y comunas](https://www.santafe.gob.ar/index.php/web/content/view/full/117351)
- [Régimen Simplificado de Santa Fe](https://www.santafe.gob.ar/index.php/web/content/view/full/211373)
- [Contactos de API Santa Fe](https://www.santafe.gob.ar/index.php/web/content/view/full/250500)
