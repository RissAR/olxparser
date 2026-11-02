#!/bin/sh
set -e

# Resolve target DB path and ensure parent directory exists
DB_FILE="${OLX_DB_PATH:-/app/data/olx_storage.db}"
DB_DIR=$(dirname "$DB_FILE")

if [ -n "$DB_DIR" ] && [ "$DB_DIR" != "." ] && [ ! -d "$DB_DIR" ]; then
    mkdir -p "$DB_DIR"
fi

# Ensure exports directory exists
EXP_DIR="${OLX_EXPORTS_DIR:-/app/exports}"
if [ -n "$EXP_DIR" ] && [ ! -d "$EXP_DIR" ]; then
    mkdir -p "$EXP_DIR"
fi

# If initial template/seed DB exists in /app and destination in mounted volume is empty, initialize it
if [ -f "/app/olx_storage.db" ] && [ "$DB_FILE" != "/app/olx_storage.db" ] && [ ! -f "$DB_FILE" ]; then
    echo "[entrypoint] Initializing persistent database at $DB_FILE from bundled storage..."
    cp /app/olx_storage.db "$DB_FILE" || true
fi

exec "$@"
