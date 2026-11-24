# One image for everything: the website, the optional worker and the backup command.
# Build:  docker build -t klciie-portal .
ARG PYTHON_IMAGE=python:3.13-slim-trixie
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

# pg_dump (PostgreSQL 17 client), age and rclone are only used by the backup command.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client age rclone ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --require-hashes -r requirements.txt

COPY . .
RUN DEBUG=0 SECRET_KEY=build-only-not-secret python manage.py collectstatic --noinput

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000
CMD ["sh", "bin/start-web.sh"]
