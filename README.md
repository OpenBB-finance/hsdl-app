# HSDL Document Explorer

FastAPI backend that scrapes the Homeland Security Digital Library catalog into SQLite and serves it as an [OpenBB Workspace](https://pro.openbb.co) backend.

The database is built at Docker build time. The catalog is static (no scheduled refresh) — rebuild the image to pick up new documents.

## Local development

```bash
conda create -n hsdl python=3.12 -y
conda activate hsdl

pip install -e .

# sync catalog (builds the SQLite database)
python -m src.sync

# run the server
uvicorn src.main:app --host 0.0.0.0 --port 7780 --reload
```

The API will be available at `http://localhost:7780`. The sync step scrapes the HSDL public catalog and builds a SQLite database with FTS5 full-text search.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `HSDL_DB_PATH` | `data/hsdl_catalog.db` | Path to the SQLite file |
| `HSDL_DATA_DIR` | `data` | Directory for data files |
| `PORT` | `7780` | Server port |

## Docker

```bash
docker compose up --build
```

The Docker build runs `python -m src.sync` to bake the database into the image. At runtime, the entrypoint seeds the volume from the build-time snapshot if no database exists yet. An nginx sidecar handles LRU caching (512 MB, 7-day inactive eviction) and CORS.

Data persists in the `hsdl_data` volume.

## Dokku deployment

```bash
dokku apps:create hsdl

# persistent storage
dokku storage:ensure-directory hsdl-data
dokku storage:mount hsdl /var/lib/dokku/data/storage/hsdl-data:/app/data

# custom nginx config (LRU caching + CORS)
cp nginx.conf.sigil /home/dokku/hsdl/nginx.conf.sigil

# deploy
git push dokku main
```

The `nginx.conf.sigil` template provides LRU caching and CORS using Dokku's template variables for dynamic container routing.

## API endpoints

### Config & health

| Endpoint | Description |
|---|---|
| `GET /health` | Health check |
| `GET /widgets.json` | OpenBB widget definitions |
| `GET /apps.json` | OpenBB app layout |

### Browse & search

| Endpoint | Description |
|---|---|
| `GET /hsdl/resource-groups/options` | Resource group filter options |
| `GET /hsdl/years/options` | Year filter options |
| `GET /hsdl/subjects/options` | Subject filter options |
| `GET /hsdl/publishers/options` | Publisher filter options |
| `GET /hsdl/documents/options` | Document selector options |
| `GET /hsdl/documents/search` | Full-text + filtered document search |
| `GET /hsdl/documents/{docid}` | Single document detail |
| `GET /hsdl/hierarchy` | Category hierarchy with counts |

### Document viewer

| Endpoint | Description |
|---|---|
| `POST /hsdl/view-url` | Fetch and return PDF documents as base64 |
