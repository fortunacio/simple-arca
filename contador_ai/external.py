from __future__ import annotations

from collections.abc import Callable, Collection

import httpx


TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429})


class ExternalDependencyError(RuntimeError):
    kind = "EXTERNAL_ERROR"

    def __init__(
        self,
        *,
        host: str,
        stage: str,
        status_code: int | None = None,
    ):
        self.host = host or "dependencia externa"
        self.stage = stage
        self.status_code = status_code
        status = f", HTTP {status_code}" if status_code is not None else ""
        super().__init__(f"{self.kind}: {self.host}, etapa={stage}{status}")


class ExternalUnavailable(ExternalDependencyError):
    kind = "EXTERNAL_UNAVAILABLE"


class ExternalContractError(ExternalDependencyError):
    kind = "INTEGRATION_CHANGED"


def _exception_host(exc: httpx.HTTPError) -> str:
    request = getattr(exc, "request", None)
    return httpx.URL(str(request.url)).host if request is not None else ""


def checked_request(
    request: Callable[[], httpx.Response],
    *,
    stage: str,
    allow_statuses: Collection[int] = (),
) -> httpx.Response:
    try:
        response = request()
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise ExternalUnavailable(host=_exception_host(exc), stage=stage) from exc
    except httpx.HTTPError as exc:
        raise ExternalContractError(host=_exception_host(exc), stage=stage) from exc

    status = response.status_code
    if status in allow_statuses:
        return response
    host = httpx.URL(str(response.url)).host
    if status in TRANSIENT_HTTP_STATUSES or status >= 500:
        raise ExternalUnavailable(host=host, stage=stage, status_code=status)
    if status >= 400:
        raise ExternalContractError(host=host, stage=stage, status_code=status)
    return response
