# syntax=docker/dockerfile:1
#
# Moana Viewer (Pacific Ocean Portal): Django + gunicorn.
# Data is NOT in the image; docker-compose mounts it read-only at /data.
# Build from the project root:  docker compose -f deploy/docker-compose.yml build

# --- packages: installed into a virtualenv, with a compiler at hand for any that
# have no ready-made wheel for this CPU (e.g. mapbox_earcut on ARM). The runtime
# image copies the virtualenv, not the compiler.
# rasterio (GDAL), pyproj (PROJ), netCDF4 and laspy ship as wheels with their
# libraries inside, so no system GIS packages are needed.
FROM python:3.12-slim AS packages
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
COPY requirements.txt ./
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --- runtime
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Libraries that write config or caches under $HOME get the (tmpfs) /tmp.
    HOME=/tmp \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

COPY --from=packages /opt/venv /opt/venv

COPY manage.py ./
COPY config ./config
COPY viewer ./viewer
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

# Static files are collected at build time and served by WhiteNoise.
RUN DJANGO_DEBUG=false DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput \
 && chmod +x /usr/local/bin/entrypoint.sh \
 && groupadd -g 1001 app && useradd -u 1001 -g app -M -d /app -s /usr/sbin/nologin app \
 && mkdir -p /data /var/lib/moana && chown app:app /var/lib/moana

USER app
EXPOSE 8000
ENTRYPOINT ["entrypoint.sh"]
# Long timeout: opening a point cloud converts it to 3D Tiles in the request (once).
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2} --threads ${GUNICORN_THREADS:-8} --timeout ${GUNICORN_TIMEOUT:-1800} --access-logfile - --error-logfile -"]
