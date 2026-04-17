#!/bin/sh
set -e

if [ ! -f "${HSDL_DB_PATH:-/app/data/hsdl_catalog.db}" ] && [ -f /app/_built_data/hsdl_catalog.db ]; then
    echo "Seeding database from build-time snapshot..."
    cp /app/_built_data/hsdl_catalog.db "${HSDL_DB_PATH:-/app/data/hsdl_catalog.db}"
    if [ -f /app/_built_data/hsdl_catalog_hierarchy.json ]; then
        cp /app/_built_data/hsdl_catalog_hierarchy.json "$(dirname "${HSDL_DB_PATH:-/app/data/hsdl_catalog.db}")/hsdl_catalog_hierarchy.json"
    fi
fi

case "$1" in
    sync)
        shift
        exec python -m src.sync "$@"
        ;;
    serve)
        shift
        exec uvicorn src.main:app --host 0.0.0.0 --port 7780 "$@"
        ;;
    *)
        echo "Usage: entrypoint.sh {sync|serve}"
        echo "  sync   - Scrape HSDL and build the SQLite catalog"
        echo "  serve  - Start the FastAPI/OpenBB backend on port 7780"
        exit 1
        ;;
esac
