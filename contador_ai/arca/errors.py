class ArcaError(RuntimeError):
    """Error base de una integración oficial con ARCA."""


class ArcaConfigurationError(ArcaError):
    """La configuración local no permite ejecutar la consulta."""


class ArcaTransportError(ArcaError):
    """El organismo no respondió o devolvió una respuesta HTTP inválida."""


class ArcaSoapFault(ArcaError):
    """ARCA devolvió un SOAP Fault."""


class ArcaResponseError(ArcaError):
    """La respuesta no cumple el contrato esperado."""


class WsfePointOfSaleUnavailable(ArcaResponseError):
    """El CUIT no tiene puntos de venta habilitados para Web Services."""
