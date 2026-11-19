#!/bin/sh
# Container entrypoint: serve mode, same CLI the desktop app embeds.
# TRANSMUTE_TOKEN unset => python -m transmute.serve generates one and
# prints it once; fetch the ready-to-open link with:  docker compose logs
set -eu
port="${TRANSMUTE_PORT:-8765}"
maxmb="${TRANSMUTE_MAX_UPLOAD_MB:-2048}"
if [ -n "${TRANSMUTE_TOKEN:-}" ]; then
    exec python -m transmute.serve --lan --port "$port" \
        --max-upload-mb "$maxmb" --token "$TRANSMUTE_TOKEN"
else
    exec python -m transmute.serve --lan --port "$port" \
        --max-upload-mb "$maxmb"
fi
