FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock requirements-fallback.lock ./
RUN pip install --no-cache-dir -r requirements.lock -r requirements-fallback.lock \
    && groupadd --gid 10001 commerce \
    && useradd --uid 10001 --gid commerce --no-create-home commerce
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 scripts ./scripts
COPY --chown=10001:10001 schema ./schema
COPY --chown=10001:10001 migrations ./migrations
USER 10001:10001
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--workers", "1"]
