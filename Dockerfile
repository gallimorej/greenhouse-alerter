# python:3.12-slim-bookworm, pinned by digest (multi-arch index; Cloud Run pulls linux/amd64).
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install --require-hashes --no-deps -r requirements.txt

COPY src/greenhouse ./greenhouse

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

# ROLE (worker|admin) and GUNICORN_THREADS come from the Cloud Run service definition.
CMD exec gunicorn --bind ":${PORT:-8080}" --workers 1 --threads "${GUNICORN_THREADS:-4}" \
    --timeout 30 --access-logfile - "greenhouse.web:create_app()"
