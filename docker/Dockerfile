# Multi-stage. Three targets:
#   base       shared dependencies
#   worker     spiders and transforms; carries Chrome for the browser portals
#   scheduler  Airflow, without Chrome, because the scheduler never renders

FROM python:3.11-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libxml2-dev \
        libxslt1-dev \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip==23.3.1 && python -m pip install -e .


FROM base AS worker

# Chromium rather than Chrome: the version is pinned by the distribution, which
# keeps the driver and the browser in step without a download step.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV STATEHOUSE_BROWSER_HEADLESS=true \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver

RUN python -m pip install -e ".[scraping,storage]"

COPY scripts ./scripts

RUN useradd --create-home --uid 10001 statehouse \
    && mkdir -p /app/var /app/reports \
    && chown -R statehouse:statehouse /app
USER statehouse

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD statehouse version || exit 1

ENTRYPOINT ["statehouse"]
CMD ["jurisdictions", "--enabled-only"]


FROM base AS scheduler

ARG AIRFLOW_VERSION=2.7.3
ARG PYTHON_VERSION=3.11

RUN python -m pip install \
        "apache-airflow==${AIRFLOW_VERSION}" \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

RUN python -m pip install -e ".[storage]"

ENV AIRFLOW_HOME=/opt/airflow \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/dags \
    AIRFLOW__LOGGING__LOGGING_LEVEL=INFO

COPY airflow/dags /opt/airflow/dags
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 airflow \
    && mkdir -p /opt/airflow/logs \
    && chown -R airflow:airflow /opt/airflow /app
USER airflow

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 \
    CMD airflow jobs check --job-type SchedulerJob --hostname "$(hostname)" || exit 1

ENTRYPOINT ["airflow"]
CMD ["scheduler"]
