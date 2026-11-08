ARG PYTHON_BASE=python:3.12-slim
FROM ${PYTHON_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt && groupadd -g 70 pgsocket && useradd -u 10001 -g 70 -M vault
COPY vault ./vault
USER 10001:70
EXPOSE 8000
CMD ["uvicorn", "vault.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--log-level", "warning"]
