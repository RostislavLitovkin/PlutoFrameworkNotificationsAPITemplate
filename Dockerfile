# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — build the virtualenv
#
# Compilers live here and are thrown away, so a missing wheel degrades to a
# slower build rather than a failed one, without shipping gcc in the runtime.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied on its own so the dependency layer is only rebuilt when it changes.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt


# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=ApiCore.settings \
    PORT=8000

COPY --from=builder /opt/venv /opt/venv

# A fixed high UID keeps file ownership predictable across hosts and mounted volumes.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Application code stays root-owned and read-only to the process that serves it;
# only the static output directory needs to be writable.
COPY . /app
RUN chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /app/static \
    && chown appuser:appuser /app/static

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]

# Wrapped in `sh -c` so $PORT and the sizing variables are expanded at run time:
# platforms that assign a port (Render, Fly, Cloud Run, Heroku) then work with no
# override. `exec` keeps gunicorn as the process the entrypoint hands control to.
CMD ["sh", "-c", "exec gunicorn ApiCore.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-3} --threads ${GUNICORN_THREADS:-2} --timeout ${GUNICORN_TIMEOUT:-60} --access-logfile - --error-logfile -"]
