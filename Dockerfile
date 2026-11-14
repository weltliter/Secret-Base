ARG PYTHON_BASE=python:3.12-slim-trixie
FROM ${PYTHON_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# Debian trixie supplies PostgreSQL 17. Do not install the unversioned client
# metapackage: the backup format must match the deployed database major.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 \
    && pg_dump --version | grep -E '^pg_dump \(PostgreSQL\) 17\.' \
    && pg_restore --version | grep -E '^pg_restore \(PostgreSQL\) 17\.' \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt && groupadd -g 70 pgsocket && useradd -u 10001 -g 70 -M vault
COPY vault ./vault
COPY VERSION ./VERSION
USER 10001:70
EXPOSE 8000
CMD ["uvicorn", "vault.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--log-level", "warning"]
