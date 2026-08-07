FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TAX_MONITOR_DB=/data/tax_monitor.sqlite3

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=appuser:appuser contador_ai ./contador_ai
COPY --chown=appuser:appuser --chmod=0555 scripts/container_entrypoint.sh /usr/local/bin/container-entrypoint

USER appuser
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["python", "-m", "contador_ai", "--healthcheck"]

ENTRYPOINT ["container-entrypoint"]
CMD ["python", "-m", "contador_ai"]
