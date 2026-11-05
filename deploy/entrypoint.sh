#!/bin/sh
# Prepare the state volume and database, then run the server (the image's CMD).
set -e

mkdir -p "$(dirname "$DATABASE_PATH")" "$CACHE_DIR" "$FILE_UPLOAD_TEMP_DIR"
python manage.py migrate --noinput

exec "$@"
