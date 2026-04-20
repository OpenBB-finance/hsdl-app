#!/bin/sh
set -e


case "$1" in
    sync)
        shift
        exec python -m src.sync "$@"
        ;;
    serve)
        shift
        DB_PATH="${HSDL_DB_PATH:-/app/data/hsdl_catalog.db}"
        if [ ! -f "$DB_PATH" ]; then
            echo "Database not found at $DB_PATH. Running ingestion..."
            python -m src.sync
        fi
        exec uvicorn src.main:app --host 0.0.0.0 --port 7780 "$@"
        ;;
    *)
        echo "Usage: entrypoint.sh {sync|serve}"
        echo "  sync   - Scrape HSDL and build the SQLite catalog"
        echo "  serve  - Start the FastAPI/OpenBB backend on port 7780"
        exit 1
        ;;
esac
