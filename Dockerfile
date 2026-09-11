FROM python:3.12-slim AS builder

WORKDIR /app

ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ src/

RUN python -m pip install --upgrade pip \
    && python -m pip install --prefix=/install .

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/src
ENV OMNICLAW_X402_FACILITATOR_HOST=0.0.0.0
ENV OMNICLAW_X402_FACILITATOR_PORT=4022

COPY --from=builder /install /usr/local
COPY --from=builder /app/src /app/src
COPY scripts/start_hosted_exact_facilitator.py /app/scripts/start_hosted_exact_facilitator.py
COPY scripts/start_hosted_reconciler.py /app/scripts/start_hosted_reconciler.py
COPY scripts/hosted_metrics_exporter.py /app/scripts/hosted_metrics_exporter.py

RUN adduser --system --group --home /nonexistent facilitator \
    && mkdir -p /var/run/omniclaw \
    && chown -R facilitator:facilitator /app /var/run/omniclaw

EXPOSE 4022

USER facilitator

CMD ["python", "scripts/start_hosted_exact_facilitator.py"]
